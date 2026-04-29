from rest_framework import serializers
from .models import Merchant, Customer


class MerchantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Merchant
        fields = [
            'id', 'name', 'trade_name', 'gstin', 'state', 'address',
            'phone', 'email', 'logo', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'role', 'gstin', 'state', 'address',
            'phone', 'email', 'credit_limit', 'opening_balance',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']