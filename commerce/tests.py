"""Tests for the commerce catalog, cart, and checkout flows."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from account.models import Organization, UserAccount
from configuration.models import ApiToken
from stock.models import Item, StockMovement

from .models import (
    Cart,
    CartItem,
    CommerceListing,
    CommerceNotification,
    CommerceOrder,
    MarketplacePayout,
    MarketplaceSettlement,
    CommercePriceOverride,
    CommerceReturnRequest,
    CommerceShipment,
    CommerceSettings,
    InventoryReservation,
    ShopperAddress,
)
from .tasks import (
    cleanup_stale_carts,
    expire_inventory_reservations,
    notify_order_placed,
    notify_shipment_update,
    notify_return_update,
    retry_marketplace_settlement_ready,
    retry_marketplace_payout_process,
)


class CommerceFlowTests(APITestCase):
    """Cover seller listing management and buyer checkout on the new commerce layer."""

    def setUp(self):
        self.seller_org = Organization.objects.create(name='Seller Org', trade_name='Seller')
        seller_user = User.objects.create_user(username='seller-owner', password='password123')
        self.seller_account = UserAccount.objects.create(
            user=seller_user,
            organization=self.seller_org,
            account_type='org_user',
            role='Owner',
        )
        _, self.seller_token = ApiToken.issue_token(self.seller_account)

        CommerceSettings.objects.create(
            organization=self.seller_org,
            reserve_stock_on_checkout=True,
            prevent_oversell=True,
            manual_fulfillment=True,
            manual_returns_only=True,
            allow_b2b_price_overrides=True,
        )

        self.seller_item = Item.objects.create(
            organization=self.seller_org,
            name='Seller Tee',
            sku='SELLER-TEE',
            description='Basic tee',
            current_stock=Decimal('25'),
            unit_price=Decimal('499.00'),
            cost_price=Decimal('250.00'),
        )

        buyer_user = User.objects.create_user(username='buyer-user', password='password123')
        self.buyer_account = UserAccount.objects.create(
            user=buyer_user,
            account_type='ecommerce',
            role='Staff',
        )
        _, self.buyer_token = ApiToken.issue_token(self.buyer_account)

        self.b2b_org = Organization.objects.create(name='Buyer Org', trade_name='Buyer')
        b2b_user = User.objects.create_user(username='b2b-buyer', password='password123')
        self.b2b_account = UserAccount.objects.create(
            user=b2b_user,
            organization=self.b2b_org,
            account_type='org_user',
            role='Staff',
        )
        _, self.b2b_token = ApiToken.issue_token(self.b2b_account)

    def auth(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def test_ecommerce_user_cannot_create_listing(self):
        """Ecommerce-only users should be blocked from seller listing management."""
        self.auth(self.buyer_token)
        response = self.client.post('/v1/commerce/listings/', {
            'item': self.seller_item.id,
            'title': 'Buyer Listing',
            'price': '499.00',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_seller_can_create_listing(self):
        """Organization owners can publish their own stock as commerce listings."""
        self.auth(self.seller_token)
        response = self.client.post('/v1/commerce/listings/', {
            'item': self.seller_item.id,
            'title': 'Seller Tee Listing',
            'description': 'Ready for marketplace',
            'price': '549.00',
            'min_quantity': '1.0000',
            'is_b2c_enabled': True,
            'is_b2b_enabled': True,
            'is_marketplace_enabled': True,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(CommerceListing.objects.count(), 1)
        listing = CommerceListing.objects.get()
        self.assertEqual(listing.organization_id, self.seller_org.id)

    def test_buyer_can_browse_add_address_and_checkout(self):
        """Buyer flow should create a cart, address, and storefront order."""
        listing = CommerceListing.objects.create(
            organization=self.seller_org,
            item=self.seller_item,
            title='Seller Tee Listing',
            description='Ready for sale',
            price=Decimal('549.00'),
        )

        self.auth(self.buyer_token)

        listing_response = self.client.get('/v1/commerce/listings/')
        self.assertEqual(listing_response.status_code, status.HTTP_200_OK)
        self.assertEqual(listing_response.data['count'], 1)

        address_response = self.client.post('/v1/commerce/addresses/', {
            'label': 'Home',
            'recipient_name': 'Buyer User',
            'phone': '9999999999',
            'line1': '42 Market Street',
            'line2': '',
            'city': 'Pune',
            'postal_code': '411001',
            'country': 'India',
            'is_default_shipping': True,
            'is_default_billing': True,
        }, format='json')
        self.assertEqual(address_response.status_code, status.HTTP_201_CREATED)
        address = ShopperAddress.objects.get()

        cart_item_response = self.client.post('/v1/commerce/cart-items/', {
            'listing_id': listing.id,
            'quantity': '2.0000',
        }, format='json')
        self.assertIn(cart_item_response.status_code, {status.HTTP_200_OK, status.HTTP_201_CREATED})

        cart_response = self.client.get('/v1/commerce/carts/')
        self.assertEqual(cart_response.status_code, status.HTTP_200_OK)
        self.assertEqual(cart_response.data['grand_total'], '1098.00')

        checkout_response = self.client.post('/v1/commerce/carts/checkout/', {
            'billing_address_id': address.id,
            'shipping_address_id': address.id,
            'notes': 'Leave at gate',
        }, format='json')
        self.assertEqual(checkout_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(CommerceOrder.objects.count(), 1)
        order = CommerceOrder.objects.get()
        self.assertEqual(order.lines.count(), 1)
        self.assertEqual(order.lines.get().organization_id, self.seller_org.id)
        self.assertEqual(InventoryReservation.objects.count(), 1)
        self.assertEqual(InventoryReservation.objects.get().status, 'reserved')
        self.assertEqual(StockMovement.objects.filter(movement_type='Reserve').count(), 1)

    def test_checkout_idempotency_returns_the_same_order(self):
        listing = CommerceListing.objects.create(
            organization=self.seller_org,
            item=self.seller_item,
            title='Seller Tee Listing',
            description='Ready for sale',
            price=Decimal('549.00'),
        )
        address = ShopperAddress.objects.create(
            user_account=self.buyer_account,
            label='Home',
            recipient_name='Buyer User',
            phone='9999999999',
            line1='42 Market Street',
            city='Pune',
            postal_code='411001',
            country='India',
        )

        self.auth(self.buyer_token)
        self.client.post('/v1/commerce/cart-items/', {'listing_id': listing.id, 'quantity': '1.0000'}, format='json')

        first = self.client.post(
            '/v1/commerce/carts/checkout/',
            {
                'billing_address_id': address.id,
                'shipping_address_id': address.id,
            },
            format='json',
            HTTP_IDEMPOTENCY_KEY='checkout-001',
        )
        second = self.client.post(
            '/v1/commerce/carts/checkout/',
            {
                'billing_address_id': address.id,
                'shipping_address_id': address.id,
            },
            format='json',
            HTTP_IDEMPOTENCY_KEY='checkout-001',
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(CommerceOrder.objects.count(), 1)
        self.assertEqual(first.data['id'], second.data['id'])

    def test_checkout_reservation_can_be_released_before_shipment(self):
        """Checkout should reserve stock and cancellation before shipment should release it."""
        listing = CommerceListing.objects.create(
            organization=self.seller_org,
            item=self.seller_item,
            title='Seller Tee Listing',
            description='Ready for sale',
            price=Decimal('549.00'),
        )
        address = ShopperAddress.objects.create(
            user_account=self.buyer_account,
            label='Home',
            recipient_name='Buyer User',
            phone='9999999999',
            line1='42 Market Street',
            city='Pune',
            postal_code='411001',
            country='India',
        )

        self.auth(self.buyer_token)
        self.client.post('/v1/commerce/cart-items/', {'listing_id': listing.id, 'quantity': '2.0000'}, format='json')
        checkout_response = self.client.post('/v1/commerce/carts/checkout/', {
            'billing_address_id': address.id,
            'shipping_address_id': address.id,
        }, format='json')
        self.assertEqual(checkout_response.status_code, status.HTTP_201_CREATED)
        order = CommerceOrder.objects.get()
        self.assertEqual(InventoryReservation.objects.filter(order=order, status='reserved').count(), 1)
        self.assertEqual(self.seller_item.current_stock, Decimal('25'))

        cancel_response = self.client.post(f'/v1/commerce/orders/{order.id}/cancel/', {'notes': 'Changed mind'}, format='json')
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(InventoryReservation.objects.filter(order=order, status='released').count(), 1)
        self.assertEqual(StockMovement.objects.filter(movement_type='Release').count(), 1)
        self.seller_item.refresh_from_db()
        self.assertEqual(self.seller_item.current_stock, Decimal('25'))

    def test_shipment_consumes_reserved_stock_and_blocks_cancel(self):
        """Manual shipment should consume reservations and prevent late cancellation."""
        listing = CommerceListing.objects.create(
            organization=self.seller_org,
            item=self.seller_item,
            title='Seller Tee Listing',
            description='Ready for sale',
            price=Decimal('549.00'),
        )
        address = ShopperAddress.objects.create(
            user_account=self.buyer_account,
            label='Home',
            recipient_name='Buyer User',
            phone='9999999999',
            line1='42 Market Street',
            city='Pune',
            postal_code='411001',
            country='India',
        )

        self.auth(self.buyer_token)
        self.client.post('/v1/commerce/cart-items/', {'listing_id': listing.id, 'quantity': '2.0000'}, format='json')
        self.client.post('/v1/commerce/carts/checkout/', {
            'billing_address_id': address.id,
            'shipping_address_id': address.id,
        }, format='json')
        order = CommerceOrder.objects.get()

        self.auth(self.seller_token)
        shipment_response = self.client.post('/v1/commerce/shipments/', {
            'order': order.id,
            'method': 'manual',
            'carrier_name': 'Manual',
            'tracking_number': 'TRACK123',
            'status': 'pending',
        }, format='json')
        self.assertEqual(shipment_response.status_code, status.HTTP_201_CREATED)
        shipment = CommerceShipment.objects.get(order=order)

        ship_response = self.client.post(f'/v1/commerce/shipments/{shipment.id}/ship/', {}, format='json')
        self.assertEqual(ship_response.status_code, status.HTTP_200_OK)
        self.seller_item.refresh_from_db()
        self.assertEqual(self.seller_item.current_stock, Decimal('23'))
        self.assertEqual(InventoryReservation.objects.filter(order=order, status='consumed').count(), 1)
        self.assertEqual(StockMovement.objects.filter(movement_type='Sale', posting_state='Posted').count(), 1)

        self.auth(self.buyer_token)
        cancel_response = self.client.post(f'/v1/commerce/orders/{order.id}/cancel/', {}, format='json')
        self.assertEqual(cancel_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_return_receipt_restock_and_b2b_price_override(self):
        """Returned stock should restock after receipt and B2B buyers should get negotiated pricing."""
        listing = CommerceListing.objects.create(
            organization=self.seller_org,
            item=self.seller_item,
            title='Seller Tee Listing',
            description='Ready for sale',
            price=Decimal('549.00'),
        )
        CommercePriceOverride.objects.create(
            seller_organization=self.seller_org,
            buyer_organization=self.b2b_org,
            listing=listing,
            price=Decimal('499.00'),
        )

        address = ShopperAddress.objects.create(
            user_account=self.b2b_account,
            label='Office',
            recipient_name='B2B Buyer',
            phone='9999999999',
            line1='100 Business Park',
            city='Mumbai',
            postal_code='400001',
            country='India',
        )

        self.auth(self.b2b_token)
        cart_item_response = self.client.post('/v1/commerce/cart-items/', {
            'listing_id': listing.id,
            'quantity': '1.0000',
        }, format='json')
        self.assertEqual(cart_item_response.status_code, status.HTTP_201_CREATED)
        cart_response = self.client.get('/v1/commerce/carts/')
        self.assertEqual(cart_response.data['grand_total'], '499.00')

        checkout_response = self.client.post('/v1/commerce/carts/checkout/', {
            'billing_address_id': address.id,
            'shipping_address_id': address.id,
        }, format='json')
        self.assertEqual(checkout_response.status_code, status.HTTP_201_CREATED)
        order = CommerceOrder.objects.get()
        order_line = order.lines.get()
        self.assertEqual(order_line.unit_price, Decimal('499.00'))

        self.auth(self.seller_token)
        shipment = CommerceShipment.objects.create(
            organization=self.seller_org,
            order=order,
            method='manual',
            status='pending',
            carrier_name='Manual',
            tracking_number='TRACK456',
        )
        shipment.mark_shipped()
        self.seller_item.refresh_from_db()
        self.assertEqual(self.seller_item.current_stock, Decimal('24'))

        self.auth(self.b2b_token)
        return_response = self.client.post('/v1/commerce/returns/', {
            'order': order.id,
            'shipment': shipment.id,
            'order_line': order_line.id,
            'quantity': '1.0000',
            'reason': 'Damaged on arrival',
        }, format='json')
        self.assertEqual(return_response.status_code, status.HTTP_201_CREATED)
        return_request = CommerceReturnRequest.objects.get()

        self.auth(self.seller_token)
        receive_response = self.client.post(f'/v1/commerce/returns/{return_request.id}/receive/', {}, format='json')
        self.assertEqual(receive_response.status_code, status.HTTP_200_OK)
        process_response = self.client.post(f'/v1/commerce/returns/{return_request.id}/process/', {}, format='json')
        self.assertEqual(process_response.status_code, status.HTTP_200_OK)
        self.seller_item.refresh_from_db()
        self.assertEqual(self.seller_item.current_stock, Decimal('25'))
        self.assertEqual(StockMovement.objects.filter(movement_type='Return', posting_state='Posted').count(), 1)

    def test_marketplace_checkout_splits_settlements_by_seller(self):
        """Marketplace checkout should create one settlement per seller organization."""
        second_org = Organization.objects.create(name='Second Seller', trade_name='Second')
        second_user = User.objects.create_user(username='second-seller', password='password123')
        second_account = UserAccount.objects.create(
            user=second_user,
            organization=second_org,
            account_type='org_user',
            role='Owner',
        )
        ApiToken.issue_token(second_account)
        CommerceSettings.objects.create(
            organization=second_org,
            reserve_stock_on_checkout=True,
            prevent_oversell=True,
            manual_fulfillment=True,
            manual_returns_only=True,
            allow_b2b_price_overrides=True,
            marketplace_commission_percent=Decimal('5.00'),
        )

        second_item = Item.objects.create(
            organization=second_org,
            name='Second Mug',
            sku='SECOND-MUG',
            description='Mug from second seller',
            current_stock=Decimal('8'),
            unit_price=Decimal('300.00'),
            cost_price=Decimal('120.00'),
        )

        first_listing = CommerceListing.objects.create(
            organization=self.seller_org,
            item=self.seller_item,
            title='Seller Tee Listing',
            description='Ready for marketplace',
            price=Decimal('549.00'),
        )
        second_listing = CommerceListing.objects.create(
            organization=second_org,
            item=second_item,
            title='Second Mug Listing',
            description='Ready for marketplace',
            price=Decimal('300.00'),
        )
        address = ShopperAddress.objects.create(
            user_account=self.buyer_account,
            label='Home',
            recipient_name='Buyer User',
            phone='9999999999',
            line1='42 Market Street',
            city='Pune',
            postal_code='411001',
            country='India',
        )

        self.auth(self.buyer_token)
        self.client.post('/v1/commerce/cart-items/?channel=marketplace', {
            'listing_id': first_listing.id,
            'quantity': '1.0000',
            'channel': 'marketplace',
        }, format='json')
        self.client.post('/v1/commerce/cart-items/?channel=marketplace', {
            'listing_id': second_listing.id,
            'quantity': '2.0000',
            'channel': 'marketplace',
        }, format='json')

        checkout_response = self.client.post('/v1/commerce/carts/checkout/?channel=marketplace', {
            'billing_address_id': address.id,
            'shipping_address_id': address.id,
            'channel': 'marketplace',
        }, format='json')
        self.assertEqual(checkout_response.status_code, status.HTTP_201_CREATED)

        order = CommerceOrder.objects.get(channel='marketplace')
        self.assertEqual(order.marketplace_settlements.count(), 2)

        seller_settlement = MarketplaceSettlement.objects.get(order=order, seller_organization=self.seller_org)
        second_settlement = MarketplaceSettlement.objects.get(order=order, seller_organization=second_org)
        self.assertEqual(seller_settlement.gross_amount, Decimal('549.00'))
        self.assertEqual(seller_settlement.commission_amount, Decimal('0.00'))
        self.assertEqual(seller_settlement.net_amount, Decimal('549.00'))
        self.assertEqual(second_settlement.gross_amount, Decimal('600.00'))
        self.assertEqual(second_settlement.commission_amount, Decimal('30.00'))
        self.assertEqual(second_settlement.net_amount, Decimal('570.00'))

        self.auth(self.seller_token)
        payout_response = self.client.post('/v1/commerce/marketplace-payouts/', {
            'settlement': seller_settlement.id,
            'method': 'manual',
            'status': 'pending',
        }, format='json')
        self.assertEqual(payout_response.status_code, status.HTTP_201_CREATED)
        payout = MarketplacePayout.objects.get(settlement=seller_settlement)
        process_response = self.client.post(f'/v1/commerce/marketplace-payouts/{payout.id}/process/', {'notes': 'Paid manually'}, format='json')
        self.assertEqual(process_response.status_code, status.HTTP_200_OK)
        payout.refresh_from_db()
        seller_settlement.refresh_from_db()
        self.assertEqual(payout.status, 'processed')
        self.assertEqual(seller_settlement.status, 'paid')

    def test_seller_can_view_seller_orders(self):
        """Seller order view should show only lines belonging to the seller org."""
        listing = CommerceListing.objects.create(
            organization=self.seller_org,
            item=self.seller_item,
            title='Seller Tee Listing',
            description='Ready for sale',
            price=Decimal('549.00'),
        )
        address = ShopperAddress.objects.create(
            user_account=self.buyer_account,
            label='Home',
            recipient_name='Buyer User',
            phone='9999999999',
            line1='42 Market Street',
            city='Pune',
            postal_code='411001',
            country='India',
        )
        cart = Cart.current_for_account(self.buyer_account)
        CartItem.objects.create(
            cart=cart,
            listing=listing,
            quantity=Decimal('1'),
            unit_price=listing.price,
        )
        CommerceOrder.create_from_cart(cart, billing_address=address, shipping_address=address)

        self.auth(self.seller_token)
        response = self.client.get('/v1/commerce/seller-orders/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(len(response.data['results'][0]['lines']), 1)

    def test_background_jobs_abandon_old_carts_and_expire_reservations(self):
        listing = CommerceListing.objects.create(
            organization=self.seller_org,
            item=self.seller_item,
            title='Seller Tee Listing',
            description='Ready for sale',
            price=Decimal('549.00'),
        )
        cart = Cart.current_for_account(self.buyer_account)
        CartItem.objects.create(
            cart=cart,
            listing=listing,
            quantity=Decimal('1'),
            unit_price=listing.price,
        )
        Cart.objects.filter(pk=cart.pk).update(updated_at=timezone.now() - timedelta(days=10))

        with self.assertLogs('commerce.tasks', level='INFO') as logs:
            cart_result = cleanup_stale_carts(days=7)
        cart.refresh_from_db()
        self.assertEqual(cart.status, 'abandoned')
        self.assertEqual(cart_result['abandoned_carts'], 1)
        self.assertTrue(any('Finished stale cart cleanup' in entry for entry in logs.output))

        address = ShopperAddress.objects.create(
            user_account=self.buyer_account,
            label='Home',
            recipient_name='Buyer User',
            phone='9999999999',
            line1='42 Market Street',
            city='Pune',
            postal_code='411001',
            country='India',
        )
        self.auth(self.buyer_token)
        self.client.post('/v1/commerce/cart-items/', {'listing_id': listing.id, 'quantity': '1.0000'}, format='json')
        self.client.post('/v1/commerce/carts/checkout/', {
            'billing_address_id': address.id,
            'shipping_address_id': address.id,
        }, format='json')
        reservation = InventoryReservation.objects.get()
        InventoryReservation.objects.filter(pk=reservation.pk).update(reserved_at=timezone.now() - timedelta(hours=48))

        with self.assertLogs('commerce.tasks', level='INFO') as logs:
            reservation_result = expire_inventory_reservations(hours=24)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, 'expired')
        self.assertEqual(reservation_result['expired_reservations'], 1)
        self.assertTrue(any('Finished inventory reservation expiry' in entry for entry in logs.output))

    def test_notification_and_retry_tasks_run(self):
        listing = CommerceListing.objects.create(
            organization=self.seller_org,
            item=self.seller_item,
            title='Seller Tee Listing',
            description='Ready for sale',
            price=Decimal('549.00'),
        )
        address = ShopperAddress.objects.create(
            user_account=self.buyer_account,
            label='Home',
            recipient_name='Buyer User',
            phone='9999999999',
            line1='42 Market Street',
            city='Pune',
            postal_code='411001',
            country='India',
        )

        self.auth(self.buyer_token)
        self.client.post('/v1/commerce/cart-items/', {'listing_id': listing.id, 'quantity': '1.0000'}, format='json')
        self.client.post('/v1/commerce/carts/checkout/', {
            'billing_address_id': address.id,
            'shipping_address_id': address.id,
        }, format='json')
        order = CommerceOrder.objects.get()

        with self.assertLogs('commerce.tasks', level='INFO') as logs:
            notify_order_placed(order.id)
        self.assertTrue(CommerceNotification.objects.filter(notification_type='order_placed').exists())
        self.assertTrue(any('Created order placed notifications' in entry for entry in logs.output))

        shipment = CommerceShipment.objects.create(
            organization=self.seller_org,
            order=order,
            method='manual',
            status='pending',
            carrier_name='Manual',
            tracking_number='TRACK-NOTIFY',
        )
        shipment.mark_shipped()
        with self.assertLogs('commerce.tasks', level='INFO') as logs:
            notify_shipment_update(shipment.id)
        self.assertTrue(CommerceNotification.objects.filter(notification_type='shipment_shipped').exists())
        self.assertTrue(any('Creating shipment update notification' in entry for entry in logs.output))

        return_request = CommerceReturnRequest.objects.create(
            organization=self.seller_org,
            order=order,
            shipment=shipment,
            order_line=order.lines.get(),
            quantity=Decimal('1.0000'),
            reason='Changed mind',
        )
        return_request.mark_received()
        with self.assertLogs('commerce.tasks', level='INFO') as logs:
            notify_return_update(return_request.id)
        self.assertTrue(CommerceNotification.objects.filter(notification_type='return_received').exists())
        self.assertTrue(any('Creating return update notification' in entry for entry in logs.output))

        settlement = MarketplaceSettlement.objects.create(
            order=order,
            seller_organization=self.seller_org,
            gross_amount=Decimal('100.00'),
            commission_rate=Decimal('0.00'),
            commission_amount=Decimal('0.00'),
            net_amount=Decimal('100.00'),
        )
        with self.assertLogs('commerce.tasks', level='INFO') as logs:
            retry_marketplace_settlement_ready(settlement.id)
        settlement.refresh_from_db()
        self.assertEqual(settlement.status, 'ready')
        self.assertTrue(any('Finished settlement ready task' in entry for entry in logs.output))

        payout = MarketplacePayout.objects.create(
            settlement=settlement,
            amount=Decimal('100.00'),
            method='manual',
            status='pending',
        )
        with self.assertLogs('commerce.tasks', level='INFO') as logs:
            retry_marketplace_payout_process(payout.id)
        payout.refresh_from_db()
        self.assertEqual(payout.status, 'processed')
        self.assertTrue(any('Finished payout process task' in entry for entry in logs.output))
