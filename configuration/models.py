"""
Configuration domain models.

This module defines the platform-level setup data and authentication token
storage:

1. State captures Indian GST state metadata.
2. Warehouse captures registered business locations and numbering sequences.
3. ApiToken stores organization/ecommerce user tokens in hashed form.
4. SuperAdminToken stores cross-organization privileged tokens in hashed form.

The models here are shared by stock, sale, pos, commerce, and account flows.
"""

import hashlib
import secrets

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.contrib.auth.models import User


class OrganizationModel(models.Model):
    """
    Abstract base model with organization FK for multi-tenancy.

    Models that inherit from this base are expected to be tenant-scoped unless
    they intentionally support global visibility.
    """
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
    """
    Indian state master for GST place-of-supply calculations.

    States are used whenever the application needs to determine intra-state vs
    inter-state tax behavior or populate address/location selections.
    """
    name = models.CharField(max_length=100)
    state_code = models.CharField(max_length=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        """Return the state name and code."""
        return f"{self.name} ({self.state_code})"


class Warehouse(OrganizationModel):
    """
    Physical warehouse or business location with GSTIN support.

    Warehouses represent registered business locations. They supply the GSTIN
    and financial-year invoice numbering used throughout the sale and purchase
    workflows, and they also act as the physical anchor for inventory and POS
    operations.

    The model replaced the older sale-app `BusinessLocation` concept so the
    tenancy and numbering logic can live in one shared location.
    """
    state = models.ForeignKey(
        'State',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='warehouses',
        help_text='State of registration for GST purposes'
    )
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
        Generate the next invoice number for this warehouse.

        The numbering pattern is GSTIN plus financial year plus sequence. The
        method increments the warehouse's sequence counter and returns the
        formatted value used by invoice creation.
        """
        self.invoice_sequence += 1
        self.save(update_fields=['invoice_sequence'])

        fy = self.get_current_financial_year()
        return f"{self.gstin}/{fy}/{self.invoice_sequence:05d}"

    def get_next_purchase_invoice_number(self):
        """
        Generate the next purchase invoice number for this warehouse.

        Purchase invoice numbering uses the same warehouse sequence pattern as
        sales invoices, but with a purchase-specific marker.
        """
        self.purchase_invoice_sequence += 1
        self.save(update_fields=['purchase_invoice_sequence'])

        fy = self.get_current_financial_year()
        return f"{self.gstin}/PO/{fy}/{self.purchase_invoice_sequence:05d}"

    def get_current_financial_year(self):
        """
        Return the current Indian financial year.

        The financial year rolls over on April 1. This format is used by invoice
        numbering, purchase invoice numbering, and any fiscal reporting that
        needs a compact year label.
        """
        today = timezone.now().date()
        if today.month >= 4:
            return f"{today.year}-{today.year + 1}"
        return f"{today.year - 1}-{today.year}"

    def clean(self):
        """
        Validate warehouse data.

        Validation checks the GSTIN format and keeps only one default warehouse
        active at a time across the tenant.
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
        """Run validation before saving the warehouse."""
        self.full_clean()
        super().save(*args, **kwargs)


class TenantSettings(models.Model):
    """
    Tenant-wide operational settings for commerce and back-office workflows.

    This keeps the business-level defaults separate from CommerceSettings so
    the organization can control print templates, notification toggles, and
    fiscal-year behavior in one place.
    """
    TEMPLATE_CHOICES = [
        ('standard', 'Standard'),
        ('compact', 'Compact'),
        ('thermal', 'Thermal'),
    ]

    organization = models.OneToOneField(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='tenant_settings',
    )
    default_warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='default_for_tenant_settings',
    )
    email_notifications_enabled = models.BooleanField(default=True)
    sms_notifications_enabled = models.BooleanField(default=False)
    invoice_print_template = models.CharField(max_length=20, choices=TEMPLATE_CHOICES, default='standard')
    receipt_print_template = models.CharField(max_length=20, choices=TEMPLATE_CHOICES, default='standard')
    delivery_note_print_template = models.CharField(max_length=20, choices=TEMPLATE_CHOICES, default='standard')
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=4)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Tenant Settings'
        verbose_name_plural = 'Tenant Settings'

    def clean(self):
        """Validate the tenant defaults before saving."""
        super().clean()
        if self.default_warehouse_id and self.default_warehouse.organization_id not in {None, self.organization_id}:
            raise ValidationError({'default_warehouse': 'Default warehouse must belong to the same organization.'})
        if not 1 <= int(self.fiscal_year_start_month) <= 12:
            raise ValidationError({'fiscal_year_start_month': 'Fiscal year start month must be between 1 and 12.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class ApiToken(models.Model):
    """
    Hashed API token linked to a UserAccount.

    Tokens are stored as hashes rather than plaintext so the database does not
    contain reusable bearer secrets. The raw token is only returned once at
    issuance time or rotation time.
    """
    user_account = models.OneToOneField(
        'account.UserAccount',
        on_delete=models.CASCADE,
        related_name='api_token'
    )
    token = models.CharField(max_length=128, blank=True, default='', editable=False)
    token_hash = models.CharField(max_length=64, unique=True)
    token_prefix = models.CharField(max_length=12, blank=True, default='')
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'API Token'
        verbose_name_plural = 'API Tokens'

    @staticmethod
    def hash_token(raw_token):
        """Return a SHA-256 hash for a raw token value."""
        return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()

    @classmethod
    def generate_raw_token(cls):
        """Generate an unpredictable raw bearer token."""
        return secrets.token_urlsafe(32)

    @classmethod
    def default_expiry(cls):
        """Return the default expiry timestamp for newly issued tokens."""
        max_age = int(getattr(settings, 'API_TOKEN_MAX_AGE_SECONDS', 30 * 24 * 60 * 60))
        if max_age <= 0:
            return None
        return timezone.now() + timedelta(seconds=max_age)

    @classmethod
    def issue_token(cls, user_account):
        """Create and persist a new token for the supplied user account."""
        raw_token = cls.generate_raw_token()
        token = cls(user_account=user_account)
        token.set_raw_token(raw_token)
        token.save()
        return token, raw_token

    def set_raw_token(self, raw_token):
        """Store the hashed representation of a raw token."""
        self.token_hash = self.hash_token(raw_token)
        self.token_prefix = raw_token[:8]
        self.token = ''
        self.expires_at = self.default_expiry()

    def rotate_token(self):
        """Generate a new raw token and replace the stored hash."""
        raw_token = self.generate_raw_token()
        self.set_raw_token(raw_token)
        self.is_active = True
        self.save(update_fields=['token_hash', 'token_prefix', 'token', 'is_active', 'expires_at', 'updated_at'])
        return raw_token

    def revoke_token(self):
        """Mark the token as inactive without deleting the record."""
        self.is_active = False
        self.save(update_fields=['is_active', 'updated_at'])
        return None

    def is_expired(self):
        """Return True when the token is past its configured expiry."""
        return self.expires_at is not None and self.expires_at <= timezone.now()

    def mark_used(self):
        """Record successful token use for audit and session hygiene."""
        self.last_used_at = timezone.now()
        self.save(update_fields=['last_used_at', 'updated_at'])

    def __str__(self):
        """Return a human-readable token label."""
        organization = getattr(self.user_account, 'organization', None)
        organization_name = organization.name if organization is not None else 'No Organization'
        return f"Token - {self.user_account.user.username} ({organization_name})"


class SuperAdminToken(models.Model):
    """
    Hashed super admin token for cross-organization access.

    Super admin tokens are intentionally separate from organization/user tokens
    because they bypass tenant scoping and are only meant for platform-wide
    support, reporting, or administrative operations.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='super_admin_token',
        help_text="User must be a superuser"
    )
    token = models.CharField(max_length=128, blank=True, default='', editable=False)
    token_hash = models.CharField(max_length=64, unique=True)
    token_prefix = models.CharField(max_length=12, blank=True, default='')
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Super Admin Token'
        verbose_name_plural = 'Super Admin Tokens'

    @staticmethod
    def hash_token(raw_token):
        """Return a SHA-256 hash for a raw super admin token."""
        return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()

    @classmethod
    def generate_raw_token(cls):
        """Generate an unpredictable raw token for a super admin principal."""
        return secrets.token_urlsafe(32)

    @classmethod
    def default_expiry(cls):
        """Return the default expiry timestamp for new super admin tokens."""
        max_age = int(getattr(settings, 'SUPER_ADMIN_TOKEN_MAX_AGE_SECONDS', 7 * 24 * 60 * 60))
        if max_age <= 0:
            return None
        return timezone.now() + timedelta(seconds=max_age)

    @classmethod
    def issue_token(cls, user):
        """Create and persist a super admin token for the supplied user."""
        token = cls(user=user)
        raw_token = cls.generate_raw_token()
        token.set_raw_token(raw_token)
        token.save()
        return token, raw_token

    def set_raw_token(self, raw_token):
        """Store the hashed representation of a raw super admin token."""
        self.token_hash = self.hash_token(raw_token)
        self.token_prefix = raw_token[:8]
        self.token = ''
        self.expires_at = self.default_expiry()

    def rotate_token(self):
        """Generate a new raw token and replace the stored super admin hash."""
        raw_token = self.generate_raw_token()
        self.set_raw_token(raw_token)
        self.is_active = True
        self.save(update_fields=['token_hash', 'token_prefix', 'token', 'is_active', 'expires_at', 'updated_at'])
        return raw_token

    def revoke_token(self):
        """Mark the super admin token as inactive without deleting it."""
        self.is_active = False
        self.save(update_fields=['is_active', 'updated_at'])
        return None

    def is_expired(self):
        """Return True when the super admin token is past its expiry."""
        return self.expires_at is not None and self.expires_at <= timezone.now()

    def mark_used(self):
        """Record successful super admin token use."""
        self.last_used_at = timezone.now()
        self.save(update_fields=['last_used_at', 'updated_at'])

    def clean(self):
        """Ensure the token is tied to a real Django superuser account."""
        super().clean()
        if not self.user:
            raise ValidationError({'user': 'Super Admin Token must be linked to a superuser'})
        if not self.user.is_superuser:
            raise ValidationError({'user': 'Only superusers can be linked to a Super Admin Token'})

    def save(self, *args, **kwargs):
        """Validate the super admin token before persisting it."""
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        """Return a human-readable super admin token label."""
        return f"SuperAdmin - {self.user.username}"
