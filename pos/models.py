"""
POS domain models.

This module holds the operational shift ledger for the point-of-sale surface:

1. Shift tracks a cashier session from opening cash to closing variance.
2. CashTransaction records cash in/out movements during the shift.

POS is deliberately separate from storefront commerce because it reflects the
in-person billing and cash reconciliation workflow of a physical counter.
"""

from decimal import Decimal
from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from configuration.models import Warehouse


class Shift(models.Model):
    """
    Cashier or counter shift session.

    A shift represents the lifecycle of a single cashier session. It captures
    opening and closing cash, computes variance, and anchors all in-shift cash
    transactions and sales reporting.
    """
    STATUS_CHOICES = [
        ('Open', 'Open'),
        ('Closed', 'Closed'),
    ]

    shift_number = models.CharField(
        max_length=20,
        unique=True,
        help_text="Auto-generated shift number"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='shifts',
        help_text="Cashier/user for this shift"
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name='shifts',
        help_text="Warehouse/location for this shift"
    )
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default='Open'
    )
    opening_cash = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Cash in drawer at shift start"
    )
    closing_cash = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Cash in drawer at shift end"
    )
    expected_cash = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Expected cash based on transactions"
    )
    variance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Difference between closing and expected cash"
    )
    opening_time = models.DateTimeField(
        auto_now_add=True,
        help_text="When shift was opened"
    )
    closing_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When shift was closed"
    )
    notes = models.TextField(
        blank=True,
        help_text="Notes for this shift"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Shift'
        verbose_name_plural = 'Shifts'
        ordering = ['-opening_time']

    def __str__(self):
        """Return the shift number, cashier, and status."""
        return f"{self.shift_number} - {self.user.username} - {self.status}"

    def save(self, *args, **kwargs):
        if not self.shift_number:
            self.shift_number = self.generate_shift_number()
        super().save(*args, **kwargs)

    def generate_shift_number(self):
        """Generate a unique shift number for the current date."""
        today = timezone.now().date()
        prefix = f"SH{today.strftime('%Y%m%d')}"
        last_shift = Shift.objects.filter(
            shift_number__startswith=prefix
        ).order_by('-shift_number').first()
        
        if last_shift:
            try:
                last_num = int(last_shift.shift_number[-4:])
                return f"{prefix}{last_num + 1:04d}"
            except ValueError:
                pass
        return f"{prefix}0001"

    def close(self, closing_cash_amount):
        """
        Close the shift and calculate variance.

        Closing a shift freezes the session, computes expected cash from cash
        transactions, and stores the variance for reconciliation.
        """
        from django.db.models import Sum
        
        if self.status == 'Closed':
            raise ValidationError("Shift is already closed")
        
        self.closing_cash = closing_cash_amount
        
        cash_in = self.transactions.filter(
            transaction_type='CashIn'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        
        cash_out = self.transactions.filter(
            transaction_type='CashOut'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        
        self.expected_cash = self.opening_cash + cash_in - cash_out
        self.variance = self.closing_cash - self.expected_cash
        
        self.status = 'Closed'
        self.closing_time = timezone.now()
        
        self.save(update_fields=[
            'closing_cash', 'expected_cash', 'variance',
            'status', 'closing_time'
        ])
        
        return {
            'shift_number': self.shift_number,
            'closing_cash': self.closing_cash,
            'expected_cash': self.expected_cash,
            'variance': self.variance,
            'status': self.status
        }

    @property
    def sales_total(self):
        """Calculate total sales during this shift."""
        from django.db.models import Sum
        from sale.models import Invoice
        
        return Invoice.objects.filter(
            warehouse=self.warehouse,
            is_finalized=True,
            is_cancelled=False,
            created_by=self.user,
            invoice_date__gte=self.opening_time,
            invoice_date__lte=(self.closing_time or timezone.now())
        ).aggregate(total=Sum('grand_total'))['total'] or Decimal('0')

    @property
    def transaction_summary(self):
        """Return a cash-in / cash-out summary for the shift."""
        from django.db.models import Sum
        
        cash_in = self.transactions.filter(
            transaction_type='CashIn'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        
        cash_out = self.transactions.filter(
            transaction_type='CashOut'
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
        
        return {
            'cash_in': cash_in,
            'cash_out': cash_out,
            'net': cash_in - cash_out
        }


class CashTransaction(models.Model):
    """
    Cash in/out transaction recorded during a shift.

    Transactions capture the movement of physical cash while a shift is open
    so reconciliation can explain why closing cash differs from opening cash.
    """
    TRANSACTION_TYPE_CHOICES = [
        ('CashIn', 'Cash In'),
        ('CashOut', 'Cash Out'),
    ]

    shift = models.ForeignKey(
        Shift,
        on_delete=models.CASCADE,
        related_name='transactions',
        help_text="Shift this transaction belongs to"
    )
    transaction_type = models.CharField(
        max_length=10,
        choices=TRANSACTION_TYPE_CHOICES,
        help_text="Type of cash transaction"
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Transaction amount"
    )
    reason = models.CharField(
        max_length=200,
        help_text="Reason for transaction"
    )
    reference = models.CharField(
        max_length=50,
        blank=True,
        help_text="Reference number (e.g., bank receipt)"
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='cash_transactions'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Cash Transaction'
        verbose_name_plural = 'Cash Transactions'
        ordering = ['-created_at']

    def __str__(self):
        """Return the shift number, transaction type, and amount."""
        return f"{self.shift.shift_number} - {self.transaction_type} - ₹{self.amount}"

    def clean(self):
        """Validate the cash transaction before saving."""
        super().clean()
        if self.amount and self.amount <= 0:
            raise ValidationError({'amount': 'Amount must be positive'})
        
        if self.shift.status == 'Closed':
            raise ValidationError({'shift': 'Cannot add transaction to closed shift'})

    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.created_by:
            self.created_by = self.shift.user
        super().save(*args, **kwargs)
