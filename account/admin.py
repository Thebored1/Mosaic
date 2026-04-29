from django.contrib import admin
from .models import Organization, Merchant, Customer



@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ['name', 'trade_name', 'gstin', 'is_active']
    search_fields = ['name', 'trade_name', 'gstin']
    list_filter = ['is_active']

@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ['name', 'trade_name', 'gstin', 'phone', 'email']
    search_fields = ['name', 'trade_name', 'gstin']


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['name', 'role', 'gstin', 'phone', 'email']
    search_fields = ['name', 'gstin']
    list_filter = ['role']