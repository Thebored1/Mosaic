from decimal import Decimal

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from account.models import Organization, UserAccount
from configuration.models import ApiToken, State, SuperAdminToken, Warehouse
from sale.models import (
    Party, Order, OrderItem, Invoice, PriceList, PriceListItem, Quotation,
    PurchaseOrder, PurchaseOrderItem, GoodReceiptNote, GRNItem, PurchaseInvoice,
)
from stock.models import Category, Item, ItemVariant, Batch, StockMovement


class SaleAuthTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Org One')
        self.other_organization = Organization.objects.create(name='Org Two')

        self.state = State.objects.create(name='Maharashtra', state_code='27')

        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            state=self.state,
            gstin='27AAAAA0000A1Z5',
            name='Main Warehouse',
            code='WH1',
            legal_name='Org One Legal',
            address='Address 1',
        )
        self.other_warehouse = Warehouse.objects.create(
            organization=self.other_organization,
            state=self.state,
            gstin='29AAAAA0000A1Z5',
            name='Other Warehouse',
            code='WH2',
            legal_name='Org Two Legal',
            address='Address 2',
        )

        self.sales_user = User.objects.create_user(username='sales', password='password123')
        self.sales_account = UserAccount.objects.create(
            user=self.sales_user,
            organization=self.organization,
            account_type='org_user',
            role='Sales',
        )
        _, self.sales_token = ApiToken.issue_token(self.sales_account)

        self.super_user = User.objects.create_superuser(
            username='superadmin',
            email='super@example.com',
            password='password123',
        )
        _, self.super_token = SuperAdminToken.issue_token(self.super_user)

        self.party = Party.objects.create(
            organization=self.organization,
            name='Org One Party',
            party_type='Customer',
        )
        Party.objects.create(
            organization=self.other_organization,
            name='Org Two Party',
            party_type='Customer',
        )

        category = Category.objects.create(name='Category', organization=self.organization)
        self.item = Item.objects.create(
            organization=self.organization,
            name='Item One',
            sku='ITEM-001',
            category=category,
            current_stock=Decimal('20'),
            unit_price=Decimal('100.00'),
        )
        self.price_list = PriceList.objects.create(
            organization=self.organization,
            name='Default Price List',
            effective_from='2026-04-01',
        )
        PriceListItem.objects.create(
            price_list=self.price_list,
            item=self.item,
            rate=Decimal('80.00'),
        )

    def test_party_list_is_scoped_to_authenticated_organization(self):
        response = self.client.get(
            '/v1/sale/parties/',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'Org One Party')

    def test_super_admin_sale_queries_require_explicit_organization(self):
        response = self.client.get(
            '/v1/sale/parties/',
            HTTP_AUTHORIZATION=f'Bearer {self.super_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

    def test_order_creation_rejects_other_org_business_location(self):
        response = self.client.post(
            '/v1/sale/orders/',
            {
                'party': self.party.id,
                'business_location': self.other_warehouse.id,
                'items': [
                    {'item': self.item.id, 'quantity': '1', 'rate': '100.00'}
                ],
                'discount_amount': '0.00',
                'discount_type': 'Fixed',
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('business_location', response.data)

    def test_ecommerce_account_cannot_access_sale_api(self):
        ecommerce_user = User.objects.create_user(username='shopper', password='password123')
        ecommerce_account = UserAccount.objects.create(
            user=ecommerce_user,
            account_type='ecommerce',
            role='Staff',
        )
        _, ecommerce_token = ApiToken.issue_token(ecommerce_account)

        response = self.client.get(
            '/v1/sale/parties/',
            HTTP_AUTHORIZATION=f'Bearer {ecommerce_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_walk_in_order_conversion_uses_business_location_state(self):
        order = Order.objects.create(
            business_location=self.warehouse,
            party=None,
            discount_amount=Decimal('0.00'),
            discount_type='Fixed',
        )
        OrderItem.objects.create(
            order=order,
            item=self.item,
            quantity=Decimal('1.0000'),
            unit=None,
            rate=Decimal('100.00'),
            discount=Decimal('0.00'),
        )

        response = self.client.post(
            f'/v1/sale/orders/{order.id}/convert/',
            {
                'invoice_type': 'Tax Invoice',
                'due_date': '2026-05-30',
                'notes': 'Walk-in sale',
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invoice = Invoice.objects.get(id=response.data['invoice_id'])
        self.assertEqual(invoice.status, 'Draft')
        self.assertEqual(invoice.billing_state_id, self.state.id)

    def test_daily_sales_reports_only_finalized_invoices(self):
        Invoice.objects.create(
            invoice_number='INV-0001',
            invoice_type='Tax Invoice',
            billing_state=self.state,
            business_location=self.warehouse,
            grand_total=Decimal('100.00'),
            cgst_amount=Decimal('9.00'),
            sgst_amount=Decimal('9.00'),
            igst_amount=Decimal('0.00'),
            taxable_amount=Decimal('82.00'),
            status='Finalized',
        )
        Invoice.objects.create(
            invoice_number='INV-0002',
            invoice_type='Tax Invoice',
            billing_state=self.state,
            business_location=self.warehouse,
            grand_total=Decimal('200.00'),
            cgst_amount=Decimal('18.00'),
            sgst_amount=Decimal('18.00'),
            igst_amount=Decimal('0.00'),
            taxable_amount=Decimal('164.00'),
            status='Cancelled',
        )

        response = self.client.get(
            '/v1/sale/reports/daily_sales/',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['invoice_count'], 1)
        self.assertEqual(response.data['total_sales'], '100')

    def test_price_list_update_and_quotation_conversion_uses_snapshot_price(self):
        price_list_response = self.client.patch(
            f'/v1/sale/price-lists/{self.price_list.id}/',
            {
                'name': 'Updated Price List',
                'description': 'Updated pricing',
                'effective_from': '2026-04-01',
                'is_active': True,
                'notes': 'No notes',
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(price_list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(price_list_response.data['name'], 'Updated Price List')

        quotation_response = self.client.post(
            '/v1/sale/quotations/',
            {
                'party': self.party.id,
                'business_location': self.warehouse.id,
                'price_list': self.price_list.id,
                'quotation_date': '2026-04-30T10:00:00Z',
                'valid_until': '2026-05-30',
                'discount_amount': '0.00',
                'discount_type': 'Fixed',
                'discount_percent': '0.00',
                'items': [
                    {
                        'item': self.item.id,
                        'quantity': '2.0000',
                        'discount': '0.00',
                    }
                ],
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(quotation_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(quotation_response.data['grand_total'], '160.00')
        self.assertEqual(quotation_response.data['items'][0]['rate'], '80.00')

        convert_response = self.client.post(
            f"/v1/sale/quotations/{quotation_response.data['id']}/convert_to_invoice/",
            {
                'invoice_type': 'Tax Invoice',
                'due_date': '2026-05-30',
                'notes': 'Converted quote',
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(convert_response.status_code, status.HTTP_200_OK)
        invoice = Invoice.objects.get(id=convert_response.data['invoice_id'])
        self.assertEqual(invoice.grand_total, Decimal('160.00'))
        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.items.first().rate, Decimal('80.00'))
        invoice.finalize()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'Finalized')
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_stock, Decimal('18'))
        self.assertEqual(StockMovement.objects.filter(source_document_type='sale.InvoiceItem').count(), 1)
        quotation = Quotation.objects.get(id=quotation_response.data['id'])
        self.assertEqual(quotation.status, 'Converted')

    def test_grn_invoice_updates_po_receiving_and_batch(self):
        supplier = Party.objects.create(
            organization=self.organization,
            name='Supplier One',
            party_type='Supplier',
        )
        grn_category = Category.objects.create(name='GRN Category', organization=self.organization)
        grn_item_master = Item.objects.create(
            organization=self.organization,
            name='GRN Item',
            sku='GRN-001',
            category=grn_category,
            unit_price=Decimal('50.00'),
            cost_price=Decimal('40.00'),
        )
        variant = ItemVariant.objects.create(
            organization=self.organization,
            item=grn_item_master,
            sku='GRN-001-V1',
            unit_price=Decimal('50.00'),
            cost_price=Decimal('40.00'),
        )
        po = PurchaseOrder.objects.create(
            supplier=supplier,
            business_location=self.warehouse,
            status='Sent',
            created_by=self.sales_user,
        )
        po_item = PurchaseOrderItem.objects.create(
            purchase_order=po,
            item=grn_item_master,
            item_variant=variant,
            quantity_ordered=Decimal('5.0000'),
            unit=None,
            rate=Decimal('50.00'),
            discount=Decimal('0.00'),
        )
        grn = GoodReceiptNote.objects.create(
            purchase_order=po,
            supplier=supplier,
            business_location=self.warehouse,
            created_by=self.sales_user,
            supplier_invoice_number='SUP-001',
        )
        grn_line = GRNItem.objects.create(
            grn=grn,
            item=grn_item_master,
            item_variant=variant,
            quantity=Decimal('3.0000'),
            unit=None,
            rate=Decimal('50.00'),
        )

        response = self.client.post(
            f'/v1/sale/grns/{grn.id}/create_invoice/?organization={self.organization.id}',
            {
                'supplier_invoice_number': 'SUP-001',
                'supplier_invoice_date': '2026-05-01',
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.super_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        po_item.refresh_from_db()
        po.refresh_from_db()
        grn.refresh_from_db()
        grn_line.refresh_from_db()
        variant.refresh_from_db()

        self.assertEqual(po_item.quantity_received, Decimal('3.0000'))
        self.assertEqual(po.status, 'Partial')
        self.assertEqual(grn.status, 'Posted')
        self.assertIsNotNone(grn_line.batch_id)
        self.assertEqual(grn_line.batch.quantity_received, Decimal('3.0000'))
        self.assertEqual(grn_line.batch.quantity_remaining, Decimal('3.0000'))
        self.assertEqual(variant.current_stock, Decimal('3.0000'))

        purchase_invoice = PurchaseInvoice.objects.get(pk=response.data['purchase_invoice_id'])
        finalize_response = self.client.post(
            f'/v1/sale/purchase-invoices/{purchase_invoice.id}/finalize/?organization={self.organization.id}',
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.super_token}',
        )

        self.assertEqual(finalize_response.status_code, status.HTTP_200_OK)
        variant.refresh_from_db()
        self.assertEqual(variant.current_stock, Decimal('3.0000'))
