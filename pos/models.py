"""
POS App Models
==============

This module provides POS-specific models for shift and cash management.

Models:
-------
Shift - Cashier shift management
CashTransaction - Cash in/out during shift
"""

from decimal import Decimal
from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from configuration.models import Warehouse


class Shift(models.Model):
    """
    Cashier/Counter Shift Management
    =================================
    
    Purpose:
    - Track individual cashier shifts
    - Record opening/closing cash counts
    - Calculate variance for accountability
    - Enable shift-wise sales reporting
    
    Workflow:
    1. Cashier opens shift (sets opening_cash)
    2. During shift: record cash in/out transactions
    3. End of day: cashier closes shift (sets closing_cash)
    4. System calculates variance
    
    Status Flow:
    Open -> Closed
    
    Interaction:
    - FK to User (cashier)
    - FK to Warehouse (location)
    - FK to CashTransaction (transactions during shift)
    - FK to sale.Order (orders in this shift)
    - FK to sale.Invoice (invoices in this shift)
    
    Reporting:
    - Shift sales total
    - Cash collected
    - Variance (closing - expected)
    
    Endpoint Interaction:
    - GET /shifts/ - List all shifts
    - POST /shifts/ - Create new shift (open shift)
    - GET /shifts/{id}/ - Get shift details
    - POST /shifts/{id}/close/ - Close shift
    - GET /shifts/{id}/transactions/ - List cash transactions
    - POST /shifts/{id}/transactions/ - Add cash transaction
    
    Query Parameters:
        ?status=Open - Filter by status
        ?warehouse=1 - Filter by warehouse
        ?cashier=1 - Filter by cashier
        ?date=2025-04-28 - Filter by date
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
        return f"{self.shift_number} - {self.user.username} - {self.status}"

    def save(self, *args, **kwargs):
        if not self.shift_number:
            self.shift_number = self.generate_shift_number()
        super().save(*args, **kwargs)

    def generate_shift_number(self):
        """
        Generate unique shift number.
        
        Format: SH{YYYYMMDD}{NNNN}
        Example: SH202504280001
        
        Returns:
            str: Unique shift number
        """
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
        
        Args:
            closing_cash_amount (Decimal): Actual cash in drawer
            
        Workflow:
        1. Set closing_cash
        2. Calculate expected cash from all transactions
        3. Calculate variance (closing - expected)
        4. Set status to Closed
        5. Set closing_time
        
        Expected Cash Calculation:
        = opening_cash + cash.in.sum() - cash.out.sum()
        
        Returns:
            dict: Shift with variance calculation
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
        """
        Calculate total sales during this shift.
        
        Returns:
            Decimal: Sum of all invoices in this shift
        """
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
        """
        Get cash transaction summary for this shift.
        
        Returns:
            dict: cash_in, cash_out, net difference
        """
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
    Cash In/Out Transactions During Shift
    ==================================
    
    Purpose:
    - Record cash movements during a shift
    - Track cash deposits, withdrawals, expenses
    - Enable cash reconciliation
    
    Transaction Types:
    - CashIn: Cash deposits (bank deposit, collections)
    - CashOut: Withdrawals (expenses, petty cash, bank deposit)
    
    Interaction:
    - FK to Shift (parent shift)
    - FK to User (recorded by)
    
    Common Use Cases:
    - Bank deposit (CashIn, reference=bank receipt)
    - Expense payout (CashOut, reason=lunch, expense)
    - Cash withdrawal (CashOut, reason=personal)
    - Change fund (CashIn, reason=change)
    
    Endpoint Interaction:
    - GET /shifts/{shift_id}/transactions/ - List transactions
    - POST /shifts/{shift_id}/transactions/ - Add transaction
    - DELETE /transactions/{id}/ - Delete transaction
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
        return f"{self.shift.shift_number} - {self.transaction_type} - ₹{self.amount}"

    def clean(self):
        """
        Validate transaction.
        
        Rules:
        - Amount must be positive
        - Cannot modify closed shift transactions
        """
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