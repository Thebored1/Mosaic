"""
Commerce API views for catalog, cart, addresses, and storefront orders.

This layer is intentionally split from the ERP/POS apps:

    - listing views expose seller-owned catalog entries to buyers
    - address views manage buyer address books
    - cart views manage the pre-checkout shopping state
    - order views expose the immutable checkout result

The authorization model follows the existing token and account system:
    - ecommerce-only accounts can use buyer-facing endpoints
    - org users can manage listings for their organization
    - super admins can inspect across organizations when required
"""

from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from decimal import Decimal
from datetime import date, datetime
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from django_filters.rest_framework import DjangoFilterBackend

from account.models import Organization
from configuration.authentication import ECOMMERCE_MARKER, SUPER_ADMIN_MARKER, ApiKeyPermission, ScopedRolePermission

from .models import (
    Cart,
    CartItem,
    CommerceAuditEvent,
    CommerceContentPage,
    CommerceListing,
    CommerceNotification,
    CommerceOrder,
    CommercePriceOverride,
    CommerceRefund,
    CommerceReturnRequest,
    CommerceSettings,
    CommerceShipment,
    MarketplacePayout,
    MarketplaceSettlement,
    InventoryReservation,
    ProductReview,
    ShopperAddress,
    Wishlist,
    WishlistItem,
    get_active_commerce_settings,
    get_listing_effective_price,
)
from .serializers import (
    CartCheckoutSerializer,
    CartItemSerializer,
    CartSerializer,
    CommerceListingSerializer,
    CommerceAuditEventSerializer,
    CommerceContentPageSerializer,
    CommerceOrderSerializer,
    CommercePriceOverrideSerializer,
    CommerceRefundSerializer,
    CommerceReturnRequestSerializer,
    CommerceSettingsSerializer,
    CommerceShipmentSerializer,
    CommerceNotificationSerializer,
    InventoryReservationSerializer,
    MarketplacePayoutSerializer,
    MarketplaceSettlementSerializer,
    ProductReviewSerializer,
    WishlistItemSerializer,
    WishlistSerializer,
    ShopperAddressSerializer,
)


def current_account(request):
    """
    Return the authenticated user account for commerce endpoints.

    Commerce APIs rely on the application-level UserAccount rather than the
    raw Django auth user because tenancy, account type, and role live there.
    """
    account = getattr(request.user, 'account', None)
    if account is None or not account.is_active:
        raise ValidationError({'account': 'Active user account required.'})
    return account


def save_listing_for_request(serializer, request):
    """
    Attach the correct organization on listing writes.

    Seller-facing writes are organization-scoped. Super admins must explicitly
    choose the target organization so cross-tenant writes are never implicit.
    """
    organization = request.auth
    if request.auth == SUPER_ADMIN_MARKER:
        org = serializer.validated_data.get('organization')
        if org is None:
            raise ValidationError({'organization_id': 'organization_id is required for super admin writes.'})
        serializer.save(organization=org)
        return
    serializer.save(organization=organization)


def record_audit_event(request, action, entity, details=None):
    """
    Persist a commerce audit trail entry for a mutating request.

    The audit row records the authenticated principal and enough context for
    support staff to reconstruct what happened without inspecting request logs.
    """
    def sanitize(value):
        """Convert model and datetime values into JSON-safe primitives."""
        if value is None:
            return None
        if hasattr(value, '_meta') and hasattr(value, 'pk'):
            return value.pk
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [sanitize(item) for item in value]
        return value

    account = getattr(request.user, 'account', None)
    organization = getattr(entity, 'organization', None) or getattr(account, 'organization', None)
    if organization is None and getattr(entity, 'order', None) is not None:
        organization = getattr(entity.order, 'organization', None)
    if organization is None and getattr(entity, 'listing', None) is not None:
        organization = getattr(entity.listing, 'organization', None)
    if organization is None and getattr(entity, 'order_line', None) is not None:
        organization = getattr(entity.order_line, 'organization', None)
    CommerceAuditEvent.objects.create(
        actor_user=getattr(request, 'user', None) if getattr(request, 'user', None) else None,
        actor_account=account,
        organization=organization,
        action=action,
        entity_type=entity.__class__.__name__ if entity is not None else 'Unknown',
        entity_id=str(getattr(entity, 'pk', '') or ''),
        details=sanitize(details or {}),
        ip_address=request.META.get('REMOTE_ADDR'),
        user_agent=request.META.get('HTTP_USER_AGENT', ''),
    )


def resolve_request_organization(request):
    """
    Resolve the seller organization from the current request.

    Regular org users are bound to their account organization. Super admins
    must pass an explicit `organization` parameter to avoid implicit cross-
    tenant writes.
    """
    if request.auth == SUPER_ADMIN_MARKER:
        org_id = request.data.get('organization') or request.query_params.get('organization')
        if not org_id:
            raise ValidationError({'organization': 'organization is required for this operation.'})
        try:
            return Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist as exc:
            raise ValidationError({'organization': 'Selected organization does not exist.'}) from exc

    account = current_account(request)
    if account.organization_id is None:
        raise ValidationError({'organization': 'Create or join an organization to access this feature.'})
    return account.organization


class CommerceListingViewSet(viewsets.ModelViewSet):
    """
    Browse public listings and manage seller-owned listings.

    Buyers can use the read side to render catalogs. Sellers can create, update,
    and delete their own listings. Super admins retain full visibility for
    support and platform operations.
    """

    serializer_class = CommerceListingSerializer
    filterset_fields = ['organization', 'item', 'item_variant', 'item__category', 'is_active']
    search_fields = ['title', 'description', 'item__name', 'item__sku', 'item_variant__sku']
    ordering_fields = ['title', 'price', 'created_at', 'updated_at']
    ordering = ['title', 'id']

    def get_permissions(self):
        """Use role-scoped permissions for mutating listing operations."""
        if self.action in {'create', 'update', 'partial_update', 'destroy'}:
            self.permission_scope = 'commerce_management'
            return [ScopedRolePermission()]
        return [ApiKeyPermission()]

    def get_queryset(self):
        """
        Return the correct listing subset for the current request context.

        Buyers see active listings filtered by channel. Sellers can query their
        own organization listings with `?mine=true`. Super admins can inspect
        all data and optionally filter by organization.
        """
        queryset = CommerceListing.objects.select_related('organization', 'item', 'item_variant').all()

        if self.action in {'create', 'update', 'partial_update', 'destroy'}:
            if self.request.auth == SUPER_ADMIN_MARKER:
                return queryset
            if self.request.auth == ECOMMERCE_MARKER:
                return queryset.none()
            return queryset.filter(organization=self.request.auth)

        if self.request.auth == SUPER_ADMIN_MARKER:
            return queryset

        mine = self.request.query_params.get('mine', '').lower() in {'1', 'true', 'yes'}
        if mine and self.request.auth != ECOMMERCE_MARKER:
            return queryset.filter(organization=self.request.auth)

        queryset = queryset.filter(is_active=True)
        channel = self.request.query_params.get('channel')
        if channel == 'b2b':
            queryset = queryset.filter(is_b2b_enabled=True)
        elif channel == 'marketplace':
            queryset = queryset.filter(is_marketplace_enabled=True)
        else:
            queryset = queryset.filter(is_b2c_enabled=True)

        return queryset

    def perform_create(self, serializer):
        """Persist a new listing under the authenticated seller organization."""
        save_listing_for_request(serializer, self.request)
        record_audit_event(self.request, 'commerce.listing.create', serializer.instance, {'title': serializer.instance.title})

    def perform_update(self, serializer):
        """Persist listing changes under the authenticated seller organization."""
        save_listing_for_request(serializer, self.request)
        record_audit_event(self.request, 'commerce.listing.update', serializer.instance, {'title': serializer.instance.title})


class ShopperAddressViewSet(viewsets.ModelViewSet):
    """
    CRUD buyer addresses for the authenticated account.

    These endpoints are only meaningful for buyer-style accounts. The address
    book is private to the account and never exposed to other users.
    """

    serializer_class = ShopperAddressSerializer
    permission_classes = [ApiKeyPermission]
    ordering_fields = ['updated_at', 'created_at', 'city']
    ordering = ['-updated_at']

    def get_queryset(self):
        """Return only the authenticated account's saved addresses."""
        if self.request.auth == SUPER_ADMIN_MARKER:
            return ShopperAddress.objects.none()
        account = current_account(self.request)
        return ShopperAddress.objects.filter(user_account=account)

    def perform_create(self, serializer):
        """Bind a new address to the authenticated account."""
        serializer.save(user_account=current_account(self.request))
        record_audit_event(self.request, 'commerce.address.create', serializer.instance, {'label': serializer.instance.label})


class CartViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    Expose the current active cart and checkout flow.

    The cart is intentionally modeled as a single active document per account
    and channel so the frontend can request it repeatedly without managing
    local cart identifiers.
    """

    serializer_class = CartSerializer
    permission_classes = [ApiKeyPermission]

    def get_queryset(self):
        """Return the active cart for the authenticated account and channel."""
        if self.request.auth == SUPER_ADMIN_MARKER:
            return Cart.objects.none()
        account = current_account(self.request)
        channel = self.request.query_params.get('channel', 'b2c')
        return Cart.objects.filter(user_account=account, status='open', channel=channel).prefetch_related(
            'items', 'items__listing', 'items__listing__organization', 'items__listing__item', 'items__listing__item_variant'
        )

    def list(self, request, *args, **kwargs):
        """Return the active cart, creating one when necessary."""
        account = current_account(request)
        channel = request.query_params.get('channel', 'b2c')
        cart = Cart.current_for_account(account, channel=channel)
        serializer = self.get_serializer(cart)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def checkout(self, request):
        """
        Convert the active cart into a storefront order.

        Checkout is deliberately server-side so the cart snapshot becomes an
        immutable order document and the frontend cannot mutate totals after the
        final confirmation step.
        """
        if request.auth == SUPER_ADMIN_MARKER:
            raise ValidationError({'account': 'Super admin tokens do not have shopper carts.'})

        account = current_account(request)
        channel = request.data.get('channel') or request.query_params.get('channel') or 'b2c'
        cart = Cart.current_for_account(account, channel=channel)
        serializer = CartCheckoutSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        order = CommerceOrder.create_from_cart(
            cart,
            billing_address=serializer.validated_data.get('billing_address'),
            shipping_address=serializer.validated_data.get('shipping_address'),
            notes=serializer.validated_data.get('notes', ''),
        )
        response_serializer = CommerceOrderSerializer(order, context={'request': request})
        record_audit_event(request, 'commerce.order.checkout', order, {'channel': channel})
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class CartItemViewSet(viewsets.ModelViewSet):
    """
    Manage line items inside the current active cart.

    Cart items support the common e-commerce UX of add, update quantity, and
    remove without requiring the client to manage cart IDs or line sequencing.
    """

    serializer_class = CartItemSerializer
    permission_classes = [ApiKeyPermission]
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        """Return the line items that belong to the current active cart."""
        if self.request.auth == SUPER_ADMIN_MARKER:
            return CartItem.objects.none()
        account = current_account(self.request)
        channel = self.request.query_params.get('channel', 'b2c')
        cart = Cart.current_for_account(account, channel=channel)
        return CartItem.objects.filter(cart=cart).select_related(
            'listing', 'listing__organization', 'listing__item', 'listing__item_variant'
        )

    def create(self, request, *args, **kwargs):
        """
        Add a listing to the active cart or update its quantity if it exists.

        This keeps the API idempotent from the client's point of view: adding
        the same listing again simply changes the quantity on the current line.
        """
        account = current_account(request)
        channel = request.data.get('channel') or request.query_params.get('channel') or 'b2c'
        cart = Cart.current_for_account(account, channel=channel)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        listing = serializer.validated_data['listing']
        quantity = serializer.validated_data['quantity']

        cart_item, created = CartItem.objects.get_or_create(
            cart=cart,
            listing=listing,
            defaults={'quantity': quantity, 'unit_price': get_listing_effective_price(listing, account)},
        )
        if not created:
            cart_item.quantity = quantity
            cart_item.unit_price = get_listing_effective_price(listing, account)
            cart_item.save()

        output = self.get_serializer(cart_item)
        record_audit_event(request, 'commerce.cart_item.create', cart_item, {'listing': listing.title, 'quantity': str(quantity)})
        return Response(output.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def perform_update(self, serializer):
        """Persist cart item edits and trigger cart total recalculation."""
        serializer.save()


class CommerceOrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    List and retrieve storefront orders for the authenticated buyer.

    Buyers can inspect their own order history. Super admins can view all
    orders across organizations for support and operations.
    """

    serializer_class = CommerceOrderSerializer
    permission_classes = [ApiKeyPermission]
    ordering_fields = ['created_at', 'placed_at', 'grand_total']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return the correct order history for the current principal."""
        if self.request.auth == SUPER_ADMIN_MARKER:
            return CommerceOrder.objects.all().prefetch_related('lines', 'lines__organization')
        account = current_account(self.request)
        return CommerceOrder.objects.filter(user_account=account).prefetch_related('lines', 'lines__organization')

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """
        Cancel a storefront order before shipment.

        Cancellation releases any stock reservations and is intentionally
        blocked once the shipment has been marked as shipped.
        """
        order = self.get_object()
        notes = request.data.get('notes', '')
        try:
            order.cancel_order(notes=notes)
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        record_audit_event(request, 'commerce.order.cancel', order, {'notes': notes})
        return Response(CommerceOrderSerializer(order, context={'request': request}).data)


class SellerOrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    List and retrieve storefront orders that contain this seller's listings.

    This is the seller-side fulfillment view. It lets an organization inspect
    only the order lines that belong to its own catalog entries.
    """

    serializer_class = CommerceOrderSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'commerce_management'
    ordering_fields = ['created_at', 'placed_at', 'grand_total']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return orders that contain at least one line for the seller."""
        queryset = CommerceOrder.objects.prefetch_related('lines', 'lines__organization').distinct()
        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            if org_id:
                return queryset.filter(lines__organization_id=org_id)
            return queryset
        return queryset.filter(lines__organization=self.request.auth)

    def get_serializer_context(self):
        """Mark the serializer context so it can filter lines to seller-owned rows."""
        context = super().get_serializer_context()
        context['seller_view'] = True
        return context


class CommerceSettingsViewSet(mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """
    Retrieve and update seller-facing commerce settings.

    This exposes the operational controls that affect checkout, reservation,
    fulfillment, and buyer-specific pricing behavior.
    """

    serializer_class = CommerceSettingsSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'commerce_settings'

    def get_object(self):
        """Return the settings row for the current seller organization."""
        org = resolve_request_organization(self.request)
        if self.request.auth == SUPER_ADMIN_MARKER and not hasattr(org, 'pk'):
            raise ValidationError({'organization': 'Valid organization required.'})
        settings, _ = CommerceSettings.objects.get_or_create(organization=org)
        return settings

    def perform_update(self, serializer):
        """Persist settings updates and record an audit trail event."""
        instance = serializer.save()
        record_audit_event(self.request, 'commerce.settings.update', instance, serializer.validated_data)


class CommercePriceOverrideViewSet(viewsets.ModelViewSet):
    """
    Manage buyer-specific B2B price overrides.

    These overrides let a seller define negotiated pricing for a specific
    buyer organization without changing the public listing price.
    """

    serializer_class = CommercePriceOverrideSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'commerce_pricing'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['seller_organization', 'buyer_organization', 'listing', 'is_active']
    search_fields = ['listing__title', 'buyer_organization__name']
    ordering_fields = ['created_at', 'updated_at', 'price']
    ordering = ['-updated_at']

    def get_queryset(self):
        """Return price overrides for the current seller or requested tenant."""
        queryset = CommercePriceOverride.objects.select_related('seller_organization', 'buyer_organization', 'listing').all()
        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            if org_id:
                return queryset.filter(seller_organization_id=org_id)
            return queryset
        return queryset.filter(seller_organization=self.request.auth)

    def perform_create(self, serializer):
        """Create a price override under the seller organization."""
        seller_org = resolve_request_organization(self.request)
        buyer_org = serializer.validated_data.get('buyer_organization')
        listing = serializer.validated_data.get('listing')
        if listing is not None and listing.organization_id != seller_org.id:
            raise ValidationError({'listing': 'Listing must belong to the seller organization.'})
        instance = serializer.save(seller_organization=seller_org)
        record_audit_event(self.request, 'commerce.price_override.create', instance, serializer.validated_data)

    def perform_update(self, serializer):
        """Update a buyer-specific price override and record the change."""
        instance = serializer.save()
        record_audit_event(self.request, 'commerce.price_override.update', instance, serializer.validated_data)


class CommerceShipmentViewSet(viewsets.ModelViewSet):
    """
    Manage manual fulfillment records for storefront orders.

    Sellers use this surface to pack, ship, and deliver orders without a third
    party logistics integration. Shipment updates are also where reserved stock
    gets consumed.
    """

    serializer_class = CommerceShipmentSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['organization', 'order', 'method', 'status']
    search_fields = ['order__order_number', 'tracking_number', 'carrier_name']
    ordering_fields = ['created_at', 'updated_at', 'shipped_at']
    ordering = ['-created_at']

    def get_permissions(self):
        """Allow buyer reads but require seller authorization for mutations."""
        if self.action in {'create', 'update', 'partial_update', 'destroy', 'pack', 'ship', 'deliver', 'cancel'}:
            self.permission_scope = 'commerce_fulfillment'
            return [ScopedRolePermission()]
        return [ApiKeyPermission()]

    def get_queryset(self):
        """Return shipment rows visible to the current principal."""
        queryset = CommerceShipment.objects.select_related('organization', 'order').all()
        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            return queryset.filter(organization_id=org_id) if org_id else queryset

        if self.request.auth == ECOMMERCE_MARKER:
            account = current_account(self.request)
            return queryset.filter(order__user_account=account)

        return queryset.filter(organization=self.request.auth)

    def perform_create(self, serializer):
        """Create a shipment for the seller organization."""
        org = resolve_request_organization(self.request)
        order = serializer.validated_data.get('order')
        if order is not None and order.lines.exclude(organization=org).exists():
            raise ValidationError({'order': 'All order lines must belong to the seller organization.'})
        instance = serializer.save(organization=org)
        record_audit_event(self.request, 'commerce.shipment.create', instance, serializer.validated_data)

    @action(detail=True, methods=['post'])
    def pack(self, request, pk=None):
        """Mark the shipment as packed."""
        shipment = self.get_object()
        try:
            shipment.mark_packed(notes=request.data.get('notes', ''))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        record_audit_event(request, 'commerce.shipment.pack', shipment, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(shipment).data)

    @action(detail=True, methods=['post'])
    def ship(self, request, pk=None):
        """Mark the shipment as shipped and consume the reserved inventory."""
        shipment = self.get_object()
        try:
            shipment.mark_shipped(notes=request.data.get('notes', ''))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        record_audit_event(request, 'commerce.shipment.ship', shipment, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(shipment).data)

    @action(detail=True, methods=['post'])
    def deliver(self, request, pk=None):
        """Mark the shipment as delivered."""
        shipment = self.get_object()
        try:
            shipment.mark_delivered(notes=request.data.get('notes', ''))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        record_audit_event(request, 'commerce.shipment.deliver', shipment, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(shipment).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel the shipment before it leaves the warehouse."""
        shipment = self.get_object()
        try:
            shipment.cancel(notes=request.data.get('notes', ''))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        record_audit_event(request, 'commerce.shipment.cancel', shipment, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(shipment).data)


class CommerceReturnRequestViewSet(viewsets.ModelViewSet):
    """
    Manage buyer return requests and seller-side receipt processing.

    Buyers can create returns for shipped or delivered orders. Sellers can mark
    returns as received and process them once the items are physically back.
    """

    serializer_class = CommerceReturnRequestSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['organization', 'order', 'shipment', 'order_line', 'status']
    search_fields = ['reason', 'order__order_number', 'order_line__title']
    ordering_fields = ['requested_at', 'received_at', 'processed_at']
    ordering = ['-requested_at']

    def get_permissions(self):
        """Allow buyers to submit return requests and sellers to manage them."""
        if self.action in {'create', 'list', 'retrieve'}:
            return [ApiKeyPermission()]
        self.permission_scope = 'commerce_after_sales'
        return [ScopedRolePermission()]

    def get_queryset(self):
        """Return return requests visible to the current principal."""
        queryset = CommerceReturnRequest.objects.select_related('organization', 'order', 'shipment', 'order_line').all()
        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            return queryset.filter(organization_id=org_id) if org_id else queryset
        if self.request.auth == ECOMMERCE_MARKER:
            account = current_account(self.request)
            return queryset.filter(order__user_account=account)
        return queryset.filter(organization=self.request.auth)

    def perform_create(self, serializer):
        """Create a return request for the buyer's own shipped order."""
        account = current_account(self.request)
        order = serializer.validated_data.get('order')
        order_line = serializer.validated_data.get('order_line')
        if order.user_account_id != account.id:
            raise ValidationError({'order': 'Return request must belong to the authenticated account.'})
        if order_line.order_id != order.id:
            raise ValidationError({'order_line': 'Return line must belong to the selected order.'})
        instance = serializer.save(organization=order_line.organization)
        record_audit_event(self.request, 'commerce.return.create', instance, serializer.validated_data)

    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        """Mark the return as physically received by the seller."""
        return_request = self.get_object()
        try:
            return_request.mark_received(notes=request.data.get('notes', ''))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        record_audit_event(request, 'commerce.return.receive', return_request, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(return_request).data)

    @action(detail=True, methods=['post'])
    def process(self, request, pk=None):
        """Restock the returned items after the seller has received them."""
        return_request = self.get_object()
        try:
            return_request.process(notes=request.data.get('notes', ''))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        record_audit_event(request, 'commerce.return.process', return_request, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(return_request).data)


class CommerceRefundViewSet(viewsets.ModelViewSet):
    """
    Manage manual refund records after a return request is received.

    Refunds are kept separate from returns so finance staff can approve or
    process them later without changing the shipment lifecycle.
    """

    serializer_class = CommerceRefundSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'commerce_after_sales'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['organization', 'order', 'return_request', 'status']
    search_fields = ['order__order_number']
    ordering_fields = ['created_at', 'processed_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return refunds visible to the current principal."""
        queryset = CommerceRefund.objects.select_related('organization', 'order', 'return_request').all()
        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            return queryset.filter(organization_id=org_id) if org_id else queryset
        return queryset.filter(organization=self.request.auth)

    def perform_create(self, serializer):
        """Create a refund under the seller organization."""
        return_request = serializer.validated_data.get('return_request')
        org = return_request.organization
        instance = serializer.save(organization=org)
        record_audit_event(self.request, 'commerce.refund.create', instance, serializer.validated_data)

    @action(detail=True, methods=['post'])
    def process(self, request, pk=None):
        """Mark the refund as processed by finance or the seller."""
        refund = self.get_object()
        try:
            refund.mark_processed(notes=request.data.get('notes', ''))
        except DjangoValidationError as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        record_audit_event(request, 'commerce.refund.process', refund, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(refund).data)


class WishlistViewSet(viewsets.GenericViewSet, mixins.ListModelMixin):
    """
    Expose the buyer's wishlists and the current default wishlist.

    The frontend can use this as a lightweight saved-items surface before or
    after checkout.
    """

    serializer_class = WishlistSerializer
    permission_classes = [ApiKeyPermission]

    def get_queryset(self):
        """Return wishlists owned by the authenticated account."""
        if self.request.auth == SUPER_ADMIN_MARKER:
            return Wishlist.objects.none()
        account = current_account(self.request)
        return Wishlist.objects.filter(user_account=account).prefetch_related('items', 'items__listing')

    def list(self, request, *args, **kwargs):
        """Return the current default wishlist for the buyer."""
        account = current_account(request)
        channel = request.query_params.get('channel', 'b2c')
        wishlist = Wishlist.current_for_account(account, channel=channel)
        return Response(self.get_serializer(wishlist).data)


class WishlistItemViewSet(viewsets.ModelViewSet):
    """
    Manage saved listings inside the buyer's wishlist.

    Items can be added and removed independently from the cart so the shopper
    has a persistent planning surface.
    """

    serializer_class = WishlistItemSerializer
    permission_classes = [ApiKeyPermission]
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        """Return wishlist items belonging to the authenticated account."""
        if self.request.auth == SUPER_ADMIN_MARKER:
            return WishlistItem.objects.none()
        account = current_account(self.request)
        return WishlistItem.objects.filter(wishlist__user_account=account).select_related('wishlist', 'listing')

    def create(self, request, *args, **kwargs):
        """Add a listing to the buyer's current wishlist."""
        account = current_account(request)
        channel = request.data.get('channel') or request.query_params.get('channel') or 'b2c'
        wishlist = Wishlist.current_for_account(account, channel=channel)
        listing_id = request.data.get('listing') or request.data.get('listing_id')
        listing = CommerceListing.objects.get(pk=listing_id)
        item, created = WishlistItem.objects.get_or_create(wishlist=wishlist, listing=listing)
        record_audit_event(request, 'commerce.wishlist_item.create', item, {'listing': listing.title})
        return Response(self.get_serializer(item).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        """Remove a listing from the wishlist."""
        instance = self.get_object()
        record_audit_event(request, 'commerce.wishlist_item.delete', instance, {'listing': instance.listing.title})
        return super().destroy(request, *args, **kwargs)


class ProductReviewViewSet(viewsets.ModelViewSet):
    """
    Manage buyer reviews for commerce listings.

    Reviews are buyer-authored social proof. They are visible publicly only
    after approval, but the authenticated buyer can always inspect their own.
    """

    serializer_class = ProductReviewSerializer
    permission_classes = [ApiKeyPermission]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['listing', 'is_approved', 'rating']
    search_fields = ['title', 'body']
    ordering_fields = ['created_at', 'rating']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return approved reviews plus the authenticated buyer's own reviews."""
        if self.request.auth == SUPER_ADMIN_MARKER:
            return ProductReview.objects.select_related('listing', 'user_account').all()

        account = current_account(self.request)
        queryset = ProductReview.objects.select_related('listing', 'user_account').filter(is_approved=True)
        mine = self.request.query_params.get('mine', '').lower() in {'1', 'true', 'yes'}
        if mine:
            return ProductReview.objects.select_related('listing', 'user_account').filter(user_account=account)
        listing_id = self.request.query_params.get('listing')
        if listing_id:
            return queryset.filter(listing_id=listing_id)
        return queryset

    def perform_create(self, serializer):
        """Create a buyer review tied to the authenticated account."""
        account = current_account(self.request)
        instance = serializer.save(user_account=account)
        record_audit_event(self.request, 'commerce.review.create', instance, {'rating': instance.rating})


class CommerceContentPageViewSet(viewsets.ModelViewSet):
    """
    Manage public storefront content pages.

    Sellers can publish home, about, policy, FAQ, and custom pages for the
    web frontend. Buyers only see pages marked as published.
    """

    serializer_class = CommerceContentPageSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['organization', 'page_type', 'is_published']
    search_fields = ['slug', 'title', 'body']
    ordering_fields = ['title', 'created_at', 'updated_at']
    ordering = ['title']

    def get_permissions(self):
        """Allow read access to published pages and restrict writes to sellers."""
        if self.action in {'create', 'update', 'partial_update', 'destroy'}:
            self.permission_scope = 'commerce_content'
            return [ScopedRolePermission()]
        return [ApiKeyPermission()]

    def get_queryset(self):
        """Return storefront pages visible to the current request context."""
        queryset = CommerceContentPage.objects.select_related('organization').all()
        org_id = self.request.query_params.get('organization')
        if org_id:
            queryset = queryset.filter(organization_id=org_id)
        if self.request.auth == SUPER_ADMIN_MARKER:
            if org_id:
                return queryset
            return queryset
        if self.request.auth == ECOMMERCE_MARKER:
            return queryset.filter(is_published=True)
        mine = self.request.query_params.get('mine', '').lower() in {'1', 'true', 'yes'}
        if mine:
            return queryset.filter(organization=self.request.auth)
        return queryset.filter(organization=self.request.auth, is_published=True)

    def perform_create(self, serializer):
        """Create a storefront page for the authenticated seller organization."""
        org = resolve_request_organization(self.request)
        instance = serializer.save(organization=org)
        record_audit_event(self.request, 'commerce.content.create', instance, {'slug': instance.slug})

    def perform_update(self, serializer):
        """Update a storefront page and record the change."""
        instance = serializer.save()
        record_audit_event(self.request, 'commerce.content.update', instance, {'slug': instance.slug})


class CommerceNotificationViewSet(viewsets.ModelViewSet):
    """
    Expose buyer notifications for order and commerce events.

    Notifications are read-write for the owning account but only listable to
    that account or platform operators.
    """

    serializer_class = CommerceNotificationSerializer
    permission_classes = [ApiKeyPermission]
    http_method_names = ['get', 'patch', 'put', 'head', 'options']
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['notification_type', 'is_read']
    ordering_fields = ['created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return notifications for the authenticated buyer or platform operator."""
        if self.request.auth == SUPER_ADMIN_MARKER:
            return CommerceNotification.objects.select_related('user_account', 'organization').all()
        account = current_account(self.request)
        return CommerceNotification.objects.filter(user_account=account).select_related('user_account', 'organization')

    def perform_update(self, serializer):
        """Allow the owning account to mark notifications as read."""
        instance = serializer.save()
        if instance.is_read and instance.read_at is None:
            instance.read_at = timezone.now()
            instance.save(update_fields=['read_at'])
        record_audit_event(self.request, 'commerce.notification.update', instance, {'is_read': instance.is_read})


class CommerceAuditEventViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Read the commerce audit trail for support and seller operations.

    Audit rows are intentionally read-only through the API so the trail remains
    immutable.
    """

    serializer_class = CommerceAuditEventSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'commerce_audit'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['action', 'entity_type', 'organization']
    search_fields = ['action', 'entity_type', 'entity_id']
    ordering_fields = ['created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return audit events visible to the current request principal."""
        queryset = CommerceAuditEvent.objects.select_related('organization', 'actor_account').all()
        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            return queryset.filter(organization_id=org_id) if org_id else queryset
        return queryset.filter(organization=self.request.auth)


class MarketplaceSettlementViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Read marketplace settlements for seller accounting.

    Settlements are derived from marketplace orders and can be marked ready or
    cancelled by seller finance operations.
    """

    serializer_class = MarketplaceSettlementSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'marketplace_settlement'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['order', 'seller_organization', 'status']
    search_fields = ['order__order_number', 'seller_organization__name']
    ordering_fields = ['created_at', 'updated_at', 'gross_amount', 'net_amount']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return settlements visible to the current principal."""
        queryset = MarketplaceSettlement.objects.select_related(
            'order',
            'seller_organization',
        ).prefetch_related('lines', 'lines__order_line', 'payout')
        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            if org_id:
                return queryset.filter(seller_organization_id=org_id)
            return queryset
        if self.request.auth == ECOMMERCE_MARKER:
            return queryset.none()
        return queryset.filter(seller_organization=self.request.auth)

    @action(detail=True, methods=['post'])
    def ready(self, request, pk=None):
        """Mark a settlement as ready for payout."""
        settlement = self.get_object()
        settlement.mark_ready(notes=request.data.get('notes', ''))
        record_audit_event(request, 'marketplace.settlement.ready', settlement, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(settlement).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a settlement before it is paid."""
        settlement = self.get_object()
        settlement.cancel(notes=request.data.get('notes', ''))
        record_audit_event(request, 'marketplace.settlement.cancel', settlement, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(settlement).data)


class MarketplacePayoutViewSet(viewsets.ModelViewSet):
    """
    Manage manual or gateway-backed marketplace payouts.

    Gateway integration can later create payouts automatically, but for now
    the seller can create and process them manually.
    """

    serializer_class = MarketplacePayoutSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'marketplace_settlement'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['settlement', 'method', 'status']
    search_fields = ['provider_name', 'provider_reference', 'settlement__order__order_number']
    ordering_fields = ['created_at', 'processed_at']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return payouts visible to the current principal."""
        queryset = MarketplacePayout.objects.select_related('settlement', 'settlement__seller_organization').all()
        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            if org_id:
                return queryset.filter(settlement__seller_organization_id=org_id)
            return queryset
        if self.request.auth == ECOMMERCE_MARKER:
            return queryset.none()
        return queryset.filter(settlement__seller_organization=self.request.auth)

    def perform_create(self, serializer):
        """Create a payout for a seller settlement."""
        settlement = serializer.validated_data.get('settlement')
        if hasattr(settlement, 'payout'):
            raise ValidationError({'settlement': 'This settlement already has a payout.'})
        instance = serializer.save(amount=settlement.net_amount)
        record_audit_event(self.request, 'marketplace.payout.create', instance, serializer.validated_data)

    @action(detail=True, methods=['post'])
    def process(self, request, pk=None):
        """Mark a payout as processed and update the settlement status."""
        payout = self.get_object()
        payout.process(notes=request.data.get('notes', ''))
        record_audit_event(request, 'marketplace.payout.process', payout, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(payout).data)

    @action(detail=True, methods=['post'])
    def fail(self, request, pk=None):
        """Mark a payout as failed without settling funds."""
        payout = self.get_object()
        payout.fail(notes=request.data.get('notes', ''))
        record_audit_event(request, 'marketplace.payout.fail', payout, {'notes': request.data.get('notes', '')})
        return Response(self.get_serializer(payout).data)
