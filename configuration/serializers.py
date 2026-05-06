"""
Configuration App Serializers
==============================

This module provides serializers for configuration models.

Serializers:
----------
WarehouseSerializer - Serializer for Warehouse model
ApiTokenSerializer - Serializer for API tokens (linked to users)
SuperAdminTokenSerializer - Serializer for super admin tokens
"""

from rest_framework import serializers
from .models import State, Warehouse, TenantSettings, ApiToken, SuperAdminToken


class StateSerializer(serializers.ModelSerializer):
    """Serializer for Indian States."""
    class Meta:
        model = State
        fields = ['id', 'name', 'state_code', 'is_active']
        ref_name = 'ConfigurationState'


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


class TenantSettingsSerializer(serializers.ModelSerializer):
    """Serializer for tenant-wide operational settings."""

    default_warehouse_name = serializers.CharField(source='default_warehouse.name', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)

    class Meta:
        model = TenantSettings
        fields = [
            'id',
            'organization',
            'organization_name',
            'default_warehouse',
            'default_warehouse_name',
            'email_notifications_enabled',
            'sms_notifications_enabled',
            'invoice_print_template',
            'receipt_print_template',
            'delivery_note_print_template',
            'fiscal_year_start_month',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class ApiTokenSerializer(serializers.ModelSerializer):
    """Serializer for ApiToken model."""
    username = serializers.CharField(source='user_account.user.username', read_only=True)
    organization_name = serializers.CharField(source='user_account.organization.name', read_only=True)

    class Meta:
        model = ApiToken
        fields = ['id', 'username', 'organization_name', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class SuperAdminTokenSerializer(serializers.ModelSerializer):
    """Serializer for SuperAdminToken model."""
    username = serializers.CharField(source='user.username', read_only=True)

    class Meta:
        model = SuperAdminToken
        fields = ['id', 'username', 'token_prefix', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']
