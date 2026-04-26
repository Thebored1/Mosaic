from rest_framework import serializers
from .models import (
    Category, Unit, AttributeType, AttributeValue,
    TaxCode, TaxComponent,
    Item, ItemVariant, ItemVariantAttribute, Batch,
    ItemImage, OpeningStock, StockMovement
)


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = '__all__'


class UnitSerializer(serializers.ModelSerializer):
    class Meta:
        model = Unit
        fields = '__all__'


class AttributeTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeType
        fields = '__all__'


class AttributeValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeValue
        fields = '__all__'


class TaxComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaxComponent
        fields = '__all__'


class TaxCodeSerializer(serializers.ModelSerializer):
    components = TaxComponentSerializer(many=True, read_only=True)

    class Meta:
        model = TaxCode
        fields = '__all__'


class ItemVariantAttributeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ItemVariantAttribute
        fields = '__all__'


class ItemVariantSerializer(serializers.ModelSerializer):
    attributes = ItemVariantAttributeSerializer(many=True, read_only=True)

    class Meta:
        model = ItemVariant
        fields = '__all__'


class ItemImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ItemImage
        fields = '__all__'


class ItemListSerializer(serializers.ModelSerializer):
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
    category = CategorySerializer(read_only=True)
    unit = UnitSerializer(read_only=True)
    tax_code = TaxCodeSerializer(read_only=True)
    variants = ItemVariantSerializer(many=True, read_only=True)
    images = ItemImageSerializer(many=True, read_only=True)

    class Meta:
        model = Item
        fields = '__all__'


class BatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Batch
        fields = '__all__'


class OpeningStockSerializer(serializers.ModelSerializer):
    class Meta:
        model = OpeningStock
        fields = '__all__'


class StockMovementSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockMovement
        fields = '__all__'