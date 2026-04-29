"""
Configuration App Serializers
=============================

This module provides serializers for configuration models.

Serializers:
----------
WarehouseSerializer - Serializer for Warehouse model
ApiConfigurationSerializer - Serializer for ApiConfiguration model
"""

from rest_framework import serializers
from .models import State, Warehouse, ApiConfiguration


class StateSerializer(serializers.ModelSerializer):
    """Serializer for Indian States."""
    class Meta:
        model = State
        fields = ['id', 'name', 'state_code', 'is_active']


class WarehouseSerializer(serializers.ModelSerializer):
    """
    Serializer for Warehouse model.
    
    Provides full serialization of Warehouse including:
    - All warehouse fields
    - Computed invoice number
    - Read-only created_at/updated_at
    
    Supports:
    - list, create, retrieve, update, partial_update
    - Automatic invoice sequence generation on finalization
    """
    
    class Meta:
        model = Warehouse
        fields = [
            'id',
            'gstin',
            'name',
            'code',
            'legal_name',
            'trade_name',
            'address',
            'phone',
            'email',
            'invoice_sequence',
            'purchase_invoice_sequence',
            'is_default',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['invoice_sequence', 'purchase_invoice_sequence', 'created_at', 'updated_at']
    
    def validate_gstin(self, value):
        """Convert GSTIN to uppercase."""
        if value:
            value = value.upper()
        return value
    
    def validate(self, attrs):
        """Ensure only one default warehouse."""
        is_default = attrs.get('is_default', False)
        if is_default and not self.instance:
            Warehouse.objects.filter(is_default=True).update(is_default=False)
        return attrs


class ApiConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApiConfiguration
        fields = ['id', 'api_bearer_token', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']