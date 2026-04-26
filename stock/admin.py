from django.contrib import admin
from .models import (
    Category, Unit, AttributeType, AttributeValue,
    TaxCode, TaxComponent,
    Item, ItemVariant, ItemVariantAttribute, Batch,
    ItemImage, OpeningStock, StockMovement,
    ApiConfiguration
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')
    search_fields = ('name',)
    list_filter = ('is_active',)


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ('name', 'short_code', 'target_unit', 'conversion_factor', 'must_be_whole_number', 'is_active')
    search_fields = ('name', 'short_code')
    list_filter = ('is_active', 'must_be_whole_number')


@admin.register(AttributeType)
class AttributeTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'description')
    search_fields = ('name',)


@admin.register(AttributeValue)
class AttributeValueAdmin(admin.ModelAdmin):
    list_display = ('attribute_type', 'value')
    list_filter = ('attribute_type',)
    search_fields = ('value',)


@admin.register(TaxCode)
class TaxCodeAdmin(admin.ModelAdmin):
    list_display = ('name', 'code_type', 'code', 'is_exempt', 'is_active')
    list_filter = ('code_type', 'is_active', 'is_exempt')
    search_fields = ('name', 'code')


@admin.register(TaxComponent)
class TaxComponentAdmin(admin.ModelAdmin):
    list_display = ('tax_code', 'component', 'rate')
    list_filter = ('component',)


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ('name', 'sku', 'category', 'unit', 'tax_code', 'has_variants', 'current_stock', 'unit_price', 'is_active')
    search_fields = ('name', 'sku')
    list_filter = ('is_active', 'category', 'unit', 'has_variants')
    ordering = ('name',)


@admin.register(ItemVariant)
class ItemVariantAdmin(admin.ModelAdmin):
    list_display = ('item', 'sku', 'unit_price', 'current_stock', 'is_active')
    search_fields = ('sku', 'item__name')
    list_filter = ('is_active', 'item')
    ordering = ('item__name',)


@admin.register(ItemVariantAttribute)
class ItemVariantAttributeAdmin(admin.ModelAdmin):
    list_display = ('item_variant', 'attribute_value')
    list_filter = ('item_variant', 'attribute_value')


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ('batch_number', 'item_variant', 'quantity_received', 'quantity_remaining', 'cost_per_unit', 'received_date', 'expiry_date')
    search_fields = ('batch_number', 'item_variant__sku')
    list_filter = ('item_variant', 'received_date')
    ordering = ('-received_date',)


@admin.register(ItemImage)
class ItemImageAdmin(admin.ModelAdmin):
    list_display = ('item', 'item_variant', 'image', 'is_primary', 'display_order')
    list_filter = ('is_primary',)


@admin.register(OpeningStock)
class OpeningStockAdmin(admin.ModelAdmin):
    list_display = ('item', 'item_variant', 'quantity', 'unit_cost', 'as_of_date', 'status')
    list_filter = ('status', 'as_of_date')
    search_fields = ('item__sku', 'item__name')


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ('movement_type', 'item', 'item_variant', 'quantity', 'rate', 'status', 'movement_date')
    list_filter = ('movement_type', 'status', 'movement_date')
    search_fields = ('item__sku', 'reference_number')


@admin.register(ApiConfiguration)
class ApiConfigurationAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        return redirect('./1/change/')

    list_display = ('api_bearer_token', 'is_active')