"""
POS App Admin
=============

This module provides admin configuration for POS models.
"""

from django.contrib import admin
from .models import Shift, CashTransaction


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    """
    Admin configuration for Shift model.
    
    List Display:
        - shift_number, user, warehouse, status, opening_time, closing_time, variance
    
    List Filter:
        - status, warehouse, user
    
    Search:
        - shift_number
    
    Ordering:
        - -opening_time
    """
    list_display = [
        'shift_number', 'user', 'warehouse', 'status',
        'opening_time', 'closing_time', 'variance'
    ]
    list_filter = ['status', 'warehouse', 'user']
    search_fields = ['shift_number']
    ordering = ['-opening_time']
    readonly_fields = [
        'shift_number', 'opening_time', 'closing_time',
        'expected_cash', 'variance', 'created_at', 'updated_at'
    ]
    
    fieldsets = (
        ('Shift Information', {
            'fields': ('shift_number', 'user', 'warehouse', 'status', 'notes')
        }),
        ('Cash Details', {
            'fields': ('opening_cash', 'closing_cash', 'expected_cash', 'variance')
        }),
        ('Timing', {
            'fields': ('opening_time', 'closing_time')
        }),
    )


@admin.register(CashTransaction)
class CashTransactionAdmin(admin.ModelAdmin):
    """
    Admin configuration for CashTransaction model.
    
    List Display:
        - shift, transaction_type, amount, reason, created_by, created_at
    
    List Filter:
        - transaction_type
    
    Ordering:
        - -created_at
    """
    list_display = ['shift', 'transaction_type', 'amount', 'reason', 'created_by', 'created_at']
    list_filter = ['transaction_type']
    ordering = ['-created_at']
    readonly_fields = ['created_at']
    
    def has_add_permission(self, request):
        """Only allow adding via shift admin."""
        return False