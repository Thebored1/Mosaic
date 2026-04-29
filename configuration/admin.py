"""
Configuration App Admin
======================

This module provides admin configuration for configuration models.
"""

from django.contrib import admin, messages
from .models import Warehouse, ApiToken, SuperAdminToken


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    """
    Admin configuration for Warehouse model.
    
    List Display:
        - name, gstin, code, is_default, is_active
    
    List Filter:
        - is_active, is_default
    
    Search:
        - name, gstin, code, legal_name
    
    Ordering:
        - name
    
    Fieldsets:
        - Basic Info (name, code, gstin)
        - GST Details (legal_name, trade_name)
        - Contact (address, phone, email)
        - Settings (is_default, is_active)
    """
    list_display = ['name', 'gstin', 'code', 'is_default', 'is_active', 'invoice_sequence']
    list_filter = ['is_active', 'is_default']
    search_fields = ['name', 'gstin', 'code', 'legal_name']
    ordering = ['name']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'code', 'gstin')
        }),
        ('GST Registration Details', {
            'fields': ('legal_name', 'trade_name', 'address')
        }),
        ('Contact Details', {
            'fields': ('phone', 'email'),
            'classes': ('collapse',)
        }),
        ('Settings', {
            'fields': ('is_default', 'is_active')
        }),
    )


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    """Admin configuration for ApiToken model."""
    list_display = ['user_account', 'token_prefix', 'is_active', 'created_at', 'updated_at']
    list_filter = ['is_active']
    search_fields = ['user_account__user__username', 'user_account__organization__name']
    readonly_fields = ['created_at', 'updated_at']
    raw_id_fields = ['user_account']


@admin.register(SuperAdminToken)
class SuperAdminTokenAdmin(admin.ModelAdmin):
    """Admin configuration for SuperAdminToken model."""
    list_display = ['user', 'token_prefix', 'is_active', 'created_at', 'updated_at']
    list_filter = ['is_active']
    search_fields = ['user__username', 'user__email']
    readonly_fields = ['created_at', 'updated_at']
    raw_id_fields = ['user']
    actions = ['regenerate_token']

    @admin.action(description='Regenerate selected tokens')
    def regenerate_token(self, request, queryset):
        for obj in queryset:
            new_token = obj.rotate_token()
            self.message_user(request, f"Regenerated token for {obj.user.username}: {new_token}", messages.SUCCESS)
