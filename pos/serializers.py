"""
POS application serializers.

This module shapes the shift and cash transaction payloads used by the POS
screen and its supporting admin workflows.
"""

from rest_framework import serializers
from .models import Shift, CashTransaction


class CashTransactionSerializer(serializers.ModelSerializer):
    """Serialize cash transactions within a shift."""
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
        """Ensure the transaction amount is positive."""
        if value and value <= 0:
            raise serializers.ValidationError("Amount must be positive")
        return value


class ShiftSerializer(serializers.ModelSerializer):
    """Serialize POS shifts with nested cash transactions."""
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
        """Ensure the opening cash value is non-negative."""
        if value is not None and value < 0:
            raise serializers.ValidationError("Opening cash cannot be negative")
        return value
    
    def validate_closing_cash(self, value):
        """Ensure the closing cash value is non-negative."""
        if value is not None and value < 0:
            raise serializers.ValidationError("Closing cash cannot be negative")
        return value
    
    def validate(self, attrs):
        """Ensure opening cash is present when creating a new shift."""
        if not self.instance and not attrs.get('opening_cash'):
            raise serializers.ValidationError({
                'opening_cash': 'Opening cash is required to open a shift'
            })
        return attrs
