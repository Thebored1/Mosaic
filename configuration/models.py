"""
Configuration App Models
=======================

This module provides configuration models for business setup:
- Organization - Multi-tenant business entity
- Warehouse - Physical locations with GSTIN
- State - Indian states for GST
- ApiConfiguration - Bearer token authentication
"""

from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone


class OrganizationModel(models.Model):
    """Abstract base model with organization FK for multi-tenancy."""
    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='%(class)s_set',
        null=True,
        blank=True
    )

    class Meta:
        abstract = True


class State(OrganizationModel):
    """Indian State for GST Place of Supply."""
    name = models.CharField(max_length=100)
    state_code = models.CharField(max_length=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.state_code})"


class Warehouse(OrganizationModel):
    """
    Physical Warehouse/Business Location with GSTIN Support
    ========================================================
    
    Purpose:
    - Manages physical warehouse/location of the business
    - Stores GSTIN for multi-state GST compliance
    - Generates invoice series for each location
    - Tracks inventory per warehouse
    
    Migration Note:
    - This model replaces BusinessLocation from the sale app
    - BusinessLocation data should be migrated to this model
    
    Interaction:
    - FK in sale.Invoice (for invoice numbering per location)
    - FK in sale.PurchaseInvoice (for multi-location purchase tracking)
    - FK in sale.Order (to track which location created the order)
    - FK in sale.Receipt / sale.PaymentOut (to track payments per location)
    - FK in sale.Quotation (for quotation per location)
    - FK in stock.Batch (for batch inventory per warehouse)
    - FK in stock.StockMovement (for stock transactions per warehouse)
    - FK in stock.OpeningStock (for initial stock per warehouse)
    - FK in stock.SerialNumber (for serial tracking per warehouse)
    - FK in pos.Shift (for shift management per warehouse)
    
    Invoice Numbering Logic:
    - Format: {GSTIN}/{Financial Year}/{Sequential Number}
    - Example: 27AAAAA0000A1Z5/2025-26/00001
    - Each warehouse has its own sequence counter
    - When creating invoice, the warehouse determines the prefix
    
    Endpoint Interaction:
    - GET/POST /v1/configuration/warehouses/ - List/Create warehouses
    - GET/PUT/DELETE /v1/configuration/warehouses/{id}/ - Warehouse detail
    - PUT /v1/configuration/warehouses/{id}/set-default/ - Set as default
    - On invoice creation, automatically uses warehouse's GSTIN for numbering
    """
    gstin = models.CharField(
        max_length=15,
        unique=True,
        help_text="Valid 15-character GSTIN (e.g., 27AAAAA0000A1Z5)"
    )
    name = models.CharField(
        max_length=200,
        help_text="Warehouse/Location name"
    )
    code = models.CharField(
        max_length=10,
        unique=True,
        help_text="Short code for warehouse (e.g., WH-01, MUM-01)"
    )
    legal_name = models.CharField(
        max_length=200,
        help_text="Legal name as per GST registration"
    )
    trade_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Trading name (if different from legal name)"
    )
    address = models.TextField(help_text="Complete registered address")
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    
    invoice_sequence = models.PositiveIntegerField(
        default=0,
        help_text="Current invoice number sequence for this location"
    )
    purchase_invoice_sequence = models.PositiveIntegerField(
        default=0,
        help_text="Current purchase invoice sequence"
    )
    
    is_default = models.BooleanField(
        default=False,
        help_text="Default warehouse for new transactions if no warehouse selected"
    )
    is_active = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Warehouse'
        verbose_name_plural = 'Warehouses'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.gstin})"

    def get_next_invoice_number(self):
        """
        Generate next invoice number for this warehouse.
        
        Format: {GSTIN}/{FY}/{NNNNN}
        Example: 27AAAAA0000A1Z5/2025-26/00001
        
        Interaction:
        - Increments invoice_sequence by 1
        - Uses current financial year
        - Called by sale.Invoice.save() during finalization
        
        Returns:
            str: Formatted invoice number
        """
        self.invoice_sequence += 1
        self.save(update_fields=['invoice_sequence'])

        fy = self.get_current_financial_year()
        return f"{self.gstin}/{fy}/{self.invoice_sequence:05d}"

    def get_next_purchase_invoice_number(self):
        """
        Generate next purchase invoice number for this warehouse.
        
        Format: {GSTIN}/PO/{FY}/{NNNNN}
        Example: 27AAAAA0000A1Z5/PO/2025-26/00001
        
        Returns:
            str: Formatted purchase invoice number
        """
        self.purchase_invoice_sequence += 1
        self.save(update_fields=['purchase_invoice_sequence'])

        fy = self.get_current_financial_year()
        return f"{self.gstin}/PO/{fy}/{self.purchase_invoice_sequence:05d}"

    def get_current_financial_year(self):
        """
        Get current financial year based on date.
        
        Logic:
        - April to December: FY is current_year - next_year
        - January to March: FY is previous_year - current_year
        
        Example:
        - April 2025 to March 2026 = 2025-26
        - January 2025 = 2024-25
        
        Returns:
            str: Financial year in format YYYY-YY
        """
        today = timezone.now().date()
        if today.month >= 4:
            return f"{today.year}-{today.year + 1}"
        return f"{today.year - 1}-{today.year}"

    def clean(self):
        """
        Validate warehouse data.
        
        Validation:
        - GSTIN must be exactly 15 characters
        - First 2 characters of GSTIN must be digits (state code)
        - If is_default=True, unset other default warehouses
        """
        super().clean()
        if self.gstin:
            self.gstin = self.gstin.upper()
            if len(self.gstin) != 15:
                raise ValidationError({'gstin': 'GSTIN must be exactly 15 characters'})
            if not self.gstin[:2].isdigit():
                raise ValidationError({'gstin': 'First 2 characters must be state code (digits)'})
        
        if self.is_default:
            Warehouse.objects.filter(is_default=True).exclude(pk=self.pk).update(is_default=False)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class ApiConfiguration(models.Model):
    """API Bearer Token per Organization."""
    organization = models.OneToOneField(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='api_configuration',
        null=True,
        blank=True
    )
    api_bearer_token = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'API Configuration'
        verbose_name_plural = 'API Configuration'

    def __str__(self):
        return f"API - {self.organization.name}"

    def delete(self, *args, **kwargs):
        """
        Prevent deletion of singleton.
        
        Raises:
            ValidationError: Cannot delete the only API configuration
        """
        raise ValidationError("Cannot delete the API configuration. Edit it instead.")

    def __str__(self):
        return 'API Configuration'