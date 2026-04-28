"""
Stock Master Views
===============

DRF viewsets for API endpoints.

Key Features:
- ViewSets for all models
- Filtering, searching, ordering
- Pagination (10 per page default)
- Custom nested endpoints for variants and images
"""

from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from .models import (
    Category, Unit, AttributeType, AttributeValue,
    TaxCode, TaxComponent,
    Item, ItemVariant, ItemVariantAttribute, Batch,
    ItemImage, OpeningStock, StockMovement
)
from .serializers import (
    CategorySerializer, UnitSerializer,
    AttributeTypeSerializer, AttributeValueSerializer,
    TaxCodeSerializer, TaxComponentSerializer,
    ItemListSerializer, ItemDetailSerializer,
    ItemVariantSerializer, ItemVariantAttributeSerializer,
    ItemImageSerializer, BatchSerializer,
    OpeningStockSerializer, StockMovementSerializer
)


class StandardPagination(PageNumberPagination):
    """
    Standard pagination for all endpoints.
    
    Defaults:
        page_size: 10
        page_size_query_param: page_size
        max_page_size: 100
    
    Response Format:
        {
            "count": 100,
            "next": "http://localhost:8000/v1/api/items/?page=2",
            "previous": null,
            "results": [...]
        }
    """
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class CategoryViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Category.
    
    Endpoints:
        GET /v1/api/categories/ - List categories
        POST /v1/api/categories/ - Create category
        GET /v1/api/categories/{id}/ - Retrieve category
        PUT /v1/api/categories/{id}/ - Update category
        DELETE /v1/api/categories/{id}/ - Delete category
    
    Filtering: ?is_active=true
    Searching: ?search=query (name, description)
    Ordering: ?ordering=name, ?-name
    
    Fields: id, name, description, is_active
    """
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'id']
    ordering = ['name']


class UnitViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Unit.
    
    Endpoints:
        GET /v1/api/units/ - List units
        POST /v1/api/units/ - Create unit
        GET /v1/api/units/{id}/ - Retrieve unit
        PUT /v1/api/units/{id}/ - Update unit
        DELETE /v1/api/units/{id}/ - Delete unit
    
    Filtering: ?is_active=true, ?must_be_whole_number=true
    Searching: ?search=query (name, short_code)
    Ordering: ?ordering=name
    
    Fields: id, name, short_code, target_unit, conversion_factor, 
            must_be_whole_number, is_active
    
    Note: target_unit is self-reference for unit conversion
    """
    queryset = Unit.objects.all()
    serializer_class = UnitSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'must_be_whole_number']
    search_fields = ['name', 'short_code']
    ordering_fields = ['name', 'id']
    ordering = ['name']


class AttributeTypeViewSet(viewsets.ModelViewSet):
    """
    ViewSet for AttributeType.
    
    Endpoints:
        GET /v1/api/attribute-types/ - List attribute types
        POST /v1/api/attribute-types/ - Create attribute type
        GET /v1/api/attribute-types/{id}/ - Retrieve
        PUT /v1/api/attribute-types/{id}/ - Update
        DELETE /v1/api/attribute-types/{id}/ - Delete
    
    Searching: ?search=query (name, description)
    Ordering: ?ordering=name
    
    Fields: id, name, description
    
    Example:
        AttributeType: Color
        AttributeType: Size
    """
    queryset = AttributeType.objects.all()
    serializer_class = AttributeTypeSerializer
    pagination_class = StandardPagination
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'id']
    ordering = ['name']


class AttributeValueViewSet(viewsets.ModelViewSet):
    """
    ViewSet for AttributeValue.
    
    Endpoints:
        GET /v1/api/attribute-values/ - List attribute values
        POST /v1/api/attribute-values/ - Create attribute value
        GET /v1/api/attribute-values/{id}/ - Retrieve
        PUT /v1/api/attribute-values/{id}/ - Update
        DELETE /v1/api/attribute-values/{id}/ - Delete
    
    Filtering: ?attribute_type=1
    Searching: ?search=query (value)
    Ordering: ?ordering=attribute_type, value
    
    Fields: id, attribute_type, value
    
    Example:
        attribute_type=Color: Red, Blue, Green
        attribute_type=Size: S, M, L, XL
    """
    queryset = AttributeValue.objects.all()
    serializer_class = AttributeValueSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['attribute_type']
    search_fields = ['value']
    ordering_fields = ['attribute_type', 'value']
    ordering = ['attribute_type', 'value']


class TaxCodeViewSet(viewsets.ModelViewSet):
    """
    ViewSet for TaxCode.
    
    Endpoints:
        GET /v1/api/tax-codes/ - List tax codes
        POST /v1/api/tax-codes/ - Create tax code
        GET /v1/api/tax-codes/{id}/ - Retrieve
        PUT /v1/api/tax-codes/{id}/ - Update
        DELETE /v1/api/tax-codes/{id}/ - Delete
    
    Nested: components (list of TaxComponent)
    
    Filtering: ?is_active=true, ?is_exempt=true, ?code_type=HSN
    Searching: ?search=query (name, code)
    Ordering: ?ordering=name, code
    
    Fields: id, name, code_type, code, is_exempt, is_active, components
    
    Creates with TaxComponents separately or nested.
    """
    queryset = TaxCode.objects.prefetch_related('components').all()
    serializer_class = TaxCodeSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'is_exempt', 'code_type']
    search_fields = ['name', 'code']
    ordering_fields = ['name', 'code_type', 'code']
    ordering = ['name']


class TaxComponentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for TaxComponent.
    
    Endpoints:
        GET /v1/api/tax-components/ - List components
        POST /v1/api/tax-components/ - Create component
        GET /v1/api/tax-components/{id}/ - Retrieve
        PUT /v1/api/tax-components/{id}/ - Update
        DELETE /v1/api/tax-components/{id}/ - Delete
    
    Filtering: ?tax_code=1, ?component=CGST
    
    Fields: id, tax_code, component, rate
    
    Typically creates under existing TaxCode.
    """
    queryset = TaxComponent.objects.all()
    serializer_class = TaxComponentSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['tax_code', 'component']


class ItemViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Item.
    
    Endpoints:
        GET /v1/api/items/ - List items
        POST /v1/api/items/ - Create item
        GET /v1/api/items/{id}/ - Retrieve item with variants/images
        PUT /v1/api/items/{id}/ - Update item
        DELETE /v1/api/items/{id}/ - Delete item
    
    Nested Endpoints:
        GET /v1/api/items/{id}/variants/ - List variants
        POST /v1/api/items/{id}/variants/ - Create variant
        GET /v1/api/items/{id}/images/ - List images
        POST /v1/api/items/{id}/images/ - Upload image
    
    Filtering: ?is_active=true, ?category=1, ?unit=1, ?has_variants=true
    Searching: ?search=query (name, sku, description)
    Ordering: ?ordering=name, sku, unit_price
    
    List Fields: id, name, sku, description, category, unit, tax_code,
                  cgst_rate, sgst_rate, igst_rate, has_variants,
                  current_stock, min_stock_level, max_stock_level,
                  unit_price, cost_price, is_active
    
    Detail Fields: All fields + nested category, unit, tax_code, variants, images
    
    Tax Rate Snapshot:
        When creating with tax_code:
        - Serializer extracts rates from TaxCode.components
        - Saves to Item.cgst_rate, sgst_rate, igst_rate
        - Persists even if TaxCode later changes
    """
    queryset = Item.objects.select_related('category', 'unit', 'tax_code').prefetch_related('variants', 'images').all()
    serializer_class = ItemListSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'category', 'unit', 'has_variants']
    search_fields = ['name', 'sku', 'description']
    ordering_fields = ['name', 'sku', 'created_at', 'unit_price']
    ordering = ['name']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ItemDetailSerializer
        return ItemListSerializer


class ItemVariantViewSet(viewsets.ModelViewSet):
    """
    ViewSet for ItemVariant.
    
    Endpoints:
        GET /v1/api/items/{item_id}/variants/ - List variants
        POST /v1/api/items/{item_id}/variants/ - Create variant
        GET /v1/api/items/{item_id}/variants/{id}/ - Retrieve
        PUT /v1/api/items/{item_id}/variants/{id}/ - Update
        DELETE /v1/api/items/{item_id}/variants/{id}/ - Delete
    
    Filtering: ?is_active=true
    Searching: ?search=query (sku)
    
    Fields: id, item, sku, unit_price, cost_price, current_stock, is_active, attributes
    
    Auto-Management:
        - First variant sets item.has_variants=True
        - First variant clears item.current_stock
        - Last variant delete sets item.has_variants=False
    """
    serializer_class = ItemVariantSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'item']
    search_fields = ['sku']
    ordering_fields = ['sku', 'id']
    ordering = ['sku']

    def get_queryset(self):
        item_id = self.kwargs.get('item_pk')
        if item_id:
            return ItemVariant.objects.filter(item_id=item_id).prefetch_related('attributes').all()
        return ItemVariant.objects.prefetch_related('attributes').all()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['item_id'] = self.kwargs.get('item_pk')
        return context


class ItemImageViewSet(viewsets.ModelViewSet):
    """
    ViewSet for ItemImage.
    
    Endpoints:
        GET /v1/api/items/{item_id}/images/ - List images
        POST /v1/api/items/{item_id}/images/ - Upload image
        GET /v1/api/items/{item_id}/images/{id}/ - Retrieve
        PUT /v1/api/items/{item_id}/images/{id}/ - Update
        DELETE /v1/api/items/{item_id}/images/{id}/ - Delete image
    
    Filtering: ?item=1, ?item_variant=1, ?is_primary=true
    
    WebP Conversion:
        - Images auto-converted to WebP on save
        - Quality: 85%
        - Original format preserved if conversion fails
    """
    serializer_class = ItemImageSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['item', 'item_variant', 'is_primary']

    def get_queryset(self):
        item_id = self.kwargs.get('item_pk')
        if item_id:
            return ItemImage.objects.filter(item_id=item_id).all()
        return ItemImage.objects.all()


class BatchViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Batch.
    
    Endpoints:
        GET /v1/api/batches/ - List batches
        POST /v1/api/batches/ - Create batch
        GET /v1/api/batches/{id}/ - Retrieve
        PUT /v1/api/batches/{id}/ - Update
        DELETE /v1/api/batches/{id}/ - Delete
    
    Filtering: ?item_variant=1
    Searching: ?search=query (batch_number)
    Ordering: ?ordering=received_date
    
    Fields: id, batch_number, item_variant, quantity_received,
            quantity_remaining, cost_per_unit, received_date, expiry_date
    
    Usage:
        - Created on purchase receipts
        - quantity_remaining decrements on sales
        - Used for FIFO/LIFO costing
    """
    queryset = Batch.objects.all()
    serializer_class = BatchSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['item_variant']
    search_fields = ['batch_number']
    ordering_fields = ['received_date', 'id']
    ordering = ['-received_date']


class OpeningStockViewSet(viewsets.ModelViewSet):
    """
    ViewSet for OpeningStock.
    
    Endpoints:
        GET /v1/api/opening-stock/ - List entries
        POST /v1/api/opening-stock/ - Create entry
        GET /v1/api/opening-stock/{id}/ - Retrieve
        PUT /v1/api/opening-stock/{id}/ - Update
        DELETE /v1/api/opening-stock/{id}/ - Delete
    
    Filtering: ?status=Pending, ?item=1
    Ordering: ?ordering=as_of_date
    
    Fields: id, item, item_variant, quantity, unit_cost,
            as_of_date, notes, status
    
    Workflow:
        1. Create with status=Pending
        2. Admin approves -> updates Item/Variant stock
        3. One-time use after approval
    """
    queryset = OpeningStock.objects.all()
    serializer_class = OpeningStockSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['status', 'item']
    ordering_fields = ['as_of_date', 'id']
    ordering = ['-as_of_date']


class StockMovementViewSet(viewsets.ModelViewSet):
    """
    ViewSet for StockMovement.
    
    Endpoints:
        GET /v1/api/stock-movements/ - List movements
        POST /v1/api/stock-movements/ - Create movement
        GET /v1/api/stock-movements/{id}/ - Retrieve
        PUT /v1/api/stock-movements/{id}/ - Update
        DELETE /v1/api/stock-movements/{id}/ - Delete
    
    Filtering: ?status=Pending, ?movement_type=Purchase, ?item=1
    Searching: ?search=query (reference_number, item__sku)
    Ordering: ?ordering=movement_date
    
    Fields: id, movement_type, item, item_variant, batch,
            quantity, rate, cgst_rate, sgst_rate, igst_rate,
            total_amount, reference_number, movement_date,
            status, notes
    
    Workflow:
        1. Create with status=Pending
        2. Admin approves -> updates stock
        3. If batch linked, decrements batch quantity
    
    Tax Snapshot:
        Rates captured at creation time.
    """
    queryset = StockMovement.objects.all()
    serializer_class = StockMovementSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'movement_type', 'item']
    search_fields = ['reference_number', 'item__sku']
    ordering_fields = ['movement_date', 'id']
    ordering = ['-movement_date']


class SerialNumberViewSet(viewsets.ModelViewSet):
    """ViewSet for SerialNumber."""
    from .models import SerialNumber
    from .serializers import SerialNumberSerializer
    
    queryset = SerialNumber.objects.all()
    serializer_class = SerialNumberSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'item', 'item_variant', 'warehouse']
    search_fields = ['serial_number', 'notes']
    ordering_fields = ['created_at', 'serial_number']
    ordering = ['-created_at']