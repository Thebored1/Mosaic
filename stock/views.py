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

from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from configuration.authentication import SUPER_ADMIN_MARKER, ECOMMERCE_MARKER, ScopedRolePermission
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
from .services import approve_opening_stock, post_existing_stock_movement, reject_opening_stock, reverse_stock_movement


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
