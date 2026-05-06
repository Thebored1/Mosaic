from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from datetime import date
from decimal import Decimal
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from account.models import Organization, UserAccount
from configuration.models import ApiToken, State, Warehouse
from stock.models import Batch, Category, Item, ItemImage, ItemVariant, OpeningStock, SerialNumber, StockMovement
from stock.services import approve_opening_stock, post_stock_movement, reverse_stock_movement


class StockAuthTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Org One')
        self.other_organization = Organization.objects.create(name='Org Two')

        self.sales_user = User.objects.create_user(username='sales', password='password123')
        self.sales_account = UserAccount.objects.create(
            user=self.sales_user,
            organization=self.organization,
            account_type='org_user',
            role='Sales',
        )
        _, self.sales_token = ApiToken.issue_token(self.sales_account)

        self.warehouse_user = User.objects.create_user(username='warehouse', password='password123')
        self.warehouse_account = UserAccount.objects.create(
            user=self.warehouse_user,
            organization=self.organization,
            account_type='org_user',
            role='Warehouse',
        )
        _, self.warehouse_token = ApiToken.issue_token(self.warehouse_account)

        Category.objects.create(name='Org One Category', organization=self.organization)
        Category.objects.create(name='Org Two Category', organization=self.other_organization)

    def test_api_tokens_are_stored_hashed(self):
        token = self.sales_account.api_token

        self.assertEqual(token.token, '')
        self.assertTrue(token.token_hash)
        self.assertEqual(token.token_prefix, self.sales_token[:8])

    def test_missing_token_returns_401(self):
        response = self.client.get('/v1/api/categories/')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_lowercase_bearer_and_org_scoping_work(self):
        response = self.client.get(
            '/v1/api/categories/',
            HTTP_AUTHORIZATION=f'bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'Org One Category')

    def test_sales_role_cannot_create_category(self):
        response = self.client.post(
            '/v1/api/categories/',
            {'name': 'Blocked Category', 'description': '', 'is_active': True},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_warehouse_role_can_create_category_for_own_org(self):
        response = self.client.post(
            '/v1/api/categories/',
            {'name': 'Warehouse Category', 'description': '', 'is_active': True},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.warehouse_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = Category.objects.get(name='Warehouse Category')
        self.assertEqual(created.organization, self.organization)

    def test_ecommerce_account_cannot_access_org_scoped_stock_api(self):
        ecommerce_user = User.objects.create_user(username='shopper', password='password123')
        ecommerce_account = UserAccount.objects.create(
            user=ecommerce_user,
            account_type='ecommerce',
            role='Staff',
        )
        _, ecommerce_token = ApiToken.issue_token(ecommerce_account)

        response = self.client.get(
            '/v1/api/categories/',
            HTTP_AUTHORIZATION=f'Bearer {ecommerce_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_item_export_and_bulk_import_work(self):
        response = self.client.get(
            '/v1/api/items/export/',
            HTTP_AUTHORIZATION=f'Bearer {self.warehouse_token}',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('sku', response.content.decode())

        import_response = self.client.post(
            '/v1/api/items/bulk_import/',
            {
                'items': [
                    {
                        'name': 'Imported Item',
                        'sku': 'IMP-001',
                        'unit_price': '25.00',
                        'cost_price': '10.00',
                        'current_stock': '5',
                        'min_stock_level': 2,
                        'max_stock_level': 20,
                        'is_active': True,
                    }
                ]
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.warehouse_token}',
        )

        self.assertEqual(import_response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Item.objects.filter(sku='IMP-001').exists())

    def test_min_stock_report_lists_low_inventory_items(self):
        Item.objects.create(
            organization=self.organization,
            name='Low Stock Item',
            sku='LOW-001',
            current_stock=Decimal('1'),
            min_stock_level=5,
            unit_price=Decimal('10.00'),
        )

        response = self.client.get(
            '/v1/api/reports/min_stock_alerts/',
            HTTP_AUTHORIZATION=f'Bearer {self.warehouse_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['rows'])

    def test_item_barcode_and_qr_endpoints_return_svg(self):
        item = Item.objects.create(
            organization=self.organization,
            name='Barcode Item',
            sku='BAR-001',
            current_stock=Decimal('3'),
            unit_price=Decimal('10.00'),
        )

        barcode_response = self.client.get(
            f'/v1/api/items/{item.id}/barcode/',
            HTTP_AUTHORIZATION=f'Bearer {self.warehouse_token}',
        )
        qr_response = self.client.get(
            f'/v1/api/items/{item.id}/qr/',
            HTTP_AUTHORIZATION=f'Bearer {self.warehouse_token}',
        )

        self.assertEqual(barcode_response.status_code, status.HTTP_200_OK)
        self.assertEqual(qr_response.status_code, status.HTTP_200_OK)
        self.assertIn('<svg', barcode_response.content.decode())
        self.assertIn('<svg', qr_response.content.decode())


class StockPostingTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Org One')
        self.state = State.objects.create(name='Maharashtra', state_code='27', organization=self.organization)
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            state=self.state,
            gstin='27AAAAA0000A1Z5',
            name='Main Warehouse',
            code='WH1',
            legal_name='Org One Legal',
            address='Address 1',
        )
        self.warehouse_user = User.objects.create_user(username='warehouse-user', password='password123')
        self.warehouse_account = UserAccount.objects.create(
            user=self.warehouse_user,
            organization=self.organization,
            account_type='org_user',
            role='Warehouse',
        )
        _, self.warehouse_token = ApiToken.issue_token(self.warehouse_account)

    def test_fifo_sale_consumes_batches(self):
        item = Item.objects.create(
            organization=self.organization,
            name='Variant Item',
            sku='VAR-001',
            has_variants=True,
            current_stock=None,
            unit_price=Decimal('100.00'),
        )
        variant = ItemVariant.objects.create(
            organization=self.organization,
            item=item,
            sku='VAR-001-RED',
            unit_price=Decimal('100.00'),
            current_stock=Decimal('10'),
        )
        batch_one = Batch.objects.create(
            organization=self.organization,
            batch_number='BATCH-001',
            item_variant=variant,
            quantity_received=Decimal('6'),
            quantity_remaining=Decimal('6'),
            cost_per_unit=Decimal('70.00'),
            received_date=date(2026, 4, 1),
        )
        batch_two = Batch.objects.create(
            organization=self.organization,
            batch_number='BATCH-002',
            item_variant=variant,
            quantity_received=Decimal('4'),
            quantity_remaining=Decimal('4'),
            cost_per_unit=Decimal('75.00'),
            received_date=date(2026, 4, 2),
        )

        movement = post_stock_movement(
            organization=self.organization,
            movement_type='Sale',
            item=item,
            item_variant=variant,
            warehouse=self.warehouse,
            quantity=Decimal('7'),
            rate=Decimal('100.00'),
            reference_number='INV-1',
            status='Approved',
            source_document_type='sale.InvoiceItem',
            source_document_id='1',
            source_line_reference='1',
        )

        variant.refresh_from_db()
        batch_one.refresh_from_db()
        batch_two.refresh_from_db()
        self.assertEqual(variant.current_stock, Decimal('3.0000'))
        self.assertEqual(batch_one.quantity_remaining, Decimal('0.0000'))
        self.assertEqual(batch_two.quantity_remaining, Decimal('3.0000'))
        self.assertEqual(movement.posting_state, 'Posted')
        self.assertEqual(len(movement.allocation_data['batch_allocations']), 2)

    def test_serial_sale_and_reverse_restores_serials(self):
        item = Item.objects.create(
            organization=self.organization,
            name='Serialized Item',
            sku='SER-001',
            requires_serial_tracking=True,
            current_stock=Decimal('5'),
            unit_price=Decimal('200.00'),
        )
        SerialNumber.objects.create(
            organization=self.organization,
            serial_number='SN-001',
            item=item,
            warehouse=self.warehouse,
        )
        SerialNumber.objects.create(
            organization=self.organization,
            serial_number='SN-002',
            item=item,
            warehouse=self.warehouse,
        )
        SerialNumber.objects.create(
            organization=self.organization,
            serial_number='SN-003',
            item=item,
            warehouse=self.warehouse,
        )
        movement = post_stock_movement(
            organization=self.organization,
            movement_type='Sale',
            item=item,
            quantity=Decimal('2'),
            rate=Decimal('200.00'),
            warehouse=self.warehouse,
            reference_number='INV-2',
            status='Approved',
            source_document_type='sale.InvoiceItem',
            source_document_id='2',
            source_line_reference='2',
        )

        item.refresh_from_db()
        self.assertEqual(item.current_stock, Decimal('3.0000'))
        self.assertEqual(movement.serial_numbers.count(), 2)
        self.assertEqual(SerialNumber.objects.filter(status='Sold').count(), 2)

        reverse_stock_movement(movement, reference_number='CN-2', notes='Cancel sale')
        item.refresh_from_db()
        self.assertEqual(item.current_stock, Decimal('5.0000'))
        self.assertEqual(SerialNumber.objects.filter(status='Available').count(), 3)

    def test_opening_stock_approval_posts_inventory(self):
        item = Item.objects.create(
            organization=self.organization,
            name='Opening Item',
            sku='OPEN-001',
            current_stock=Decimal('0'),
            unit_price=Decimal('50.00'),
        )
        opening_stock = OpeningStock.objects.create(
            organization=self.organization,
            item=item,
            quantity=Decimal('4'),
            unit_cost=Decimal('40.00'),
            as_of_date=date(2026, 4, 1),
            notes='Seed stock',
        )

        approve_opening_stock(opening_stock)
        item.refresh_from_db()
        opening_stock.refresh_from_db()
        self.assertEqual(opening_stock.status, 'Approved')
        self.assertEqual(item.current_stock, Decimal('4.0000'))
        self.assertEqual(StockMovement.objects.filter(movement_type='Opening', posting_state='Posted').count(), 1)

    def test_stock_transfer_between_warehouses_moves_serial_stock(self):
        destination = Warehouse.objects.create(
            organization=self.organization,
            state=self.state,
            gstin='27BBBBB0000B1Z5',
            name='Destination Warehouse',
            code='WH2',
            legal_name='Org One Legal',
            address='Address 2',
        )
        item = Item.objects.create(
            organization=self.organization,
            name='Transfer Item',
            sku='TRF-001',
            requires_serial_tracking=True,
            current_stock=Decimal('2'),
            unit_price=Decimal('100.00'),
            cost_price=Decimal('80.00'),
        )
        serial_one = SerialNumber.objects.create(
            organization=self.organization,
            serial_number='TRF-SN-001',
            item=item,
            warehouse=self.warehouse,
        )
        SerialNumber.objects.create(
            organization=self.organization,
            serial_number='TRF-SN-002',
            item=item,
            warehouse=self.warehouse,
        )

        response = self.client.post(
            '/v1/api/stock-movements/transfer/',
            {
                'item': item.id,
                'from_warehouse': self.warehouse.id,
                'to_warehouse': destination.id,
                'quantity': '2',
                'notes': 'Move stock',
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.warehouse_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        item.refresh_from_db()
        serial_one.refresh_from_db()
        destination.refresh_from_db()

        self.assertEqual(item.current_stock, Decimal('2.0000'))
        self.assertEqual(serial_one.warehouse_id, destination.id)
        self.assertEqual(StockMovement.objects.filter(movement_type='TransferOut').count(), 1)
        self.assertEqual(StockMovement.objects.filter(movement_type='TransferIn').count(), 1)

    def test_stock_transfer_rejects_invalid_quantity_payload(self):
        destination = Warehouse.objects.create(
            organization=self.organization,
            state=self.state,
            gstin='27CCCCC0000C1Z5',
            name='Destination Warehouse',
            code='WH3',
            legal_name='Org One Legal',
            address='Address 3',
        )
        item = Item.objects.create(
            organization=self.organization,
            name='Transfer Item',
            sku='TRF-ERR-001',
            current_stock=Decimal('5'),
            unit_price=Decimal('100.00'),
            cost_price=Decimal('80.00'),
        )

        response = self.client.post(
            '/v1/api/stock-movements/transfer/',
            {
                'item': item.id,
                'from_warehouse': self.warehouse.id,
                'to_warehouse': destination.id,
                'quantity': 'abc',
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.warehouse_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data['quantity'], 'Transfer quantity must be a valid number.')


class StockImageTests(APITestCase):
    def test_failed_webp_conversion_is_logged_and_original_image_is_kept(self):
        organization = Organization.objects.create(name='Org One')
        category = Category.objects.create(name='Org One Category', organization=organization)
        item = Item.objects.create(
            organization=organization,
            name='Photo Item',
            sku='PHOTO-001',
            category=category,
            current_stock=Decimal('1'),
            unit_price=Decimal('100.00'),
        )

        upload = SimpleUploadedFile('photo.png', b'not a real image', content_type='image/png')

        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                with patch('PIL.Image.open', side_effect=OSError('boom')):
                    with self.assertLogs('stock.models', level='ERROR') as logs:
                        item_image = ItemImage.objects.create(
                            organization=organization,
                            item=item,
                            image=upload,
                        )

        item_image.refresh_from_db()
        self.assertTrue(item_image.image.name.endswith('.png'))
        self.assertTrue(any('Failed to convert item image' in entry for entry in logs.output))
