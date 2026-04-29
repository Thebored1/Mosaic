from django.contrib import admin
from .models import Merchant, Customer


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ['name', 'trade_name', 'gstin', 'phone', 'email']
    search_fields = ['name', 'trade_name', 'gstin']


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ['name', 'role', 'gstin', 'phone', 'email']
    search_fields = ['name', 'gstin']
    list_filter = ['role']