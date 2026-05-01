from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from account.models import Organization, UserAccount
from configuration.models import ApiToken, State, Warehouse
from sale.models import Party, Invoice, InvoiceItem, Receipt, PurchaseInvoice, PurchaseInvoiceItem
from stock.models import Category, Item
from accounting.models import JournalEntry, LedgerAccount


class AccountingPostingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.organization = Organization.objects.create(name='Org One')
        self.state = State.objects.create(name='Maharashtra', state_code='27')
        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            state=self.state,
            gstin='27AAAAA0000A1Z5',
            name='Main',
            code='WH1',
            legal_name='Org One Legal',
            address='Address',
        )
        self.user = User.objects.create_user(username='sales', password='password123')
        self.account = UserAccount.objects.create(user=self.user, organization=self.organization, account_type='org_user', role='Sales')
        _, self.token = ApiToken.issue_token(self.account)
        self.party = Party.objects.create(organization=self.organization, name='Customer One', party_type='Customer', state=self.state)
        category = Category.objects.create(name='Cat', organization=self.organization)
        self.item = Item.objects.create(organization=self.organization, name='Item', sku='SKU-1', category=category, current_stock=Decimal('10'), unit_price=Decimal('100.00'))

    def auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}

    def test_invoice_finalize_posts_balanced_journal(self):
        invoice = Invoice.objects.create(
            business_location=self.warehouse,
            party=self.party,
            billing_state=self.state,
            invoice_type='Tax Invoice',
            created_by=self.user,
        )
        InvoiceItem.objects.create(invoice=invoice, item=self.item, quantity=Decimal('1'), unit=None, rate=Decimal('100.00'), discount=Decimal('0.00'))
        invoice.calculate_totals()

        response = self.client.post(f'/v1/sale/invoices/{invoice.id}/finalize/', {}, format='json', **self.auth())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(JournalEntry.objects.count(), 1)
        entry = JournalEntry.objects.first()
        self.assertEqual(entry.total_debit, entry.total_credit)

    def test_receipt_create_posts_journal(self):
        invoice = Invoice.objects.create(
            business_location=self.warehouse,
            party=self.party,
            billing_state=self.state,
            invoice_type='Tax Invoice',
            created_by=self.user,
        )
        InvoiceItem.objects.create(invoice=invoice, item=self.item, quantity=Decimal('1'), unit=None, rate=Decimal('100.00'), discount=Decimal('0.00'))
        invoice.calculate_totals()
        self.client.post(f'/v1/sale/invoices/{invoice.id}/finalize/', {}, format='json', **self.auth())

        self.client.post('/v1/sale/receipts/', {
            'party': self.party.id,
            'business_location': self.warehouse.id,
            'amount': '100.00',
            'payment_mode': 'Cash',
            'reference_number': 'RC-1',
            'notes': '',
        }, format='json', **self.auth())

        self.assertTrue(JournalEntry.objects.filter(batch__source_event='receipt.create').exists())
