"""
Stock Master Serializers
=====================

DRF serializers for API endpoints.
Each serializer maps models to JSON.

Key Features:
- Nested serializers for related data
- Automatic tax rate snapshot on item creation
- Read-only nested data in list views
"""

from rest_framework import serializers
from .models import (
    Category, Unit, AttributeType, AttributeValue,
    TaxCode, TaxComponent,
    Item, ItemVariant, ItemVariantAttribute, Batch,
    ItemImage, OpeningStock, StockMovement, SerialNumber
)


class CategorySerializer(serializers.ModelSerializer):
    """
    Serializer for Category model.
    
    Endpoints:
        GET/POST /v1/api/categories/
        GET/PUT/DELETE /v1/api/categories/{id}/
    
    Fields: id, name, description, is_active
    """
    class Meta:
        model = Category
        fields = '__all__'


class UnitSerializer(serializers.ModelSerializer):
    """
    Serializer for Unit model.
    
    Endpoints:
        GET/POST /v1/api/units/
        GET/PUT/DELETE /v1/api/units/{id}/
    
    Fields: id, name, short_code, target_unit, conversion_factor, 
            must_be_whole_number, is_active
    
    Nested: target_unit (read only)
    """
    class Meta:
        model = Unit
        fields = '__all__'


class AttributeTypeSerializer(serializers.ModelSerializer):
    """
    Serializer for AttributeType model.
    
    Endpoints:
        GET/POST /v1/api/attribute-types/
        GET/PUT/DELETE /v1/api/attribute-types/{id}/
    
    Fields: id, name, description
    """
    class Meta:
        model = AttributeType
        fields = '__all__'


class AttributeValueSerializer(serializers.ModelSerializer):
    """
    Serializer for AttributeValue model.
    
    Endpoints:
        GET/POST /v1/api/attribute-values/
        GET/PUT/DELETE /v1/api/attribute-values/{id}/
    
    Fields: id, attribute_type, value
    
    Nested: attribute_type (read only)
    
    Filters: ?attribute_type=1
    """
    class Meta:
        model = AttributeValue
        fields = '__all__'


class TaxComponentSerializer(serializers.ModelSerializer):
    """
    Serializer for TaxComponent model.
    
    Endpoints:
        GET/POST /v1/api/tax-components/
        GET/PUT/DELETE /v1/api/tax-components/{id}/
    
    Fields: id, tax_code, component, rate
    
    Nested: tax_code (read only)
    
    Filters: ?tax_code=1, ?component=CGST
    """
    class Meta:
        model = TaxComponent
        fields = '__all__'


class TaxCodeSerializer(serializers.ModelSerializer):
    """
    Serializer for TaxCode with nested components.
    
    Endpoints:
        GET/POST /v1/api/tax-codes/
        GET/PUT/DELETE /v1/api/tax-codes/{id}/
    
    Fields: id, name, code_type, code, is_exempt, is_active, components
    
    Nested: components (list of TaxComponent)
    
    How Tax Rates Work with Items:
        When Item is created with tax_code:
        1. Serializer looks up TaxCode.components
        2. Extracts CGST, SGST, IGST rates
        3. Saves to Item.cgst_rate, sgst_rate, igst_rate
        4. These values persist even if TaxCode changes
    
    Filters: ?is_active=true, ?is_exempt=true, ?code_type=HSN
    """
    components = TaxComponentSerializer(many=True, read_only=True)

    class Meta:
        model = TaxCode
        fields = '__all__'


class ItemVariantAttributeSerializer(serializers.ModelSerializer):
    """
    Serializer for ItemVariantAttribute.
    
    Endpoints: Via nested under item variants
    
    Fields: id, item_variant, attribute_value
    
    Nested: attribute_value (read only)
    """
    class Meta:
        model = ItemVariantAttribute
        fields = '__all__'


class ItemVariantSerializer(serializers.ModelSerializer):
    """
    Serializer for ItemVariant with nested attributes.
    
    Endpoints:
        GET/POST /v1/api/items/{id}/variants/
        GET/PUT/DELETE /v1/api/items/{id}/variants/{id}/
    
    Fields: id, item, sku, unit_price, cost_price, current_stock, is_active, attributes
    
    Nested: attributes (list of ItemVariantAttribute)
    
    Note: item is read-only in nested context, write only in full list
    """
    attributes = ItemVariantAttributeSerializer(many=True, read_only=True)

    class Meta:
        model = ItemVariant
        fields = '__all__'


class ItemImageSerializer(serializers.ModelSerializer):
    """
    Serializer for ItemImage.
    
    Endpoints:
        GET/POST /v1/api/items/{id}/images/
        GET/PUT/DELETE /v1/api/items/{id}/images/{id}/
    
    Fields: id, item, item_variant, image, is_primary, display_order
    
    WebP Conversion:
        - Happens automatically on model save
        - Non-WebP images converted to WebP format
    """
    class Meta:
        model = ItemImage
        fields = '__all__'


class ItemListSerializer(serializers.ModelSerializer):
    """
    Serializer for Item list view.
    
    Used when listing items (not detailed view).
    
    Endpoints:
        GET /v1/api/items/
    
    Fields: id, name, sku, description, category, unit, tax_code,
            cgst_rate, sgst_rate, igst_rate, has_variants, current_stock,
            min_stock_level, max_stock_level, unit_price, cost_price,
            is_active, created_at, updated_at
    
    Nested (read only): category, unit, tax_code (with components)
    
    Filters: ?is_active=true, ?category=1, ?unit=1, ?has_variants=true
    Search: ?search=query (name, sku, description)
    """
    category = CategorySerializer(read_only=True)
    unit = UnitSerializer(read_only=True)
    tax_code = TaxCodeSerializer(read_only=True)

    class Meta:
        model = Item
        fields = [
            'id', 'name', 'sku', 'description', 'category', 'unit',
            'tax_code', 'cgst_rate', 'sgst_rate', 'igst_rate',
            'has_variants', 'current_stock', 'min_stock_level',
            'max_stock_level', 'unit_price', 'cost_price', 'is_active',
            'created_at', 'updated_at'
        ]


class ItemDetailSerializer(serializers.ModelSerializer):
    """
    Serializer for Item detail view.
    
    Used when retrieving single item.
    
    Endpoints:
        GET /v1/api/items/{id}/
        POST /v1/api/items/
        PUT /v1/api/items/{id}/
    
    Fields: All Item fields + nested variants and images
    
    Nested (read only): category, unit, tax_code, variants, images
    
    Tax Rate Snapshot:
        On create/update with tax_code:
        - Serializer captures current TaxComponent rates
        - Saves to cgst_rate, sgst_rate, igst_rate
        - Rates persist independent of TaxCode changes
    """
    category = CategorySerializer(read_only=True)
    unit = UnitSerializer(read_only=True)
    tax_code = TaxCodeSerializer(read_only=True)
    variants = ItemVariantSerializer(many=True, read_only=True)
    images = ItemImageSerializer(many=True, read_only=True)

    class Meta:
        model = Item
        fields = '__all__'


class BatchSerializer(serializers.ModelSerializer):
    """
    Serializer for Batch model.
    
    Endpoints:
        GET/POST /v1/api/batches/
        GET/PUT/DELETE /v1/api/batches/{id}/
    
    Fields: id, batch_number, item_variant, quantity_received,
            quantity_remaining, cost_per_unit, received_date, expiry_date
    
    Nested: item_variant (read only)
    
    Filters: ?item_variant=1
    
    Usage: Tracks inventory by lot for FIFO/LIFO costing
    """
    class Meta:
        model = Batch
        fields = '__all__'


class OpeningStockSerializer(serializers.ModelSerializer):
    """
    Serializer for OpeningStock model.
    
    Endpoints:
        GET/POST /v1/api/opening-stock/
        GET/PUT/DELETE /v1/api/opening-stock/{id}/
    
    Fields: id, item, item_variant, quantity, unit_cost, as_of_date,
            notes, status, created_at, updated_at
    
    Workflow:
        1. Create with status=Pending
        2. Admin approves -> updates stock
        3. One-time use
    
    Filters: ?status=Pending, ?item=1
    """
    class Meta:
        model = OpeningStock
        fields = '__all__'


class StockMovementSerializer(serializers.ModelSerializer):
    """
    Serializer for StockMovement model.
    
    Endpoints:
        GET/POST /v1/api/stock-movements/
        GET/PUT/DELETE /v1/api/stock-movements/{id}/
    
    Fields: id, movement_type, item, item_variant, batch, quantity,
            rate, cgst_rate, sgst_rate, igst_rate, total_amount,
            reference_number, movement_date, status, notes,
            created_at, updated_at
    
    Tax Snapshot:
        Rates captured at creation time for historical accuracy
    
    Workflow:
        1. Create with status=Pending
        2. Admin approves -> updates stock
        3. If batch linked, decrements batch quantity
    
    Filters: ?status=Pending, ?movement_type=Purchase, ?item=1
    """
    class Meta:
        model = StockMovement
        fields = '__all__'


class SerialNumberSerializer(serializers.ModelSerializer):
    """Serializer for SerialNumber."""
    
    class Meta:
        model = SerialNumber
        fields = '__all__'