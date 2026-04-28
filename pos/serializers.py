"""
POS App Serializers
===================

This module provides serializers for POS models.

Serializers:
-----------
ShiftSerializer - Serializer for Shift model
CashTransactionSerializer - Serializer for CashTransaction model
"""

from rest_framework import serializers
from .models import Shift, CashTransaction


class CashTransactionSerializer(serializers.ModelSerializer):
    """
    Serializer for CashTransaction model.
    
    Provides full serialization including:
    - Transaction details
    - Read-only created_by, created_at
    """
    created_by_username = serializers.CharField(
        source='created_by.username',
        read_only=True
    )

    class Meta:
        model = CashTransaction
        fields = [
            'id',
            'shift',
            'transaction_type',
            'amount',
            'reason',
            'reference',
            'created_by',
            'created_by_username',
            'created_at',
        ]
        read_only_fields = ['created_by', 'created_at']
    
    def validate_amount(self, value):
        """Ensure positive amount."""
        if value and value <= 0:
            raise serializers.ValidationError("Amount must be positive")
        return value


class ShiftSerializer(serializers.ModelSerializer):
    """
    Serializer for Shift model.
    
    Provides full serialization including:
    - Shift details
    - Computed sales_total
    - Computed transaction_summary
    - Read-only shift_number, opening_time
    
    Nested:
    - transactions: List of cash transactions
    """
    transactions = CashTransactionSerializer(many=True, read_only=True)
    user_username = serializers.CharField(source='user.username', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.name', read_only=True)
    sales_total = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        read_only=True
    )
    transaction_summary = serializers.DictField(read_only=True)

    class Meta:
        model = Shift
        fields = [
            'id',
            'shift_number',
            'user',
            'user_username',
            'warehouse',
            'warehouse_name',
            'status',
            'opening_cash',
            'closing_cash',
            'expected_cash',
            'variance',
            'opening_time',
            'closing_time',
            'notes',
            'transactions',
            'sales_total',
            'transaction_summary',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'shift_number', 'expected_cash', 'variance',
            'opening_time', 'closing_time', 'created_at', 'updated_at'
        ]
    
    def validate_opening_cash(self, value):
        """Ensure non-negative opening cash."""
        if value is not None and value < 0:
            raise serializers.ValidationError("Opening cash cannot be negative")
        return value
    
    def validate_closing_cash(self, value):
        """Ensure non-negative closing cash."""
        if value is not None and value < 0:
            raise serializers.ValidationError("Closing cash cannot be negative")
        return value
    
    def validate(self, attrs):
        """Validate opening cash for new shifts."""
        if not self.instance and not attrs.get('opening_cash'):
            raise serializers.ValidationError({
                'opening_cash': 'Opening cash is required to open a shift'
            })
        return attrs