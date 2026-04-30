"""
POS application serializers.

This module shapes the shift and cash transaction payloads used by the POS
screen and its supporting admin workflows.
"""

from decimal import Decimal

from rest_framework import serializers

from configuration.models import Warehouse
from sale.models import Party

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


class POSCheckoutSerializer(serializers.Serializer):
    """Validate a POS checkout request."""

    shift = serializers.PrimaryKeyRelatedField(queryset=Shift.objects.all())
    order_id = serializers.IntegerField(required=False)
    business_location = serializers.PrimaryKeyRelatedField(
        queryset=Warehouse.objects.all(),
        required=False,
    )
    party = serializers.PrimaryKeyRelatedField(
        queryset=Party.objects.all(),
        required=False,
        allow_null=True,
    )
    items = serializers.ListField(child=serializers.DictField(), required=False)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=Decimal('0'))
    discount_type = serializers.ChoiceField(choices=[('Percentage', 'Percentage'), ('Fixed', 'Fixed')], required=False, default='Fixed')
    hold_notes = serializers.CharField(required=False, allow_blank=True, default='')
    invoice_type = serializers.CharField(required=False, default='Cash')
    due_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    terms = serializers.CharField(required=False, allow_blank=True, default='')
    payment_mode = serializers.ChoiceField(
        choices=[
            ('Cash', 'Cash'),
            ('Card', 'Card'),
            ('UPI', 'UPI'),
            ('Bank Transfer', 'Bank Transfer'),
            ('Credit', 'Credit'),
        ],
        required=False,
        default='Cash',
    )
    paid_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=Decimal('0'))
    reference_number = serializers.CharField(required=False, allow_blank=True, default='')
    receipt_notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        if not attrs.get('order_id') and not attrs.get('items'):
            raise serializers.ValidationError({'items': 'Provide order_id or at least one item for POS checkout.'})
        return attrs
