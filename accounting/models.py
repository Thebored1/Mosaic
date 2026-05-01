"""
Accounting domain models.

This module defines the double-entry layer that sits underneath sales and POS
transactions:

1. ledger accounts for the chart of accounts
2. posting batches and immutable journal entries
3. fiscal periods for close and lock behavior
4. reconciliation records for bank and cash matching

The accounting app is intentionally source-of-truth neutral. Operational
documents in sale and pos generate postings here, but once written the journal
records are immutable and corrections happen through reversal entries.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class LedgerAccount(models.Model):
    """
    Chart of accounts record scoped to a single organization.

    Ledger accounts represent the financial buckets used by journal entries
    and reporting. The code field is the stable business identifier while the
    category and normal balance determine how balances are interpreted.
    """
    CATEGORY_CHOICES = [
        ('Asset', 'Asset'),
        ('Liability', 'Liability'),
        ('Equity', 'Equity'),
        ('Revenue', 'Revenue'),
        ('Expense', 'Expense'),
    ]
    BALANCE_CHOICES = [
        ('Debit', 'Debit'),
        ('Credit', 'Credit'),
    ]

    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='ledger_accounts')
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    normal_balance = models.CharField(max_length=10, choices=BALANCE_CHOICES)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children')
    is_control = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['code', 'name']
        unique_together = [('organization', 'code')]

    def __str__(self):
        """Return the account code and human-readable account name."""
        return f"{self.code} - {self.name}"


class FiscalPeriod(models.Model):
    """
    Open or closed accounting period for a single organization.

    Fiscal periods are used to prevent back-dated posting into locked ranges
    and to support close controls for reporting.
    """
    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='fiscal_periods')
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    is_closed = models.BooleanField(default=False)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='closed_fiscal_periods')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_date']
        unique_together = [('organization', 'start_date', 'end_date')]

    def clean(self):
        """Validate that the closing date is not earlier than the opening date."""
        super().clean()
        if self.end_date < self.start_date:
            raise ValidationError({'end_date': 'End date cannot be before start date.'})

    def close(self, user=None):
        """
        Mark the period as closed and record who closed it.

        Closing a period blocks further ledger postings into the date range
        covered by this record.
        """
        self.is_closed = True
        self.closed_at = timezone.now()
        self.closed_by = user
        self.save(update_fields=['is_closed', 'closed_at', 'closed_by', 'updated_at'])


class PostingBatch(models.Model):
    """
    Group a set of journal lines produced by one operational event.

    Each source document maps to exactly one posting batch so the accounting
    layer can remain idempotent and traceable back to the originating record.
    """
    STATUS_CHOICES = [
        ('Posted', 'Posted'),
        ('Reversed', 'Reversed'),
    ]

    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='posting_batches')
    source_app = models.CharField(max_length=50)
    source_model = models.CharField(max_length=100)
    source_object_id = models.CharField(max_length=64)
    source_event = models.CharField(max_length=50)
    source_reference = models.CharField(max_length=100, blank=True)
    memo = models.CharField(max_length=255, blank=True)
    batch_date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Posted')
    metadata = models.JSONField(default=dict, blank=True)
    reversal_of = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='reversals')
    posted_at = models.DateTimeField(auto_now_add=True)
    posted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='posting_batches')

    class Meta:
        ordering = ['-posted_at', '-id']
        unique_together = [('organization', 'source_app', 'source_model', 'source_object_id', 'source_event')]


class JournalEntry(models.Model):
    """
    Immutable journal header for a balanced accounting posting.

    The journal entry is the audit-friendly header record. All financial
    movement lives in JournalLine rows underneath it and the entry itself is
    not edited after posting.
    """
    ENTRY_KIND_CHOICES = [
        ('Normal', 'Normal'),
        ('Reversal', 'Reversal'),
        ('Opening', 'Opening'),
    ]

    batch = models.OneToOneField(PostingBatch, on_delete=models.CASCADE, related_name='journal_entry')
    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='journal_entries')
    entry_date = models.DateField(default=timezone.localdate)
    entry_kind = models.CharField(max_length=20, choices=ENTRY_KIND_CHOICES, default='Normal')
    narration = models.CharField(max_length=255)
    reference = models.CharField(max_length=100, blank=True)
    reversed_entry = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='reversal_entries')
    reversal_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_entries')

    class Meta:
        ordering = ['-entry_date', '-id']

    def save(self, *args, **kwargs):
        """Prevent updates after the entry has been created."""
        if self.pk:
            raise ValidationError('Journal entries are immutable once posted.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Prevent deletion so the audit trail remains intact."""
        raise ValidationError('Journal entries cannot be deleted.')

    @property
    def total_debit(self):
        """Return the summed debit amount across all lines."""
        return sum((line.debit for line in self.lines.all()), Decimal('0'))

    @property
    def total_credit(self):
        """Return the summed credit amount across all lines."""
        return sum((line.credit for line in self.lines.all()), Decimal('0'))


class JournalLine(models.Model):
    """
    Single debit or credit line inside a journal entry.

    Lines point to the account being affected and optionally to a party for
    subledger-style reporting such as receivables and payables.
    """
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name='lines')
    line_no = models.PositiveIntegerField(default=1)
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name='journal_lines')
    party = models.ForeignKey('sale.Party', on_delete=models.SET_NULL, null=True, blank=True, related_name='journal_lines')
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    memo = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['line_no', 'id']
        constraints = [
            models.CheckConstraint(
                condition=Q(debit__gte=0) & Q(credit__gte=0),
                name='journal_line_non_negative',
            ),
            models.CheckConstraint(
                condition=~(Q(debit__gt=0) & Q(credit__gt=0)),
                name='journal_line_single_sided',
            ),
        ]

    def save(self, *args, **kwargs):
        """Prevent line edits after the posting batch has been created."""
        if self.pk:
            raise ValidationError('Journal lines are immutable once posted.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Prevent line deletion so posted journals remain auditable."""
        raise ValidationError('Journal lines cannot be deleted.')


class Expense(models.Model):
    """
    Expense record posted through the accounting layer.

    Expenses capture operational spend such as rent, freight, utilities, and
    other overheads. Each expense is posted into the ledger and can be tracked
    by category and date.
    """
    PAYMENT_MODE_CHOICES = [
        ('Cash', 'Cash'),
        ('Card', 'Card'),
        ('UPI', 'UPI'),
        ('Bank Transfer', 'Bank Transfer'),
        ('Credit', 'Credit'),
    ]

    STATUS_CHOICES = [
        ('Posted', 'Posted'),
        ('Cancelled', 'Cancelled'),
    ]

    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='expenses')
    business_location = models.ForeignKey('configuration.Warehouse', on_delete=models.PROTECT, related_name='expenses')
    party = models.ForeignKey('account.Merchant', on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses')
    expense_number = models.CharField(max_length=30, unique=True)
    category = models.CharField(max_length=100)
    expense_date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    payment_mode = models.CharField(max_length=20, choices=PAYMENT_MODE_CHOICES, default='Cash')
    reference_number = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Posted')
    journal_entry = models.OneToOneField(
        JournalEntry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expense',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='expenses_created')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-expense_date', '-id']

    def __str__(self):
        """Return the expense number and category."""
        return f"{self.expense_number} - {self.category}"

    def save(self, *args, **kwargs):
        """Generate the expense number and post the journal on create."""
        created = self.pk is None
        update_fields = kwargs.get('update_fields')
        if self.pk and update_fields is None:
            raise ValidationError('Expense records are immutable once posted.')
        if self.pk and update_fields is not None:
            allowed_updates = {'status', 'journal_entry', 'updated_at'}
            if not set(update_fields).issubset(allowed_updates):
                raise ValidationError('Expense records are immutable once posted.')
        if not self.expense_number:
            prefix = f"EX{timezone.now().strftime('%Y%m%d')}"
            last = Expense.objects.filter(expense_number__startswith=prefix).order_by('-expense_number').first()
            if last:
                try:
                    num = int(last.expense_number[-4:])
                    self.expense_number = f"{prefix}{num + 1:04d}"
                except ValueError:
                    self.expense_number = f"{prefix}0001"
            else:
                self.expense_number = f"{prefix}0001"
        super().save(*args, **kwargs)
        if created and self.status == 'Posted':
            from accounting.services import post_expense
            post_expense(self)

    def cancel(self, user=None):
        """Mark the expense cancelled and reverse the posted journal entry."""
        if self.status == 'Cancelled':
            return
        self.status = 'Cancelled'
        self.save(update_fields=['status', 'updated_at'])
        if self.journal_entry_id:
            from accounting.services import reverse_expense
            reverse_expense(self, user=user)


class Reconciliation(models.Model):
    """
    Reconciliation record for bank or cash matching.

    Reconciliations compare the system balance against an external statement
    balance and store the resulting variance for review.
    """
    STATUS_CHOICES = [
        ('Open', 'Open'),
        ('Matched', 'Matched'),
        ('Closed', 'Closed'),
    ]

    organization = models.ForeignKey('account.Organization', on_delete=models.CASCADE, related_name='reconciliations')
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name='reconciliations')
    statement_date = models.DateField()
    statement_balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    system_balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    variance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Open')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reconciliations')

    class Meta:
        ordering = ['-statement_date', '-id']
