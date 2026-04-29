"""
Account domain models.

This module defines the tenant and identity primitives used across the
application:

1. Organization is the top-level business tenant.
2. UserAccount binds a Django auth user to an organization or ecommerce-only
   account state.
3. Merchant captures suppliers/vendors that an organization buys from.
4. Customer captures buyers and counterparties for sales-side workflows.

Design notes:
    - Organization is the tenant boundary for stock, configuration, sale, and
      POS features.
    - UserAccount is the application-level profile, not the auth user itself.
    - The account_type field distinguishes ecommerce-only users from
      organization users and system-level super admin contexts.
"""

from decimal import Decimal
from django.db import models
from django.contrib.auth.models import User


class Organization(models.Model):
    """
    Top-level multi-tenant business organization.

    An Organization owns the business data for one tenant. Most operational
    records in the application hang off this model either directly or through
    a related organization-aware model in other apps.
    """
    name = models.CharField(max_length=200)
    trade_name = models.CharField(max_length=200, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    logo = models.ImageField(upload_to='organizations/logos/', null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Organization'
        verbose_name_plural = 'Organizations'
        ordering = ['name']

    def __str__(self):
        """Return the organization name for admin and debug displays."""
        return self.name


class UserAccount(models.Model):
    """
    Application-level user profile with optional organization membership.

    The Django auth user handles authentication credentials. UserAccount adds
    the business-facing context required by the application:

        - account_type distinguishes ecommerce-only, organization, and
          super-admin-originated identities
        - organization links the account to a tenant when applicable
        - role is only meaningful for organization-backed accounts

    This model is the source of truth for tenant-aware authorization and for
    deciding whether a user can access the back-office or only the commerce
    surface.
    """
    ACCOUNT_TYPE_CHOICES = [
        ('ecommerce', 'Ecommerce'),
        ('org_user', 'Organization User'),
        ('super_admin', 'Super Admin'),
    ]

    ROLE_CHOICES = [
        ('Owner', 'Owner'),
        ('Admin', 'Admin'),
        ('Manager', 'Manager'),
        ('Sales', 'Sales'),
        ('Delivery', 'Delivery'),
        ('Warehouse', 'Warehouse'),
        ('Staff', 'Staff'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='account')
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='users',
        null=True,
        blank=True,
    )
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES, default='ecommerce')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='Staff')
    phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['user', 'organization']

    def __str__(self):
        """Render a concise human-readable account label."""
        if self.organization:
            return f"{self.user.username} - {self.organization.name}"
        return f"{self.user.username} - {self.account_type}"


class Merchant(models.Model):
    """
    Supplier or vendor record owned by an organization.

    Merchants are used on the purchase side to represent counterparties that
    supply goods or services into the tenant's inventory and accounting flows.
    """
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='merchants')
    name = models.CharField(max_length=200)
    trade_name = models.CharField(max_length=200, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    logo = models.ImageField(upload_to='merchants/logos/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Merchant'
        verbose_name_plural = 'Merchants'
        ordering = ['name']

    def __str__(self):
        """Return the merchant name."""
        return self.name


class Customer(models.Model):
    """
    Customer master record owned by an organization.

    Customers are the selling-side counterparty. They support both retail and
    B2B scenarios by keeping credit, GST, and contact details in one place.
    """
    ROLE_CHOICES = [
        ('Regular', 'Regular'),
        ('Preferred', 'Preferred'),
        ('Walk-in', 'Walk-in'),
        ('Subscription', 'Subscription'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='customers')
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='Regular')
    gstin = models.CharField(max_length=15, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Customer'
        verbose_name_plural = 'Customers'
        ordering = ['name']

    def __str__(self):
        """Return a display string with the customer role."""
        return f"{self.name} ({self.role})"
