"""
Accounting posting services.

This module converts operational documents from sale and pos into immutable
double-entry journal entries. The helpers here are intentionally narrow:

1. create default chart-of-accounts rows
2. post balanced journal entries from source documents
3. reverse previously posted entries when documents are cancelled
4. provide source-specific posting helpers used by model hooks and backfill

The services are idempotent at the posting-batch level so repeated callbacks do
not create duplicate journals.
"""

from decimal import Decimal

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import FiscalPeriod, JournalEntry, JournalLine, LedgerAccount, PostingBatch


DEFAULT_ACCOUNTS = [
    ('1000', 'Cash on Hand', 'Asset', 'Debit'),
    ('1010', 'Bank Account', 'Asset', 'Debit'),
    ('1020', 'Bank Clearing', 'Asset', 'Debit'),
    ('1100', 'Accounts Receivable', 'Asset', 'Debit'),
    ('1200', 'Inventory', 'Asset', 'Debit'),
    ('1300', 'Input GST Receivable', 'Asset', 'Debit'),
    ('2000', 'Accounts Payable', 'Liability', 'Credit'),
    ('2100', 'Output GST Payable', 'Liability', 'Credit'),
    ('2110', 'TCS Payable', 'Liability', 'Credit'),
    ('3000', 'Owner Equity', 'Equity', 'Credit'),
    ('4000', 'Sales Revenue', 'Revenue', 'Credit'),
    ('4100', 'Sales Returns', 'Revenue', 'Debit'),
    ('4200', 'Purchase Returns', 'Expense', 'Credit'),
    ('5000', 'Cost of Goods Sold', 'Expense', 'Debit'),
    ('5100', 'Cash Over/Short', 'Expense', 'Debit'),
    ('6000', 'Operating Expenses', 'Expense', 'Debit'),
]


def ensure_default_accounts(organization, created_by=None):
    """Create the default chart of accounts rows for an organization."""
    accounts = {}
    for code, name, category, normal_balance in DEFAULT_ACCOUNTS:
        account, _ = LedgerAccount.objects.get_or_create(
            organization=organization,
            code=code,
            defaults={
                'name': name,
                'category': category,
                'normal_balance': normal_balance,
                'is_control': code in {'1100', '1200', '1300', '2000', '2100'},
            },
        )
        accounts[code] = account
    return accounts


def get_account(organization, code, name, category, normal_balance, is_control=False):
    """Fetch or create a single ledger account definition."""
    account, _ = LedgerAccount.objects.get_or_create(
        organization=organization,
        code=code,
        defaults={
            'name': name,
            'category': category,
            'normal_balance': normal_balance,
            'is_control': is_control,
        },
    )
    return account


def _validate_period_open(organization, entry_date):
    """Reject postings into a closed fiscal period."""
    closed = FiscalPeriod.objects.filter(
        organization=organization,
        start_date__lte=entry_date,
        end_date__gte=entry_date,
        is_closed=True,
    ).exists()
    if closed:
        raise ValidationError('Accounting period is closed for this date.')


def post_journal(
    *,
    organization,
    source_app,
    source_model,
    source_object_id,
    source_event,
    source_reference='',
    entry_date,
    narration,
    lines,
    created_by=None,
    metadata=None,
    entry_kind='Normal',
    reversal_of=None,
):
    """
    Create a balanced journal entry from a list of debit and credit lines.

    The function also creates the posting batch that ties the journal back to
    the source document and guarantees idempotency for the source event.
    """
    _validate_period_open(organization, entry_date)
    ensure_default_accounts(organization, created_by=created_by)

    with transaction.atomic():
        batch, created = PostingBatch.objects.get_or_create(
            organization=organization,
            source_app=source_app,
            source_model=source_model,
            source_object_id=str(source_object_id),
            source_event=source_event,
            defaults={
                'source_reference': source_reference or '',
                'memo': narration,
                'batch_date': entry_date,
                'metadata': metadata or {},
                'posted_by': created_by,
            },
        )
        if not created and hasattr(batch, 'journal_entry'):
            return batch.journal_entry

        if not created:
            batch.source_reference = source_reference or batch.source_reference
            batch.memo = narration
            batch.batch_date = entry_date
            batch.metadata = metadata or batch.metadata
            batch.posted_by = created_by
            batch.save(update_fields=['source_reference', 'memo', 'batch_date', 'metadata', 'posted_by'])

        entry = JournalEntry.objects.create(
            batch=batch,
            organization=organization,
            entry_date=entry_date,
            entry_kind=entry_kind,
            narration=narration,
            reference=source_reference or '',
            created_by=created_by,
            reversed_entry=reversal_of,
        )

        total_debit = Decimal('0')
        total_credit = Decimal('0')
        for index, line in enumerate(lines, start=1):
            debit = Decimal(line.get('debit', '0') or '0')
            credit = Decimal(line.get('credit', '0') or '0')
            if debit < 0 or credit < 0:
                raise ValidationError('Journal line amounts must be non-negative.')
            if debit and credit:
                raise ValidationError('Journal line cannot contain both debit and credit.')
            if not debit and not credit:
                raise ValidationError('Journal line must contain a debit or credit.')
            JournalLine.objects.create(
                entry=entry,
                line_no=index,
                account=line['account'],
                party=line.get('party'),
                debit=debit,
                credit=credit,
                memo=line.get('memo', ''),
            )
            total_debit += debit
            total_credit += credit

        if total_debit != total_credit:
            raise ValidationError('Journal entry must balance before posting.')

        return entry


def reverse_entry(*, original_entry, source_event, source_reference, entry_date, created_by=None, memo='Reversal'):
    """Create a reversing journal entry for an existing posted entry."""
    reversed_lines = []
    for line in original_entry.lines.all():
        reversed_lines.append({
            'account': line.account,
            'party': line.party,
            'debit': line.credit,
            'credit': line.debit,
            'memo': line.memo,
        })
    return post_journal(
        organization=original_entry.organization,
        source_app=original_entry.batch.source_app,
        source_model=original_entry.batch.source_model,
        source_object_id=original_entry.batch.source_object_id,
        source_event=source_event,
        source_reference=source_reference,
        entry_date=entry_date,
        narration=memo,
        lines=reversed_lines,
        created_by=created_by,
        entry_kind='Reversal',
        reversal_of=original_entry,
    )


def _money(value):
    """Convert a value to a Decimal money amount."""
    return Decimal(value or '0')


def post_sale_invoice(invoice):
    """Post a sales invoice into receivables, revenue, and tax accounts."""
    from sale.models import Invoice
    if not isinstance(invoice, Invoice):
        raise TypeError('invoice must be a sale.Invoice instance')
    accounts = ensure_default_accounts(invoice.business_location.organization)
    debit_account = accounts['1000'] if invoice.invoice_type == 'Cash' else accounts['1100']
    gst_total = _money(invoice.cgst_amount) + _money(invoice.sgst_amount) + _money(invoice.igst_amount)
    tcs_amount = _money(invoice.tcs_amount)
    total_cost = _money(sum((item.cost_basis for item in invoice.items.all()), Decimal('0')))
    lines = [
        {'account': debit_account, 'party': invoice.party, 'debit': invoice.grand_total},
        {'account': accounts['4000'], 'party': invoice.party, 'credit': invoice.taxable_amount},
    ]
    if gst_total:
        lines.append({'account': accounts['2100'], 'party': invoice.party, 'credit': gst_total})
    if tcs_amount:
        lines.append({'account': accounts['2110'], 'party': invoice.party, 'credit': tcs_amount})
    if total_cost:
        lines.append({'account': accounts['5000'], 'party': invoice.party, 'debit': total_cost})
        lines.append({'account': accounts['1200'], 'party': invoice.party, 'credit': total_cost})
    if invoice.round_off:
        round_off = _money(invoice.round_off)
        if round_off > 0:
            lines.append({'account': accounts['5100'], 'credit': round_off})
        elif round_off < 0:
            lines.append({'account': accounts['5100'], 'debit': abs(round_off)})
    return post_journal(
        organization=invoice.business_location.organization,
        source_app='sale',
        source_model='Invoice',
        source_object_id=invoice.pk,
        source_event='invoice.finalize',
        source_reference=invoice.invoice_number,
        entry_date=invoice.invoice_date.date(),
        narration=f'Sales invoice {invoice.invoice_number}',
        lines=lines,
        created_by=invoice.created_by,
        metadata={'invoice_type': invoice.invoice_type},
    )


def post_receipt(receipt):
    """Post a customer receipt against cash or bank and receivables."""
    accounts = ensure_default_accounts(receipt.business_location.organization)
    if receipt.payment_mode == 'Cash':
        asset_account = accounts['1000']
    elif receipt.payment_mode == 'Bank Transfer':
        asset_account = accounts['1010']
    else:
        asset_account = accounts['1020']
    return post_journal(
        organization=receipt.business_location.organization,
        source_app='sale',
        source_model='Receipt',
        source_object_id=receipt.pk,
        source_event='receipt.create',
        source_reference=receipt.receipt_number,
        entry_date=receipt.transaction_date.date(),
        narration=f'Receipt {receipt.receipt_number}',
        lines=[
            {'account': asset_account, 'debit': receipt.amount},
            {'account': accounts['1100'], 'party': receipt.party, 'credit': receipt.amount},
        ],
        created_by=receipt.received_by,
        metadata={'payment_mode': receipt.payment_mode},
    )


def post_purchase_invoice(purchase_invoice):
    """Post a supplier bill into inventory, input tax, and payables."""
    accounts = ensure_default_accounts(purchase_invoice.business_location.organization)
    gst_total = _money(purchase_invoice.cgst_amount) + _money(purchase_invoice.sgst_amount) + _money(purchase_invoice.igst_amount)
    lines = [
        {'account': accounts['1200'], 'debit': purchase_invoice.taxable_amount},
        {'account': accounts['1300'], 'debit': gst_total} if gst_total else None,
        {'account': accounts['2000'], 'party': purchase_invoice.supplier, 'credit': purchase_invoice.grand_total},
    ]
    lines = [line for line in lines if line is not None]
    return post_journal(
        organization=purchase_invoice.business_location.organization,
        source_app='sale',
        source_model='PurchaseInvoice',
        source_object_id=purchase_invoice.pk,
        source_event='purchase_invoice.finalize',
        source_reference=purchase_invoice.invoice_number,
        entry_date=purchase_invoice.invoice_date,
        narration=f'Purchase invoice {purchase_invoice.invoice_number}',
        lines=lines,
        created_by=purchase_invoice.created_by,
        metadata={'supplier_invoice_number': purchase_invoice.supplier_invoice_number},
    )


def post_purchase_invoice_cancellation(purchase_invoice, user=None):
    """Reverse a posted purchase invoice after cancellation."""
    original_entry = purchase_invoice.business_location.organization.journal_entries.filter(
        batch__source_app='sale',
        batch__source_model='PurchaseInvoice',
        batch__source_object_id=str(purchase_invoice.pk),
        batch__source_event='purchase_invoice.finalize',
    ).first()
    if original_entry is None:
        return None
    return reverse_entry(
        original_entry=original_entry,
        source_event='purchase_invoice.cancel',
        source_reference=f'CN-{purchase_invoice.invoice_number}',
        entry_date=timezone.localdate(),
        created_by=user,
        memo=f'Cancel purchase invoice {purchase_invoice.invoice_number}',
    )


def post_credit_note(credit_note):
    """Post a customer credit note as a sales return."""
    accounts = ensure_default_accounts(credit_note.party.organization)
    return post_journal(
        organization=credit_note.party.organization,
        source_app='sale',
        source_model='CreditNote',
        source_object_id=credit_note.pk,
        source_event='credit_note.create',
        source_reference=credit_note.credit_note_number,
        entry_date=credit_note.created_at.date(),
        narration=f'Credit note {credit_note.credit_note_number}',
        lines=[
            {'account': accounts['4100'], 'debit': credit_note.amount},
            {'account': accounts['1100'], 'party': credit_note.party, 'credit': credit_note.amount},
        ],
        created_by=credit_note.created_by,
    )


def post_debit_note(debit_note):
    """Post a supplier debit note as a purchase return."""
    accounts = ensure_default_accounts(debit_note.supplier.organization)
    return post_journal(
        organization=debit_note.supplier.organization,
        source_app='sale',
        source_model='DebitNote',
        source_object_id=debit_note.pk,
        source_event='debit_note.create',
        source_reference=debit_note.debit_note_number,
        entry_date=debit_note.created_at.date(),
        narration=f'Debit note {debit_note.debit_note_number}',
        lines=[
            {'account': accounts['2000'], 'party': debit_note.supplier, 'debit': debit_note.amount},
            {'account': accounts['4200'], 'credit': debit_note.amount},
        ],
        created_by=debit_note.created_by,
    )


def post_payment_out(payment_out):
    """Post a payment to a supplier against cash or bank balances."""
    accounts = ensure_default_accounts(payment_out.business_location.organization)
    if payment_out.payment_mode == 'Cash':
        asset_account = accounts['1000']
    elif payment_out.payment_mode == 'Bank Transfer':
        asset_account = accounts['1010']
    else:
        asset_account = accounts['1020']
    return post_journal(
        organization=payment_out.business_location.organization,
        source_app='sale',
        source_model='PaymentOut',
        source_object_id=payment_out.pk,
        source_event='payment_out.create',
        source_reference=payment_out.payment_number,
        entry_date=payment_out.transaction_date.date(),
        narration=f'Payment out {payment_out.payment_number}',
        lines=[
            {'account': accounts['2000'], 'party': payment_out.supplier, 'debit': payment_out.amount},
            {'account': asset_account, 'party': payment_out.supplier, 'credit': payment_out.amount},
        ],
        created_by=payment_out.paid_by,
    )


def post_cash_transaction(shift, cash_transaction):
    """Post a POS cash movement inside the active shift."""
    accounts = ensure_default_accounts(shift.warehouse.organization)
    if cash_transaction.transaction_type == 'CashIn':
        lines = [
            {'account': accounts['1000'], 'debit': cash_transaction.amount},
            {'account': accounts['5100'], 'credit': cash_transaction.amount},
        ]
    else:
        lines = [
            {'account': accounts['5100'], 'debit': cash_transaction.amount},
            {'account': accounts['1000'], 'credit': cash_transaction.amount},
        ]
    return post_journal(
        organization=shift.warehouse.organization,
        source_app='pos',
        source_model='CashTransaction',
        source_object_id=cash_transaction.pk,
        source_event='cash_transaction.create',
        source_reference=shift.shift_number,
        entry_date=cash_transaction.created_at.date(),
        narration=f'Cash transaction {cash_transaction.id}',
        lines=lines,
        created_by=cash_transaction.created_by,
    )


def post_shift_close(shift):
    """Post the cash variance created when a POS shift is closed."""
    accounts = ensure_default_accounts(shift.warehouse.organization)
    variance = _money(shift.variance)
    if not variance:
        return None
    if variance > 0:
        lines = [
            {'account': accounts['1000'], 'debit': variance},
            {'account': accounts['5100'], 'credit': variance},
        ]
    else:
        lines = [
            {'account': accounts['5100'], 'debit': abs(variance)},
            {'account': accounts['1000'], 'credit': abs(variance)},
        ]
    return post_journal(
        organization=shift.warehouse.organization,
        source_app='pos',
        source_model='Shift',
        source_object_id=shift.pk,
        source_event='shift.close',
        source_reference=shift.shift_number,
        entry_date=shift.closing_time.date() if shift.closing_time else timezone.localdate(),
        narration=f'Shift close {shift.shift_number}',
        lines=lines,
        created_by=shift.user,
    )


def post_expense(expense):
    """Post an expense record into operating expense and cash/bank/payable accounts."""
    accounts = ensure_default_accounts(expense.organization)
    if expense.payment_mode == 'Cash':
        credit_account = accounts['1000']
    elif expense.payment_mode == 'Bank Transfer':
        credit_account = accounts['1010']
    elif expense.payment_mode in {'Card', 'UPI'}:
        credit_account = accounts['1020']
    else:
        credit_account = accounts['2000']
    total = _money(expense.amount) + _money(expense.tax_amount)
    lines = [
        {'account': accounts['6000'], 'debit': expense.amount, 'party': None},
    ]
    if expense.tax_amount:
        lines.append({'account': accounts['1300'], 'debit': expense.tax_amount})
    payable_party = expense.party if expense.payment_mode == 'Credit' else None
    lines.append({'account': credit_account, 'credit': total, 'party': payable_party})
    entry = post_journal(
        organization=expense.organization,
        source_app='accounting',
        source_model='Expense',
        source_object_id=expense.pk,
        source_event='expense.create',
        source_reference=expense.expense_number,
        entry_date=expense.expense_date,
        narration=f'Expense {expense.expense_number}',
        lines=lines,
        created_by=expense.created_by,
        metadata={'category': expense.category, 'payment_mode': expense.payment_mode},
    )
    expense.journal_entry = entry
    expense.save(update_fields=['journal_entry', 'updated_at'])
    return entry


def reverse_expense(expense, user=None):
    """Reverse a posted expense entry."""
    if not expense.journal_entry_id:
        return None
    return reverse_entry(
        original_entry=expense.journal_entry,
        source_event='expense.cancel',
        source_reference=f'REV-{expense.expense_number}',
        entry_date=timezone.localdate(),
        created_by=user,
        memo=f'Cancel expense {expense.expense_number}',
    )


def post_invoice_cancellation(invoice, user=None):
    original_entry = invoice.business_location.organization.journal_entries.filter(
        batch__source_app='sale',
        batch__source_model='Invoice',
        batch__source_object_id=str(invoice.pk),
        batch__source_event='invoice.finalize',
    ).first()
    if original_entry is None:
        return None
    return reverse_entry(
        original_entry=original_entry,
        source_event='invoice.cancel',
        source_reference=f'CN-{invoice.invoice_number}',
        entry_date=timezone.localdate(),
        created_by=user,
        memo=f'Cancel invoice {invoice.invoice_number}',
    )
