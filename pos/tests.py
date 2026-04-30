from decimal import Decimal

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from account.models import Organization, UserAccount
from configuration.models import ApiToken, State, Warehouse
from sale.models import Party
from stock.models import Category, Item, StockMovement, TaxCode

from .models import Shift


class POSTerminalTests(APITestCase):
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

        self.user = User.objects.create_user(username='pos-user', password='password123')
        self.account = UserAccount.objects.create(
            user=self.user,
            organization=self.organization,
            account_type='org_user',
            role='Sales',
        )
        _, self.token = ApiToken.issue_token(self.account)

        self.shift = Shift.objects.create(
            user=self.user,
            warehouse=self.warehouse,
            opening_cash=Decimal('500.00'),
        )

        self.category = Category.objects.create(name='POS Category', organization=self.organization)
        tax_code = TaxCode.objects.create(name='Standard GST', code_type='HSN', code='1001', is_active=True)
        self.item = Item.objects.create(
            organization=self.organization,
            name='POS Item',
            sku='POS-001',
            category=self.category,
            tax_code=tax_code,
            cgst_rate=Decimal('9.00'),
            sgst_rate=Decimal('9.00'),
            igst_rate=Decimal('18.00'),
            current_stock=Decimal('10'),
            unit_price=Decimal('100.00'),
        )

    def auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def test_checkout_creates_invoice_receipt_and_stock_movement(self):
        self.auth()
        response = self.client.post(
            '/v1/pos/checkout/',
            {
                'shift': self.shift.id,
                'items': [
                    {
                        'item': self.item.id,
                        'quantity': '1.0000',
                        'rate': '100.00',
                        'discount': '0.00',
                    }
                ],
                'invoice_type': 'Cash',
                'payment_mode': 'Cash',
                'paid_amount': '118.00',
                'reference_number': 'CASH-001',
                'notes': 'POS counter sale',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['invoice']['grand_total'], '118.00')
        self.assertIn('print', response.data['invoice'])
        self.assertIsNotNone(response.data['receipt'])

        self.item.refresh_from_db()
        self.assertEqual(self.item.current_stock, Decimal('9.0000'))
        self.assertEqual(StockMovement.objects.filter(movement_type='Sale', posting_state='Posted').count(), 1)
        self.assertEqual(self.shift.transactions.filter(transaction_type='CashIn').count(), 1)

    def test_invoice_print_data_is_exposed_through_pos(self):
        self.auth()
        checkout = self.client.post(
            '/v1/pos/checkout/',
            {
                'shift': self.shift.id,
                'items': [
                    {
                        'item': self.item.id,
                        'quantity': '1.0000',
                        'rate': '100.00',
                        'discount': '0.00',
                    }
                ],
                'invoice_type': 'Cash',
                'payment_mode': 'Cash',
                'paid_amount': '118.00',
                'reference_number': 'CASH-002',
            },
            format='json',
        )
        invoice_id = checkout.data['invoice']['id']

        response = self.client.get(f'/v1/pos/invoices/{invoice_id}/print_data/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], invoice_id)
        self.assertIn('print', response.data)
        self.assertIn('share', response.data)

    def test_shift_reconciliation_reports_invoice_and_stock_totals(self):
        self.auth()
        self.client.post(
            '/v1/pos/checkout/',
            {
                'shift': self.shift.id,
                'items': [
                    {
                        'item': self.item.id,
                        'quantity': '1.0000',
                        'rate': '100.00',
                        'discount': '0.00',
                    }
                ],
                'invoice_type': 'Cash',
                'payment_mode': 'Cash',
                'paid_amount': '118.00',
                'reference_number': 'CASH-003',
            },
            format='json',
        )

        response = self.client.get(f'/v1/pos/shifts/{self.shift.id}/reconciliation/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['invoice_count'], 1)
        self.assertEqual(response.data['stock_movement_count'], 1)
        self.assertEqual(response.data['receipt_count'], 1)
        self.assertEqual(response.data['cash_in'], '118.00')
