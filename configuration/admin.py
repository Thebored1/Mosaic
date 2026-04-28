"""
Configuration App Admin
====================

This module provides admin configuration for configuration models.
"""

from django.contrib import admin
from .models import Warehouse, ApiConfiguration


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


@admin.register(ApiConfiguration)
class ApiConfigurationAdmin(admin.ModelAdmin):
    """
    Admin configuration for ApiConfiguration model.
    
    Due to singleton pattern, only edit form is shown.
    No list view - just single edit form.
    
    Fields:
        - api_bearer_token
        - is_active
    
    Read-only:
        - created_at, updated_at
    
    Note:
        Token is displayed as masked in admin for security.
    """
    list_display = ['is_active', 'created_at', 'updated_at']
    fields = ['api_bearer_token', 'is_active']
    readonly_fields = ['created_at', 'updated_at']
    
    def has_add_permission(self, request):
        """Prevent adding new configuration."""
        return False
    
    def has_delete_permission(self, request):
        """Prevent deleting configuration."""
        return False
    
    def get_queryset(self, request):
        """Return singleton instance."""
        return self.model.objects.filter(pk=1)