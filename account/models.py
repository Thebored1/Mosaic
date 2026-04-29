"""
Account App Models - Multi-Tenant Organization & User Management
===========================================================

This module provides:
1. Organization - Multi-tenant business organization
2. UserAccount - User linked to Organization with roles
3. Merchant - Suppliers/vendors we buy from
4. Customer - Customers we sell to
"""

from decimal import Decimal
from django.db import models
from django.contrib.auth.models import User


class Organization(models.Model):
    """Multi-tenant Organization - business tenant."""
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
        return self.name


class UserAccount(models.Model):
    """User linked to Organization with role."""
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
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='users')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='Staff')
    phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['user', 'organization']

    def __str__(self):
        return f"{self.user.username} - {self.organization.name}"


class Merchant(models.Model):
    """Supplier/vendor we buy from."""
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
        return self.name


class Customer(models.Model):
    """Customer we sell to."""
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
        return f"{self.name} ({self.role})"