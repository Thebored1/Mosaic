"""
Stock master serializers.

These serializers define the public stock API contract for the inventory
management layer:

1. master data such as categories, units, attributes, and tax codes
2. item detail payloads with nested variants and images
3. batch, opening stock, movement, and serial tracking records
4. read-only nested summaries for browse-heavy endpoints

The serializer layer is intentionally explicit because the stock app is used
both by admin screens and by other business workflows such as sale and
commerce.
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
    Serialize inventory categories.

    Categories are simple master records, so the serializer keeps the payload
    flat and mirrors the model closely.
    """
    class Meta:
        model = Category
        fields = '__all__'


class UnitSerializer(serializers.ModelSerializer):
    """
    Serialize measurement units.

    Units participate in stock conversion and whole-number validation, so the
    serializer exposes both the base unit fields and the conversion metadata.
    """
    class Meta:
        model = Unit
        fields = '__all__'


class AttributeTypeSerializer(serializers.ModelSerializer):
    """
    Serialize item attribute types.

    Attribute types are used to describe variant dimensions such as color,
    size, or other catalog-specific attributes.
    """
    class Meta:
        model = AttributeType
        fields = '__all__'


class AttributeValueSerializer(serializers.ModelSerializer):
    """
    Serialize concrete attribute values.

    Values are linked to an attribute type and later assigned to item variants
    to define a sellable combination.
    """
    class Meta:
        model = AttributeValue
        fields = '__all__'


class TaxComponentSerializer(serializers.ModelSerializer):
    """
    Serialize the component-level tax breakdown.

    Tax components let the frontend and reporting layers show the GST split
    behind each HSN/SAC tax code.
    """
    class Meta:
        model = TaxComponent
        fields = '__all__'


class TaxCodeSerializer(serializers.ModelSerializer):
    """
    Serialize tax codes with nested components.

    Item creation snapshots these tax components into the item master so later
    tax code edits do not rewrite historical inventory records.
    """
    components = TaxComponentSerializer(many=True, read_only=True)

    class Meta:
        model = TaxCode
        fields = '__all__'


class ItemVariantAttributeSerializer(serializers.ModelSerializer):
    """
    Serialize the link between a variant and an attribute value.

    This serializer is usually used in nested variant payloads where the
    frontend is building or reading a complete SKU definition.
    """
    class Meta:
        model = ItemVariantAttribute
        fields = '__all__'


class ItemVariantSerializer(serializers.ModelSerializer):
    """
    Serialize an item variant with nested attributes.

    Variants are the inventory unit sold when an item has multiple SKUs for
    size, color, or other attribute combinations.
    """
    attributes = ItemVariantAttributeSerializer(many=True, read_only=True)

    class Meta:
        model = ItemVariant
        fields = '__all__'


class ItemImageSerializer(serializers.ModelSerializer):
    """
    Serialize product images.

    Images can belong to either the parent item or a specific variant, letting
    the frontend render both general catalog views and variant-specific views.
    """
    class Meta:
        model = ItemImage
        fields = '__all__'


class ItemListSerializer(serializers.ModelSerializer):
    """
    Serialize inventory items for list views.

    The list serializer stays compact while still surfacing the inventory and
    tax metadata needed by browse screens and selection dialogs.
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
    Serialize a full inventory item detail payload.

    The detail view includes nested variants and images so the frontend can
    render a complete product management or buyer-facing catalog screen.
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
    Serialize inventory batches.

    Batches track lot-level quantities and costs for FIFO/LIFO-aware inventory
    workflows.
    """
    class Meta:
        model = Batch
        fields = '__all__'


class OpeningStockSerializer(serializers.ModelSerializer):
    """
    Serialize opening stock entries.

    Opening stock is typically used once during migration or initial setup and
    then approved into the live inventory totals.
    """
    class Meta:
        model = OpeningStock
        fields = '__all__'


class StockMovementSerializer(serializers.ModelSerializer):
    """
    Serialize stock movements.

    Stock movements are the audit trail for inventory changes and capture the
    quantity, rate, and tax snapshot at the time the movement was recorded.
    """
    class Meta:
        model = StockMovement
        fields = '__all__'


class SerialNumberSerializer(serializers.ModelSerializer):
    """
    Serialize serial-number tracked inventory.

    Serial numbers are used for individually traceable items such as phones or
    other high-value stock.
    """
    
    class Meta:
        model = SerialNumber
        fields = '__all__'
