from rest_framework import viewsets, status
from rest_framework.response import Response
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
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'id']
    ordering = ['name']


class UnitViewSet(viewsets.ModelViewSet):
    queryset = Unit.objects.all()
    serializer_class = UnitSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'must_be_whole_number']
    search_fields = ['name', 'short_code']
    ordering_fields = ['name', 'id']
    ordering = ['name']


class AttributeTypeViewSet(viewsets.ModelViewSet):
    queryset = AttributeType.objects.all()
    serializer_class = AttributeTypeSerializer
    pagination_class = StandardPagination
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'id']
    ordering = ['name']


class AttributeValueViewSet(viewsets.ModelViewSet):
    queryset = AttributeValue.objects.all()
    serializer_class = AttributeValueSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['attribute_type']
    search_fields = ['value']
    ordering_fields = ['attribute_type', 'value']
    ordering = ['attribute_type', 'value']


class TaxCodeViewSet(viewsets.ModelViewSet):
    queryset = TaxCode.objects.prefetch_related('components').all()
    serializer_class = TaxCodeSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'is_exempt', 'code_type']
    search_fields = ['name', 'code']
    ordering_fields = ['name', 'code_type', 'code']
    ordering = ['name']


class TaxComponentViewSet(viewsets.ModelViewSet):
    queryset = TaxComponent.objects.all()
    serializer_class = TaxComponentSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['tax_code', 'component']


class ItemViewSet(viewsets.ModelViewSet):
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
    queryset = Batch.objects.all()
    serializer_class = BatchSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['item_variant']
    search_fields = ['batch_number']
    ordering_fields = ['received_date', 'id']
    ordering = ['-received_date']


class OpeningStockViewSet(viewsets.ModelViewSet):
    queryset = OpeningStock.objects.all()
    serializer_class = OpeningStockSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['status', 'item']
    ordering_fields = ['as_of_date', 'id']
    ordering = ['-as_of_date']


class StockMovementViewSet(viewsets.ModelViewSet):
    queryset = StockMovement.objects.all()
    serializer_class = StockMovementSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'movement_type', 'item']
    search_fields = ['reference_number', 'item__sku']
    ordering_fields = ['movement_date', 'id']
    ordering = ['-movement_date']