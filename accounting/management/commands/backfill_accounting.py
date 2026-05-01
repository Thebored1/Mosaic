from django.core.management.base import BaseCommand

from account.models import Organization
from accounting.models import JournalEntry
from accounting.services import (
    post_credit_note,
    post_debit_note,
    post_invoice_cancellation,
    post_payment_out,
    post_purchase_invoice,
    post_purchase_invoice_cancellation,
    post_receipt,
    post_sale_invoice,
    post_shift_close,
)
from pos.models import CashTransaction, Shift
from sale.models import CreditNote, DebitNote, Invoice, PaymentOut, PurchaseInvoice, Receipt


class Command(BaseCommand):
    help = 'Backfill accounting journal entries from existing sales and POS documents.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', type=int, help='Only backfill one organization id.')
        parser.add_argument('--dry-run', action='store_true', help='Print what would be posted without creating entries.')

    def handle(self, *args, **options):
        org_id = options.get('organization')
        dry_run = options.get('dry_run', False)
        organizations = Organization.objects.all()
        if org_id:
            organizations = organizations.filter(pk=org_id)

        created = 0
        for organization in organizations:
            for invoice in Invoice.objects.filter(business_location__organization=organization, status='Finalized'):
                if not JournalEntry.objects.filter(batch__source_app='sale', batch__source_model='Invoice', batch__source_object_id=str(invoice.pk), batch__source_event='invoice.finalize').exists():
                    created += 1
                    if not dry_run:
                        post_sale_invoice(invoice)

            for invoice in Invoice.objects.filter(business_location__organization=organization, status='Cancelled'):
                if not JournalEntry.objects.filter(batch__source_app='sale', batch__source_model='Invoice', batch__source_object_id=str(invoice.pk), batch__source_event='invoice.cancel').exists():
                    created += 1
                    if not dry_run:
                        post_invoice_cancellation(invoice)

            for receipt in Receipt.objects.filter(business_location__organization=organization):
                if not JournalEntry.objects.filter(batch__source_app='sale', batch__source_model='Receipt', batch__source_object_id=str(receipt.pk), batch__source_event='receipt.create').exists():
                    created += 1
                    if not dry_run:
                        post_receipt(receipt)

            for pi in PurchaseInvoice.objects.filter(business_location__organization=organization, status='Finalized'):
                if not JournalEntry.objects.filter(batch__source_app='sale', batch__source_model='PurchaseInvoice', batch__source_object_id=str(pi.pk), batch__source_event='purchase_invoice.finalize').exists():
                    created += 1
                    if not dry_run:
                        post_purchase_invoice(pi)

            for pi in PurchaseInvoice.objects.filter(business_location__organization=organization, status='Cancelled'):
                if not JournalEntry.objects.filter(batch__source_app='sale', batch__source_model='PurchaseInvoice', batch__source_object_id=str(pi.pk), batch__source_event='purchase_invoice.cancel').exists():
                    created += 1
                    if not dry_run:
                        post_purchase_invoice_cancellation(pi)

            for credit_note in CreditNote.objects.filter(party__organization=organization):
                if credit_note.amount and not JournalEntry.objects.filter(batch__source_app='sale', batch__source_model='CreditNote', batch__source_object_id=str(credit_note.pk), batch__source_event='credit_note.create').exists():
                    created += 1
                    if not dry_run:
                        post_credit_note(credit_note)

            for debit_note in DebitNote.objects.filter(supplier__organization=organization):
                if debit_note.amount and not JournalEntry.objects.filter(batch__source_app='sale', batch__source_model='DebitNote', batch__source_object_id=str(debit_note.pk), batch__source_event='debit_note.create').exists():
                    created += 1
                    if not dry_run:
                        post_debit_note(debit_note)

            for payment in PaymentOut.objects.filter(business_location__organization=organization):
                if not JournalEntry.objects.filter(batch__source_app='sale', batch__source_model='PaymentOut', batch__source_object_id=str(payment.pk), batch__source_event='payment_out.create').exists():
                    created += 1
                    if not dry_run:
                        post_payment_out(payment)

            for shift in Shift.objects.filter(warehouse__organization=organization, status='Closed'):
                if shift.variance and not JournalEntry.objects.filter(batch__source_app='pos', batch__source_model='Shift', batch__source_object_id=str(shift.pk), batch__source_event='shift.close').exists():
                    created += 1
                    if not dry_run:
                        post_shift_close(shift)

            for tx in CashTransaction.objects.filter(shift__warehouse__organization=organization):
                if not JournalEntry.objects.filter(batch__source_app='pos', batch__source_model='CashTransaction', batch__source_object_id=str(tx.pk), batch__source_event='cash_transaction.create').exists():
                    created += 1
                    if not dry_run:
                        from accounting.services import post_cash_transaction
                        post_cash_transaction(tx.shift, tx)

        self.stdout.write(self.style.SUCCESS(f"{'Would create' if dry_run else 'Created'} {created} journal entries."))
