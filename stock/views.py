"""
Stock Master Views
===============

DRF viewsets for API endpoints.

Key Features:
- ViewSets for all models
- Filtering, searching, ordering
- Pagination (10 per page default)
- Custom nested endpoints for variants and images
- Organization filtering for multi-tenancy
"""

from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from .models import (
    Category, Unit, AttributeType, AttributeValue,
    TaxCode, TaxComponent,
    Item, ItemVariant, ItemVariantAttribute, Batch,
    ItemImage, OpeningStock, StockMovement, SerialNumber
)
from .serializers import (
    CategorySerializer, UnitSerializer,
    AttributeTypeSerializer, AttributeValueSerializer,
    TaxCodeSerializer, TaxComponentSerializer,
    ItemListSerializer, ItemDetailSerializer,
    ItemVariantSerializer, ItemVariantAttributeSerializer,
    ItemImageSerializer, BatchSerializer,
    OpeningStockSerializer, StockMovementSerializer,
    SerialNumberSerializer
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
    """ViewSet for Category."""
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'id']
    ordering = ['name']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return Category.objects.none()
        return Category.objects.filter(organization=self.request.auth)


class UnitViewSet(viewsets.ModelViewSet):
    """ViewSet for Unit."""
    queryset = Unit.objects.all()
    serializer_class = UnitSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'must_be_whole_number']
    search_fields = ['name', 'short_code']
    ordering_fields = ['name', 'id']
    ordering = ['name']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return Unit.objects.none()
        return Unit.objects.filter(organization=self.request.auth)


class AttributeTypeViewSet(viewsets.ModelViewSet):
    """ViewSet for AttributeType."""
    queryset = AttributeType.objects.all()
    serializer_class = AttributeTypeSerializer
    pagination_class = StandardPagination
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'id']
    ordering = ['name']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return AttributeType.objects.none()
        return AttributeType.objects.filter(organization=self.request.auth)


class AttributeValueViewSet(viewsets.ModelViewSet):
    """ViewSet for AttributeValue."""
    queryset = AttributeValue.objects.all()
    serializer_class = AttributeValueSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['attribute_type']
    search_fields = ['value']
    ordering_fields = ['attribute_type', 'value']
    ordering = ['attribute_type', 'value']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return AttributeValue.objects.none()
        return AttributeValue.objects.filter(organization=self.request.auth)


class TaxCodeViewSet(viewsets.ModelViewSet):
    """ViewSet for TaxCode."""
    queryset = TaxCode.objects.prefetch_related('components').all()
    serializer_class = TaxCodeSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'is_exempt', 'code_type']
    search_fields = ['name', 'code']
    ordering_fields = ['name', 'code_type', 'code']
    ordering = ['name']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return TaxCode.objects.none()
        return TaxCode.objects.filter(organization=self.request.auth).prefetch_related('components')


class TaxComponentViewSet(viewsets.ModelViewSet):
    """ViewSet for TaxComponent."""
    queryset = TaxComponent.objects.all()
    serializer_class = TaxComponentSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['tax_code', 'component']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return TaxComponent.objects.none()
        return TaxComponent.objects.filter(organization=self.request.auth)


class ItemViewSet(viewsets.ModelViewSet):
    """ViewSet for Item."""
    queryset = Item.objects.select_related('category', 'unit', 'tax_code').prefetch_related('variants', 'images').all()
    serializer_class = ItemListSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'category', 'unit', 'has_variants']
    search_fields = ['name', 'sku', 'description']
    ordering_fields = ['name', 'sku', 'created_at', 'unit_price']
    ordering = ['name']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return Item.objects.none()
        return Item.objects.filter(organization=self.request.auth).select_related('category', 'unit', 'tax_code').prefetch_related('variants', 'images')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ItemDetailSerializer
        return ItemListSerializer


class ItemVariantViewSet(viewsets.ModelViewSet):
    """ViewSet for ItemVariant."""
    queryset = ItemVariant.objects.prefetch_related('attributes').all()
    serializer_class = ItemVariantSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'item']
    search_fields = ['sku']
    ordering_fields = ['sku', 'id']
    ordering = ['sku']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return ItemVariant.objects.none()
        qs = ItemVariant.objects.filter(organization=self.request.auth).prefetch_related('attributes')
        item_id = self.kwargs.get('item_pk')
        if item_id:
            return qs.filter(item_id=item_id)
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['item_id'] = self.kwargs.get('item_pk')
        return context


class ItemImageViewSet(viewsets.ModelViewSet):
    """ViewSet for ItemImage."""
    queryset = ItemImage.objects.all()
    serializer_class = ItemImageSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['item', 'item_variant', 'is_primary']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return ItemImage.objects.none()
        qs = ItemImage.objects.filter(organization=self.request.auth)
        item_id = self.kwargs.get('item_pk')
        if item_id:
            return qs.filter(item_id=item_id)
        return qs


class BatchViewSet(viewsets.ModelViewSet):
    """ViewSet for Batch."""
    queryset = Batch.objects.all()
    serializer_class = BatchSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['item_variant']
    search_fields = ['batch_number']
    ordering_fields = ['received_date', 'id']
    ordering = ['-received_date']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return Batch.objects.none()
        return Batch.objects.filter(organization=self.request.auth)


class OpeningStockViewSet(viewsets.ModelViewSet):
    """ViewSet for OpeningStock."""
    queryset = OpeningStock.objects.all()
    serializer_class = OpeningStockSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['status', 'item']
    ordering_fields = ['as_of_date', 'id']
    ordering = ['-as_of_date']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return OpeningStock.objects.none()
        return OpeningStock.objects.filter(organization=self.request.auth)


class StockMovementViewSet(viewsets.ModelViewSet):
    """ViewSet for StockMovement."""
    queryset = StockMovement.objects.all()
    serializer_class = StockMovementSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'movement_type', 'item']
    search_fields = ['reference_number', 'item__sku']
    ordering_fields = ['movement_date', 'id']
    ordering = ['-movement_date']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return StockMovement.objects.none()
        return StockMovement.objects.filter(organization=self.request.auth)


class SerialNumberViewSet(viewsets.ModelViewSet):
    """ViewSet for SerialNumber."""
    queryset = SerialNumber.objects.all()
    serializer_class = SerialNumberSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'item', 'item_variant', 'warehouse']
    search_fields = ['serial_number', 'notes']
    ordering_fields = ['created_at', 'serial_number']
    ordering = ['-created_at']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return SerialNumber.objects.none()
        return SerialNumber.objects.filter(organization=self.request.auth)