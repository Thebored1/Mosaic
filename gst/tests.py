from decimal import Decimal

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from account.models import Organization, UserAccount
from configuration.models import ApiToken, State, Warehouse
from gst.models import GSTEInvoice, GSTEWayBill, GSTReturnFiling, TallyExport
from sale.models import CreditNote, CreditNoteItem, Invoice, InvoiceItem, Party, PurchaseInvoice, PurchaseInvoiceItem
from stock.models import Category, Item


class GSTR1PayloadTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Org One')
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
        self.sales_user = User.objects.create_user(username='sales', password='password123')
        self.sales_account = UserAccount.objects.create(
            user=self.sales_user,
            organization=self.organization,
            account_type='org_user',
            role='Manager',
        )
        _, self.sales_token = ApiToken.issue_token(self.sales_account)

        category = Category.objects.create(name='Category', organization=self.organization)
        self.item = Item.objects.create(
            organization=self.organization,
            name='Item One',
            sku='ITEM-001',
            category=category,
            current_stock=Decimal('20'),
            unit_price=Decimal('100.00'),
        )

    def _finalized_invoice_with_line(
        self,
        invoice_number,
        party,
        billing_state,
        invoice_type,
        taxable,
        cgst=Decimal('0.00'),
        sgst=Decimal('0.00'),
        igst=Decimal('0.00'),
    ):
        invoice = Invoice.objects.create(
            invoice_number=invoice_number,
            invoice_type=invoice_type,
            billing_state=billing_state,
            business_location=self.warehouse,
            party=party,
            status='Finalized',
        )
        line = InvoiceItem.objects.create(
            invoice=invoice,
            item=self.item,
            quantity=Decimal('1.0000'),
            unit=None,
            rate=taxable,
            discount=Decimal('0.00'),
        )
        InvoiceItem.objects.filter(pk=line.pk).update(
            hsn_code='1001',
            taxable_amount=taxable,
            cgst_rate=Decimal('9.00') if cgst else Decimal('0.00'),
            cgst_amount=cgst,
            sgst_rate=Decimal('9.00') if sgst else Decimal('0.00'),
            sgst_amount=sgst,
            igst_rate=Decimal('18.00') if igst else Decimal('0.00'),
            igst_amount=igst,
            cess_amount=Decimal('0.00'),
            total=taxable + cgst + sgst + igst,
        )
        invoice.calculate_totals()
        invoice.refresh_from_db()
        return invoice

    def _finalized_purchase_with_line(
        self,
        supplier,
        taxable,
        cgst=Decimal('0.00'),
        sgst=Decimal('0.00'),
        igst=Decimal('0.00'),
    ):
        purchase = PurchaseInvoice.objects.create(
            invoice_number='PINV-1',
            supplier=supplier,
            business_location=self.warehouse,
            supplier_invoice_number='SUP-1',
            supplier_invoice_date='2026-05-01',
            invoice_date='2026-05-01',
            status='Finalized',
        )
        line = PurchaseInvoiceItem.objects.create(
            purchase_invoice=purchase,
            item=self.item,
            quantity=Decimal('1.0000'),
            unit=None,
            rate=taxable,
            discount=Decimal('0.00'),
        )
        PurchaseInvoiceItem.objects.filter(pk=line.pk).update(
            hsn_code='1001',
            taxable_amount=taxable,
            cgst_rate=Decimal('9.00') if cgst else Decimal('0.00'),
            cgst_amount=cgst,
            sgst_rate=Decimal('9.00') if sgst else Decimal('0.00'),
            sgst_amount=sgst,
            igst_rate=Decimal('18.00') if igst else Decimal('0.00'),
            igst_amount=igst,
            cess_amount=Decimal('0.00'),
            total=taxable + cgst + sgst + igst,
        )
        purchase.calculate_totals()
        purchase.refresh_from_db()
        return purchase

    def test_gstr1_payload_returns_sectioned_portal_payload(self):
        registered_party = Party.objects.create(
            organization=self.organization,
            name='Registered Buyer',
            party_type='Customer',
            gstin='27BBBBB0000B1Z5',
            state=self.state,
        )
        interstate_state = State.objects.create(name='Gujarat', state_code='24')
        b2b_invoice = self._finalized_invoice_with_line(
            'B2B-1',
            registered_party,
            self.state,
            'Tax Invoice',
            Decimal('100.00'),
            cgst=Decimal('9.00'),
            sgst=Decimal('9.00'),
        )
        self._finalized_invoice_with_line(
            'B2CL-1',
            None,
            interstate_state,
            'Cash',
            Decimal('100000.00'),
            igst=Decimal('18000.00'),
        )
        credit_note = CreditNote.objects.create(
            credit_note_number='CN-1',
            invoice=b2b_invoice,
            party=registered_party,
            amount=Decimal('118.00'),
            reason='Sales return',
        )
        CreditNoteItem.objects.create(
            credit_note=credit_note,
            invoice_item=b2b_invoice.items.first(),
            quantity_returned=Decimal('1.0000'),
            rate=Decimal('100.00'),
            cgst_rate=Decimal('9.00'),
            sgst_rate=Decimal('9.00'),
        )

        response = self.client.get(
            '/v1/gst/reports/gstr1_payload/?gstin=27AAAAA0000A1Z5&return_period=052026',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.data['payload']
        self.assertEqual(payload['gstin'], '27AAAAA0000A1Z5')
        self.assertEqual(payload['fp'], '052026')
        self.assertEqual(payload['b2b'][0]['ctin'], '27BBBBB0000B1Z5')
        self.assertEqual(payload['b2b'][0]['inv'][0]['inum'], 'B2B-1')
        self.assertEqual(payload['b2cl'][0]['pos'], '24')
        self.assertEqual(payload['b2cl'][0]['inv'][0]['inum'], 'B2CL-1')
        self.assertEqual(payload['cdnr'][0]['nt'][0]['nt_num'], 'CN-1')
        self.assertEqual(payload['hsn']['data'][0]['hsn_sc'], '1001')

        invoice_docs = payload['doc_issue']['doc_det'][0]['docs'][0]
        credit_note_docs = payload['doc_issue']['doc_det'][4]['docs'][0]
        self.assertEqual(invoice_docs['totnum'], 2)
        self.assertEqual(credit_note_docs['totnum'], 1)

    def test_gst_liability_report_returns_totals(self):
        response = self.client.get(
            '/v1/gst/reports/gst_liability/',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('net_liability', response.data)
        self.assertIn('sales_tax', response.data)

    def test_gstr3b_payload_returns_summary_sections(self):
        supplier = Party.objects.create(
            organization=self.organization,
            name='Supplier',
            party_type='Supplier',
            gstin='27CCCCC0000C1Z5',
            state=self.state,
        )
        self._finalized_invoice_with_line(
            'B2B-3B',
            None,
            self.state,
            'Cash',
            Decimal('100.00'),
            cgst=Decimal('9.00'),
            sgst=Decimal('9.00'),
        )
        self._finalized_purchase_with_line(
            supplier,
            Decimal('50.00'),
            cgst=Decimal('4.50'),
            sgst=Decimal('4.50'),
        )

        response = self.client.get(
            '/v1/gst/reports/gstr3b_payload/?gstin=27AAAAA0000A1Z5&return_period=052026',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.data['payload']
        self.assertEqual(payload['ret_period'], '052026')
        self.assertEqual(payload['sup_details']['osup_det']['txval'], 100.0)
        self.assertEqual(payload['itc_elg']['itc_net']['camt'], 4.5)

    def test_gstr9_payload_returns_annual_summary(self):
        supplier = Party.objects.create(
            organization=self.organization,
            name='Supplier FY',
            party_type='Supplier',
            gstin='27DDDDD0000D1Z5',
            state=self.state,
        )
        self._finalized_invoice_with_line(
            'FY-1',
            None,
            self.state,
            'Cash',
            Decimal('100.00'),
            cgst=Decimal('9.00'),
            sgst=Decimal('9.00'),
        )
        self._finalized_purchase_with_line(
            supplier,
            Decimal('50.00'),
            cgst=Decimal('4.50'),
            sgst=Decimal('4.50'),
        )

        response = self.client.get(
            '/v1/gst/reports/gstr9_payload/?gstin=27AAAAA0000A1Z5&financial_year=2026-2027',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.data['payload']
        self.assertEqual(payload['financial_year'], '2026-2027')
        self.assertIn('table_10', payload)
        self.assertEqual(payload['table_4']['4B_taxable_b2c']['txval'], 100.0)
        self.assertEqual(payload['table_17'][0]['hsn_sc'], '1001')
        self.assertEqual(payload['table_18'][0]['hsn_sc'], '1001')

    def test_e_invoice_and_e_way_bill_dry_runs_create_records(self):
        party = Party.objects.create(
            organization=self.organization,
            name='Registered Buyer',
            party_type='Customer',
            gstin='27BBBBB0000B1Z5',
            state=self.state,
            address='Buyer address',
        )
        invoice = self._finalized_invoice_with_line(
            'IRN-1',
            party,
            self.state,
            'Tax Invoice',
            Decimal('100.00'),
            cgst=Decimal('9.00'),
            sgst=Decimal('9.00'),
        )

        einvoice_response = self.client.post(
            '/v1/gst/reports/generate_e_invoice/',
            {'invoice': invoice.id},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )
        eway_response = self.client.post(
            '/v1/gst/reports/generate_e_way_bill/',
            {'invoice': invoice.id, 'distance_km': 10, 'vehicle_no': 'MH01AB1234'},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(einvoice_response.status_code, status.HTTP_200_OK)
        self.assertEqual(eway_response.status_code, status.HTTP_200_OK)
        self.assertTrue(GSTEInvoice.objects.filter(invoice=invoice, status='Ready').exists())
        self.assertTrue(GSTEWayBill.objects.filter(invoice=invoice, status='Ready').exists())
        self.assertEqual(einvoice_response.data['payload']['DocDtls']['No'], 'IRN-1')
        self.assertEqual(eway_response.data['payload']['docNo'], 'IRN-1')

    def test_tally_sales_xml_creates_export(self):
        self._finalized_invoice_with_line(
            'TALLY-1',
            None,
            self.state,
            'Cash',
            Decimal('100.00'),
            cgst=Decimal('9.00'),
            sgst=Decimal('9.00'),
        )

        response = self.client.get(
            '/v1/gst/reports/tally_sales_xml/?gstin=27AAAAA0000A1Z5',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('<ENVELOPE>', response.data['content'])
        self.assertIn('<VOUCHERNUMBER>TALLY-1</VOUCHERNUMBER>', response.data['content'])
        self.assertEqual(TallyExport.objects.count(), 1)

    def test_save_gstr3b_sandbox_dry_run_records_return(self):
        self._finalized_invoice_with_line(
            'DRY-3B',
            None,
            self.state,
            'Cash',
            Decimal('100.00'),
            cgst=Decimal('9.00'),
            sgst=Decimal('9.00'),
        )

        response = self.client.post(
            '/v1/gst/reports/save_gstr3b_sandbox/?gstin=27AAAAA0000A1Z5&return_period=052026',
            {},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(GSTReturnFiling.objects.filter(return_type='GSTR3B', period='052026', status='Ready').exists())

    def test_save_gstr9_sandbox_dry_run_records_return(self):
        self._finalized_invoice_with_line(
            'DRY-9',
            None,
            self.state,
            'Cash',
            Decimal('100.00'),
            cgst=Decimal('9.00'),
            sgst=Decimal('9.00'),
        )

        response = self.client.post(
            '/v1/gst/reports/save_gstr9_sandbox/',
            {'gstin': '27AAAAA0000A1Z5', 'financial_year': '2026-2027'},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(GSTReturnFiling.objects.filter(return_type='GSTR9', period='2026-2027', status='Ready').exists())
