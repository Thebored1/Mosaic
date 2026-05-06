"""
Stock master API views.

This module exposes the inventory control surface used by org users and super
admins:

1. master data such as categories, units, tax codes, and attributes
2. item, variant, image, and batch management
3. opening stock, stock movement, and serial tracking workflows
4. organization-aware query scoping and write-time validation

The views here are intentionally opinionated because inventory is one of the
most tenancy-sensitive parts of the application.
"""

import csv
import io
from decimal import Decimal
from decimal import InvalidOperation
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Max, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.http import HttpResponse
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from account.models import Organization
from configuration.authentication import SUPER_ADMIN_MARKER, ECOMMERCE_MARKER, ScopedRolePermission
from configuration.models import Warehouse
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
    SerialNumberSerializer, StockTransferResponseSerializer
)
from .services import (
    approve_opening_stock,
    generate_barcode_svg,
    generate_qr_svg,
    post_existing_stock_movement,
    post_stock_movement,
    reject_opening_stock,
    reverse_stock_movement,
    transfer_stock_between_warehouses,
)


def csv_response(filename, rows):
    """Return a CSV response body for export endpoints."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow(row)
    response = HttpResponse(buffer.getvalue(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


class StandardPagination(PageNumberPagination):
    """
    Standard pagination for stock endpoints.

    Inventory master data tends to be browsed in admin grids and forms, so the
    default page size is intentionally small and client-adjustable.
    """
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


def org_filter(qs, request):
    """
    Filter a queryset by the organization from the auth context.

    Ecommerce-only accounts are never allowed to inspect stock master data.
    Super admins can optionally inspect a specific tenant by passing
    `organization` in the query string.
    """
    if not hasattr(request, 'auth') or request.auth is None:
        return qs.none()
    if request.auth == ECOMMERCE_MARKER:
        return qs.none()

    if request.auth == SUPER_ADMIN_MARKER:
        org_id = request.query_params.get('organization')
        if org_id:
            if hasattr(qs.model, '_meta') and any(f.name == 'organization' for f in qs.model._meta.get_fields()):
                return qs.filter(organization_id=org_id)
        return qs

    if hasattr(qs.model, '_meta') and any(f.name == 'organization' for f in qs.model._meta.get_fields()):
        return qs.filter(organization=request.auth)
    return qs


def save_for_request_organization(serializer, request):
    """
    Save a stock object into the organization resolved from the request.

    The helper is used by create/update handlers so the write path stays
    consistent across every stock viewset.
    """
    model_fields = {field.name for field in serializer.Meta.model._meta.get_fields()}
    if 'organization' not in model_fields:
        serializer.save()
        return

    org_id = request.data.get('organization') or request.query_params.get('organization')

    if request.auth == SUPER_ADMIN_MARKER:
        if not org_id:
            raise ValidationError({'organization': 'organization is required for super admin writes'})
        serializer.save(organization_id=org_id)
        return
    if request.auth == ECOMMERCE_MARKER:
        raise ValidationError({'organization': 'Create or join an organization to access this feature'})

    serializer.save(organization=request.auth)


def get_related_organization_id(related_obj):
    """
    Resolve the organization id for a related object.

    Several stock models relate to items, variants, or warehouses instead of
    having a direct organization field. This helper walks those relationships so
    write-time validation can still enforce tenant boundaries.
    """
    organization_id = getattr(related_obj, 'organization_id', None)
    if organization_id is not None:
        return organization_id

    if hasattr(related_obj, 'item_variant_id'):
        item_variant = getattr(related_obj, 'item_variant', None)
        organization_id = getattr(item_variant, 'organization_id', None)
        if organization_id is not None:
            return organization_id
        item = getattr(item_variant, 'item', None)
        return getattr(item, 'organization_id', None)

    if hasattr(related_obj, 'item_id'):
        item = getattr(related_obj, 'item', None)
        return getattr(item, 'organization_id', None)

    if hasattr(related_obj, 'warehouse_id'):
        warehouse = getattr(related_obj, 'warehouse', None)
        return getattr(warehouse, 'organization_id', None)

    return None


def validate_serializer_relations(serializer, request):
    """
    Validate nested or related objects before a stock write is saved.

    The serializer may contain foreign keys that point to objects outside the
    active organization. This check blocks those references before the model
    save runs.
    """
    if request.auth == SUPER_ADMIN_MARKER:
        return
    if request.auth == ECOMMERCE_MARKER:
        raise ValidationError({'organization': 'Create or join an organization to access this feature'})

    for field_name, value in serializer.validated_data.items():
        if isinstance(value, (list, dict)):
            continue

        related_organization_id = get_related_organization_id(value)
        if related_organization_id is not None and related_organization_id != request.auth.pk:
            raise ValidationError({field_name: f'{field_name} does not belong to the authenticated organization'})


class CategoryViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for inventory categories.

    Categories provide the primary grouping dimension for stock items and are
    tenant-scoped so each organization manages its own catalog taxonomy.
    """
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'stock_master'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'id']
    ordering = ['name']

    def get_queryset(self):
        """Return categories visible to the current organization."""
        return org_filter(Category.objects.all(), self.request)

    def perform_create(self, serializer):
        """Validate related objects and write the category under the tenant."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Validate related objects and update the category under the tenant."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)


class UnitViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for measurement units.

    Units define how stock quantities are interpreted and converted. They are
    shared across item creation, inventory validation, and purchasing flows.
    """
    queryset = Unit.objects.all()
    serializer_class = UnitSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'stock_master'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'must_be_whole_number']
    search_fields = ['name', 'short_code']
    ordering_fields = ['name', 'id']
    ordering = ['name']

    def get_queryset(self):
        """Return units visible to the current organization."""
        return org_filter(Unit.objects.all(), self.request)

    def perform_create(self, serializer):
        """Write a unit under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update a unit under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)


class AttributeTypeViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for variant attribute types.

    Attribute types describe the shape of item variants, such as color or size.
    """
    queryset = AttributeType.objects.all()
    serializer_class = AttributeTypeSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'stock_master'
    pagination_class = StandardPagination
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'id']
    ordering = ['name']

    def get_queryset(self):
        """Return attribute types visible to the current organization."""
        return org_filter(AttributeType.objects.all(), self.request)

    def perform_create(self, serializer):
        """Create an attribute type within the tenant scope."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update an attribute type within the tenant scope."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)


class AttributeValueViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for concrete attribute values.

    Values are attached to attribute types and later assigned to item variants.
    """
    queryset = AttributeValue.objects.all()
    serializer_class = AttributeValueSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'stock_master'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['attribute_type']
    search_fields = ['value']
    ordering_fields = ['attribute_type', 'value']
    ordering = ['attribute_type', 'value']

    def get_queryset(self):
        """Return attribute values visible to the current organization."""
        return org_filter(AttributeValue.objects.all(), self.request)

    def perform_create(self, serializer):
        """Create an attribute value within the tenant scope."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update an attribute value within the tenant scope."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)


class TaxCodeViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for tax codes.

    Tax codes carry the HSN/SAC metadata that item creation snapshots into
    stock records for historical consistency.
    """
    queryset = TaxCode.objects.prefetch_related('components').all()
    serializer_class = TaxCodeSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'stock_master'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'is_exempt', 'code_type']
    search_fields = ['name', 'code']
    ordering_fields = ['name', 'code_type', 'code']
    ordering = ['name']

    def get_queryset(self):
        """Return tax codes visible to the current organization."""
        return org_filter(TaxCode.objects.prefetch_related('components').all(), self.request)

    def perform_create(self, serializer):
        """Create a tax code after validating tenant ownership."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update a tax code after validating tenant ownership."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)


class TaxComponentViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for individual tax components.

    Components capture the CGST, SGST, and IGST breakdown for a tax code.
    """
    queryset = TaxComponent.objects.all()
    serializer_class = TaxComponentSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'stock_master'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['tax_code', 'component']

    def get_queryset(self):
        """Return tax components visible to the current organization."""
        return org_filter(TaxComponent.objects.all(), self.request)

    def perform_create(self, serializer):
        """Create a tax component after validating tenant ownership."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update a tax component after validating tenant ownership."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)


class ItemViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for inventory items.

    Items are the central stock master record. The list view exposes a compact
    payload while retrieve returns the full nested inventory context.
    """
    queryset = Item.objects.select_related('category', 'unit', 'tax_code').prefetch_related('variants', 'images').all()
    serializer_class = ItemListSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'stock_master'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'category', 'unit', 'has_variants']
    search_fields = ['name', 'sku', 'description']
    ordering_fields = ['name', 'sku', 'created_at', 'unit_price']
    ordering = ['name']

    def get_queryset(self):
        """Return items visible to the current organization."""
        return org_filter(
            Item.objects.select_related('category', 'unit', 'tax_code').prefetch_related('variants', 'images').all(),
            self.request
        )

    def get_serializer_class(self):
        """Use a compact serializer for lists and a nested serializer for detail."""
        if self.action == 'retrieve':
            return ItemDetailSerializer
        return ItemListSerializer

    def perform_create(self, serializer):
        """Create an item under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update an item under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export the visible item catalog as CSV."""
        rows = [['name', 'sku', 'category', 'unit', 'unit_price', 'cost_price', 'current_stock', 'min_stock_level', 'max_stock_level', 'is_active']]
        for item in self.get_queryset():
            rows.append([
                item.name,
                item.sku,
                item.category.name if item.category_id else '',
                item.unit.name if item.unit_id else '',
                str(item.unit_price),
                str(item.cost_price),
                str(item.current_stock if item.current_stock is not None else ''),
                str(item.min_stock_level),
                str(item.max_stock_level),
                str(item.is_active),
            ])
        return csv_response('items.csv', rows)

    @action(detail=False, methods=['get'])
    def import_template(self, request):
        """Return a CSV template for item imports."""
        rows = [[
            'name', 'sku', 'category', 'unit', 'tax_code', 'unit_price',
            'cost_price', 'current_stock', 'min_stock_level', 'max_stock_level',
            'is_active', 'has_variants', 'valuation_method', 'requires_serial_tracking'
        ]]
        return csv_response('items-template.csv', rows)

    @action(detail=False, methods=['post'])
    def bulk_import(self, request):
        """Validate and import item rows in a single transaction."""
        rows = request.data.get('items')
        if not isinstance(rows, list):
            raise ValidationError({'items': 'items must be a list of row objects'})

        org = request.auth if request.auth != SUPER_ADMIN_MARKER else None
        errors = []
        prepared = []

        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append({'row': index, 'error': 'Each row must be an object'})
                continue
            sku = (row.get('sku') or '').strip()
            name = (row.get('name') or '').strip()
            if not sku or not name:
                errors.append({'row': index, 'error': 'name and sku are required'})
                continue
            try:
                unit_price = Decimal(str(row.get('unit_price', '0')))
                cost_price = Decimal(str(row.get('cost_price', '0')))
                current_stock = row.get('current_stock')
                current_stock = Decimal(str(current_stock)) if current_stock not in {None, ''} else None
                category = None
                unit = None
                tax_code = None
                if row.get('category'):
                    category = Category.objects.get(pk=row['category'])
                if row.get('unit'):
                    unit = Unit.objects.get(pk=row['unit'])
                if row.get('tax_code'):
                    tax_code = TaxCode.objects.get(pk=row['tax_code'])
                prepared.append({
                    'row': index,
                    'sku': sku,
                    'name': name,
                    'category': category,
                    'unit': unit,
                    'tax_code': tax_code,
                    'unit_price': unit_price,
                    'cost_price': cost_price,
                    'current_stock': current_stock,
                    'min_stock_level': int(row.get('min_stock_level', 0)),
                    'max_stock_level': int(row.get('max_stock_level', 0)),
                    'is_active': str(row.get('is_active', True)).lower() not in {'false', '0', 'no'},
                    'has_variants': str(row.get('has_variants', False)).lower() in {'true', '1', 'yes'},
                    'valuation_method': row.get('valuation_method', 'FIFO'),
                    'requires_serial_tracking': str(row.get('requires_serial_tracking', False)).lower() in {'true', '1', 'yes'},
                })
            except Exception as exc:
                errors.append({'row': index, 'error': str(exc)})

        if errors:
            raise ValidationError({'rows': errors})

        imported = []
        with transaction.atomic():
            for row in prepared:
                defaults = {
                    'organization': org,
                    'name': row['name'],
                    'category': row['category'],
                    'unit': row['unit'],
                    'tax_code': row['tax_code'],
                    'unit_price': row['unit_price'],
                    'cost_price': row['cost_price'],
                    'current_stock': row['current_stock'],
                    'min_stock_level': row['min_stock_level'],
                    'max_stock_level': row['max_stock_level'],
                    'is_active': row['is_active'],
                    'has_variants': row['has_variants'],
                    'valuation_method': row['valuation_method'],
                    'requires_serial_tracking': row['requires_serial_tracking'],
                }
                item, _ = Item.objects.update_or_create(sku=row['sku'], defaults=defaults)
                imported.append(ItemListSerializer(item).data)

        return Response({'imported': imported}, status=201)

    @action(detail=True, methods=['get'])
    @extend_schema(responses=OpenApiTypes.STR)
    def barcode(self, request, pk=None):
        """Return a Code128 barcode SVG for the selected item."""
        item = self.get_object()
        svg = generate_barcode_svg(item.sku or item.name)
        return HttpResponse(svg, content_type='image/svg+xml')

    @action(detail=True, methods=['get'])
    @extend_schema(responses=OpenApiTypes.STR)
    def qr(self, request, pk=None):
        """Return a QR code SVG for the selected item."""
        item = self.get_object()
        payload = f'item:{item.pk}|sku:{item.sku}|name:{item.name}'
        svg = generate_qr_svg(payload)
        return HttpResponse(svg, content_type='image/svg+xml')


class ItemVariantViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for item variants.

    Variants are scoped beneath an item and are commonly used for color, size,
    or other sellable combinations.
    """
    queryset = ItemVariant.objects.prefetch_related('attributes').all()
    serializer_class = ItemVariantSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'stock_master'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'item']
    search_fields = ['sku']
    ordering_fields = ['sku', 'id']
    ordering = ['sku']

    def get_queryset(self):
        """Return variants visible to the current organization."""
        qs = org_filter(ItemVariant.objects.prefetch_related('attributes').all(), self.request)
        item_id = self.kwargs.get('item_pk')
        if item_id:
            return qs.filter(item_id=item_id)
        return qs

    def get_serializer_context(self):
        """Pass the parent item id to nested variant serializers."""
        context = super().get_serializer_context()
        context['item_id'] = self.kwargs.get('item_pk')
        return context

    def perform_create(self, serializer):
        """Create a variant under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update a variant under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)


class ItemImageViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for item images.

    Images can belong to either the parent item or a specific variant, which
    lets the frontend render catalogs and variant selectors with the right
    media assets.
    """
    queryset = ItemImage.objects.all()
    serializer_class = ItemImageSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'stock_master'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['item', 'item_variant', 'is_primary']

    def get_queryset(self):
        """Return images visible to the current organization."""
        qs = org_filter(ItemImage.objects.all(), self.request)
        item_id = self.kwargs.get('item_pk')
        if item_id:
            return qs.filter(item_id=item_id)
        return qs

    def perform_create(self, serializer):
        """Create an image under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update an image under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)


class BatchViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for inventory batches.

    Batches record lot-level inventory and are used for cost tracking and
    serial-number-adjacent workflows.
    """
    queryset = Batch.objects.all()
    serializer_class = BatchSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'inventory_control'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['item_variant']
    search_fields = ['batch_number']
    ordering_fields = ['received_date', 'id']
    ordering = ['-received_date']

    def get_queryset(self):
        """Return batches visible to the current organization."""
        return org_filter(Batch.objects.all(), self.request)

    def perform_create(self, serializer):
        """Create a batch under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update a batch under the tenant after relation validation."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)


class OpeningStockViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for opening stock entries.

    Opening stock is used to seed inventory when migrating from a prior system
    or when performing an initial stock count.
    """
    queryset = OpeningStock.objects.all()
    serializer_class = OpeningStockSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'inventory_control'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['status', 'item']
    ordering_fields = ['as_of_date', 'id']
    ordering = ['-as_of_date']

    def get_queryset(self):
        """Return opening stock entries visible to the current organization."""
        return org_filter(OpeningStock.objects.all(), self.request)

    def perform_create(self, serializer):
        """Create an opening stock record under the tenant."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update an opening stock record under the tenant."""
        validate_serializer_relations(serializer, self.request)
        save_for_request_organization(serializer, self.request)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve and post an opening stock record."""
        opening_stock = self.get_object()
        approve_opening_stock(opening_stock, approved_by=request.user)
        return Response(OpeningStockSerializer(opening_stock).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Reject an opening stock record without posting inventory."""
        opening_stock = self.get_object()
        reject_opening_stock(opening_stock, notes=request.data.get('notes', ''))
        return Response(OpeningStockSerializer(opening_stock).data)


class StockMovementViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for stock movements.

    Stock movements are the audit trail for inventory changes and are treated
    as tenant-scoped accounting evidence.
    """
    queryset = StockMovement.objects.all()
    serializer_class = StockMovementSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'inventory_control'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'posting_state', 'movement_type', 'item', 'warehouse']
    search_fields = ['reference_number', 'item__sku']
    ordering_fields = ['movement_date', 'id']
    ordering = ['-movement_date']

    def get_queryset(self):
        """Return stock movements visible to the current organization."""
        return org_filter(StockMovement.objects.all(), self.request)

    def perform_create(self, serializer):
        """Create a stock movement under the tenant."""
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update a stock movement under the tenant."""
        save_for_request_organization(serializer, self.request)

    @action(detail=True, methods=['post'])
    def post(self, request, pk=None):
        """Post a pending stock movement into inventory."""
        movement = self.get_object()
        post_existing_stock_movement(movement)
        return Response(StockMovementSerializer(movement).data)

    @action(detail=True, methods=['post'])
    def reverse(self, request, pk=None):
        """Reverse a posted stock movement."""
        movement = self.get_object()
        reversal = reverse_stock_movement(
            movement,
            reference_number=request.data.get('reference_number', ''),
            notes=request.data.get('notes', ''),
        )
        return Response(StockMovementSerializer(reversal).data, status=201)

    @action(detail=False, methods=['post'])
    @extend_schema(responses=StockTransferResponseSerializer)
    def transfer(self, request):
        """Move stock from one warehouse to another within the same organization."""
        data = request.data
        organization = request.auth
        if organization == SUPER_ADMIN_MARKER:
            org_id = data.get('organization') or request.query_params.get('organization')
            if not org_id:
                raise ValidationError({'organization': 'organization is required for super admin writes'})
            organization = get_object_or_404(Organization, pk=org_id)

        item_id = data.get('item')
        from_warehouse_id = data.get('from_warehouse')
        to_warehouse_id = data.get('to_warehouse')
        if not all([item_id, from_warehouse_id, to_warehouse_id]):
            raise ValidationError({'detail': 'item, from_warehouse, and to_warehouse are required.'})

        item = get_object_or_404(Item.objects.select_related('category', 'unit', 'tax_code'), pk=item_id)
        item_variant_id = data.get('item_variant')
        item_variant = None
        if item_variant_id:
            item_variant = get_object_or_404(ItemVariant, pk=item_variant_id)

        from_warehouse = get_object_or_404(Warehouse, pk=from_warehouse_id)
        to_warehouse = get_object_or_404(Warehouse, pk=to_warehouse_id)

        try:
            quantity = Decimal(str(data.get('quantity', '0')))
        except (InvalidOperation, TypeError):
            raise ValidationError({'quantity': 'Transfer quantity must be a valid number.'})
        if quantity <= 0:
            raise ValidationError({'quantity': 'Transfer quantity must be greater than zero.'})

        try:
            rate = Decimal(str(data.get('rate', item.cost_price or item.unit_price or Decimal('0'))))
        except (InvalidOperation, TypeError):
            raise ValidationError({'rate': 'Transfer rate must be a valid number.'})
        transfer = transfer_stock_between_warehouses(
            organization=organization,
            item=item,
            from_warehouse=from_warehouse,
            to_warehouse=to_warehouse,
            quantity=quantity,
            rate=rate,
            item_variant=item_variant,
            reference_number=data.get('reference_number', ''),
            notes=data.get('notes', ''),
        )
        return Response({
            'message': 'Stock transferred successfully',
            'out_movement': StockMovementSerializer(transfer['out_movement']).data,
            'in_movement': StockMovementSerializer(transfer['in_movement']).data,
        }, status=201)


class SerialNumberViewSet(viewsets.ModelViewSet):
    """
    CRUD viewset for serial-number tracked inventory.

    Serial numbers are used for high-value or regulated stock where each unit
    must be traced individually through warehouse and sale workflows.
    """
    queryset = SerialNumber.objects.all()
    serializer_class = SerialNumberSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'inventory_control'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'item', 'item_variant', 'warehouse']
    search_fields = ['serial_number', 'notes']
    ordering_fields = ['created_at', 'serial_number']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return serial numbers visible to the current organization."""
        return org_filter(SerialNumber.objects.all(), self.request)

    def perform_create(self, serializer):
        """Create a serial number under the tenant."""
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Update a serial number under the tenant."""
        save_for_request_organization(serializer, self.request)


@extend_schema_view(
    valuation=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    movement_summary=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    slow_moving=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    min_stock_alerts=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
)
class ReportsViewSet(viewsets.ViewSet):
    """Stock reports for valuation and exception monitoring."""

    permission_classes = [ScopedRolePermission]
    permission_scope = 'reporting'

    def get_queryset(self):
        return org_filter(Item.objects.all(), self.request)

    @action(detail=False, methods=['get'])
    def valuation(self, request):
        """Return a simple inventory valuation summary by item."""
        rows = []
        total_value = Decimal('0')
        for item in self.get_queryset().select_related('unit'):
            quantity = item.current_stock or Decimal('0')
            value = (quantity * item.cost_price).quantize(Decimal('0.01'))
            total_value += value
            rows.append({
                'item_id': item.id,
                'sku': item.sku,
                'name': item.name,
                'quantity': str(quantity),
                'cost_price': str(item.cost_price),
                'value': str(value),
            })
        return Response({'total_value': str(total_value), 'rows': rows})

    @action(detail=False, methods=['get'])
    def movement_summary(self, request):
        """Summarize posted stock movement activity by type."""
        movements = org_filter(StockMovement.objects.all(), request).values('movement_type').annotate(
            count=Count('id'),
            total_quantity=Sum('quantity'),
            last_movement=Max('movement_date'),
        ).order_by('movement_type')
        return Response({'rows': list(movements)})

    @action(detail=False, methods=['get'])
    def slow_moving(self, request):
        """Return items with stock but no recent sale activity."""
        days = int(request.query_params.get('days', '90'))
        cutoff = timezone.now() - timedelta(days=days)
        rows = []
        for item in self.get_queryset():
            if (item.current_stock or Decimal('0')) <= 0:
                continue
            last_sale = item.stock_movements.filter(movement_type='Sale').aggregate(last_sale=Max('movement_date'))['last_sale']
            if last_sale is None or last_sale < cutoff:
                rows.append({
                    'item_id': item.id,
                    'sku': item.sku,
                    'name': item.name,
                    'current_stock': str(item.current_stock or Decimal('0')),
                    'last_sale': last_sale.isoformat() if last_sale else None,
                })
        return Response({'rows': rows})

    @action(detail=False, methods=['get'])
    def min_stock_alerts(self, request):
        """Return items that are at or below minimum stock levels."""
        rows = []
        for item in self.get_queryset():
            quantity = item.current_stock or Decimal('0')
            if quantity <= Decimal(str(item.min_stock_level)):
                rows.append({
                    'item_id': item.id,
                    'sku': item.sku,
                    'name': item.name,
                    'current_stock': str(quantity),
                    'min_stock_level': item.min_stock_level,
                })
        return Response({'rows': rows})
