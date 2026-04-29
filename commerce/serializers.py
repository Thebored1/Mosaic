"""
Serializers for storefront catalog, cart, address, and order APIs.

The commerce serializers intentionally shape data around the buyer journey:

1. catalog browse serializers expose item/variant summaries and channel flags
2. address serializers manage the buyer's reusable address book
3. cart serializers validate editable pre-checkout state
4. order serializers expose the frozen checkout snapshot

This keeps the API boundary stable while the underlying stock, payment, and
fulfillment systems evolve independently.
"""

from rest_framework import serializers

from account.models import Organization

from .models import (
    Cart,
    CartItem,
    CommerceAuditEvent,
    CommerceContentPage,
    CommerceListing,
    CommerceNotification,
    CommerceOrder,
    CommerceOrderLine,
    CommercePriceOverride,
    CommerceRefund,
    CommerceReturnRequest,
    CommerceSettings,
    CommerceShipment,
    MarketplacePayout,
    MarketplaceSettlement,
    MarketplaceSettlementLine,
    InventoryReservation,
    ProductReview,
    ShopperAddress,
    Wishlist,
    WishlistItem,
    get_listing_effective_price,
)


class OrganizationSummarySerializer(serializers.Serializer):
    """
    Compact organization representation used in commerce responses.

    Commerce endpoints frequently need to show the seller identity without
    embedding the entire Organization model payload, especially in listings and
    order lines.
    """

    id = serializers.IntegerField()
    name = serializers.CharField()
    trade_name = serializers.CharField()


class ListingItemSummarySerializer(serializers.Serializer):
    """
    Compact stock item representation for catalog APIs.

    This serializer keeps the catalog response light while still giving the
    frontend enough information to render a product card and pricing summary.
    """

    id = serializers.IntegerField()
    name = serializers.CharField()
    sku = serializers.CharField()
    description = serializers.CharField()
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    current_stock = serializers.DecimalField(max_digits=12, decimal_places=4, allow_null=True)


class ListingVariantSummarySerializer(serializers.Serializer):
    """
    Compact stock variant representation for catalog APIs.

    Variant summaries are only included when a CommerceListing points to a
    specific item variant.
    """

    id = serializers.IntegerField()
    sku = serializers.CharField()
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    current_stock = serializers.DecimalField(max_digits=12, decimal_places=4)


class CommerceListingSerializer(serializers.ModelSerializer):
    """
    Serialize listings for both buyer browse and seller management.

    Seller users use this serializer for create/update flows, while buyers use
    the read-only representation to browse the public catalog.
    """

    organization = OrganizationSummarySerializer(read_only=True)
    organization_id = serializers.PrimaryKeyRelatedField(
        source='organization',
        queryset=Organization.objects.all(),
        required=False,
        allow_null=True,
        default=None,
        write_only=True,
    )
    item_summary = serializers.SerializerMethodField()
    variant_summary = serializers.SerializerMethodField()
    available_quantity = serializers.DecimalField(max_digits=12, decimal_places=4, read_only=True)
    effective_price = serializers.SerializerMethodField()

    class Meta:
        model = CommerceListing
        fields = [
            'id', 'organization', 'organization_id', 'item', 'item_variant',
            'item_summary', 'variant_summary', 'title', 'description', 'price',
            'compare_at_price', 'min_quantity', 'max_quantity', 'available_quantity',
            'effective_price',
            'is_active', 'is_b2c_enabled', 'is_b2b_enabled', 'is_marketplace_enabled',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'available_quantity', 'effective_price']

    def get_item_summary(self, obj):
        """Return a compact representation of the linked stock item."""
        return ListingItemSummarySerializer(obj.item).data

    def get_variant_summary(self, obj):
        """Return a compact representation of the linked variant, if present."""
        if obj.item_variant_id is None:
            return None
        return ListingVariantSummarySerializer(obj.item_variant).data

    def get_effective_price(self, obj):
        """Resolve the buyer-specific price for the current request context."""
        request = self.context.get('request')
        account = getattr(getattr(request, 'user', None), 'account', None) if request else None
        return get_listing_effective_price(obj, account)


class ShopperAddressSerializer(serializers.ModelSerializer):
    """
    Serialize buyer addresses owned by the authenticated account.

    Address data is editable by the buyer and later copied into the checkout
    snapshot on the resulting order.
    """

    class Meta:
        model = ShopperAddress
        fields = [
            'id', 'label', 'recipient_name', 'phone', 'line1', 'line2',
            'city', 'state', 'postal_code', 'country',
            'is_default_shipping', 'is_default_billing',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CartItemSerializer(serializers.ModelSerializer):
    """
    Serialize items inside the active shopper cart.

    The cart item includes the live listing payload for display, but only the
    listing_id and quantity are writable from the API.
    """

    listing = CommerceListingSerializer(read_only=True)
    listing_id = serializers.PrimaryKeyRelatedField(
        source='listing',
        queryset=CommerceListing.objects.filter(is_active=True),
        write_only=True,
    )

    class Meta:
        model = CartItem
        fields = [
            'id', 'listing', 'listing_id', 'quantity',
            'unit_price', 'line_total', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'unit_price', 'line_total', 'created_at', 'updated_at']


class CartSerializer(serializers.ModelSerializer):
    """
    Serialize the current cart with nested items.

    The cart serializer is used by the buyer to inspect the live working basket
    before checkout.
    """

    items = CartItemSerializer(many=True, read_only=True)

    class Meta:
        model = Cart
        fields = [
            'id', 'channel', 'status', 'notes', 'sub_total',
            'grand_total', 'created_at', 'updated_at', 'items'
        ]
        read_only_fields = ['id', 'status', 'sub_total', 'grand_total', 'created_at', 'updated_at', 'items']


class CartCheckoutSerializer(serializers.Serializer):
    """
    Validate checkout inputs before creating a storefront order.

    Checkout input is intentionally small. The server derives the order lines
    from the active cart, while the client only supplies the selected addresses
    and optional notes.
    """

    billing_address_id = serializers.PrimaryKeyRelatedField(
        source='billing_address',
        queryset=ShopperAddress.objects.all(),
        required=False,
        allow_null=True,
    )
    shipping_address_id = serializers.PrimaryKeyRelatedField(
        source='shipping_address',
        queryset=ShopperAddress.objects.all(),
        required=False,
        allow_null=True,
    )
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        """
        Enforce that checkout addresses belong to the authenticated account.

        This prevents a buyer from referencing another user's saved addresses
        during checkout, which would otherwise create a cross-account leakage
        path in the order snapshot.
        """
        request = self.context['request']
        account = request.user.account
        for field_name in ('billing_address', 'shipping_address'):
            address = attrs.get(field_name)
            if address is not None and address.user_account_id != account.id:
                raise serializers.ValidationError({field_name: 'Address does not belong to the authenticated account.'})
        return attrs


class CommerceOrderLineSerializer(serializers.ModelSerializer):
    """
    Serialize a storefront order line.

    Order lines are exposed as frozen snapshots so buyers and sellers can see
    exactly what was purchased at checkout time.
    """

    organization = OrganizationSummarySerializer(read_only=True)

    class Meta:
        model = CommerceOrderLine
        fields = ['id', 'organization', 'title', 'sku', 'quantity', 'unit_price', 'line_total']


class MarketplaceSettlementLineSerializer(serializers.ModelSerializer):
    """Serialize one order line inside a marketplace settlement."""

    order_line_detail = CommerceOrderLineSerializer(source='order_line', read_only=True)

    class Meta:
        model = MarketplaceSettlementLine
        fields = ['id', 'order_line', 'order_line_detail', 'quantity', 'line_total', 'commission_amount']


class MarketplacePayoutSerializer(serializers.ModelSerializer):
    """Serialize a seller payout record for a marketplace settlement."""

    class Meta:
        model = MarketplacePayout
        fields = [
            'id', 'settlement', 'amount', 'method', 'status', 'provider_name',
            'provider_reference', 'processed_at', 'failed_at', 'notes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'amount', 'processed_at', 'failed_at', 'created_at', 'updated_at']


class MarketplaceSettlementSerializer(serializers.ModelSerializer):
    """Serialize one seller-specific marketplace settlement."""

    seller_organization = OrganizationSummarySerializer(read_only=True)
    lines = MarketplaceSettlementLineSerializer(many=True, read_only=True)
    payout = MarketplacePayoutSerializer(read_only=True)

    class Meta:
        model = MarketplaceSettlement
        fields = [
            'id', 'order', 'seller_organization', 'gross_amount',
            'commission_rate', 'commission_amount', 'tax_amount',
            'adjustment_amount', 'net_amount', 'status', 'notes',
            'ready_at', 'paid_at', 'cancelled_at', 'reversed_at',
            'created_at', 'updated_at', 'lines', 'payout'
        ]
        read_only_fields = [
            'id', 'order', 'seller_organization', 'gross_amount', 'commission_rate',
            'commission_amount', 'tax_amount', 'adjustment_amount', 'net_amount',
            'ready_at', 'paid_at', 'cancelled_at', 'reversed_at', 'created_at',
            'updated_at', 'lines', 'payout'
        ]


class CommerceOrderSerializer(serializers.ModelSerializer):
    """
    Serialize storefront orders for buyers and sellers.

    Buyers see their own order history through this serializer. Sellers can
    reuse it to inspect the lines that belong to their organization when the
    view context sets `seller_view=True`.
    """

    lines = serializers.SerializerMethodField()
    shipment = serializers.SerializerMethodField()
    marketplace_settlements = serializers.SerializerMethodField()

    class Meta:
        model = CommerceOrder
        fields = [
            'id', 'order_number', 'channel', 'status', 'payment_status',
            'fulfillment_status', 'billing_snapshot', 'shipping_snapshot',
            'notes', 'sub_total', 'grand_total', 'placed_at',
            'created_at', 'updated_at', 'shipment', 'marketplace_settlements', 'lines'
        ]

    def get_lines(self, obj):
        """Return order lines, optionally filtered to the seller's organization."""
        request = self.context.get('request')
        lines = obj.lines.all()
        if request is not None and hasattr(request, 'auth') and getattr(request.auth, 'pk', None):
            if self.context.get('seller_view'):
                lines = lines.filter(organization=request.auth)
        return CommerceOrderLineSerializer(lines, many=True).data

    def get_shipment(self, obj):
        """Return the linked shipment if the order has one."""
        shipment = getattr(obj, 'shipment', None)
        if shipment is None:
            return None
        return CommerceShipmentSerializer(shipment).data

    def get_marketplace_settlements(self, obj):
        """Return seller settlements for marketplace orders."""
        settlements = obj.marketplace_settlements.all().prefetch_related('lines', 'lines__order_line', 'payout')
        if obj.channel != 'marketplace' and not settlements.exists():
            return []
        return MarketplaceSettlementSerializer(settlements, many=True).data


class CommerceSettingsSerializer(serializers.ModelSerializer):
    """Serialize organization-level ecommerce operational settings."""

    class Meta:
        model = CommerceSettings
        fields = [
            'id', 'organization', 'reserve_stock_on_checkout', 'prevent_oversell',
            'manual_fulfillment', 'manual_returns_only', 'allow_b2b_price_overrides',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class CommercePriceOverrideSerializer(serializers.ModelSerializer):
    """Serialize seller-defined buyer-specific pricing rules."""

    seller_organization = OrganizationSummarySerializer(read_only=True)
    buyer_organization = OrganizationSummarySerializer(read_only=True)
    seller_organization_id = serializers.PrimaryKeyRelatedField(
        source='seller_organization',
        queryset=Organization.objects.all(),
        write_only=True,
        required=False,
        allow_null=True,
    )
    buyer_organization_id = serializers.PrimaryKeyRelatedField(
        source='buyer_organization',
        queryset=Organization.objects.all(),
        write_only=True,
    )
    listing_id = serializers.PrimaryKeyRelatedField(
        source='listing',
        queryset=CommerceListing.objects.all(),
        write_only=True,
    )

    class Meta:
        model = CommercePriceOverride
        fields = [
            'id', 'seller_organization', 'seller_organization_id',
            'buyer_organization', 'buyer_organization_id', 'listing',
            'listing_id', 'price', 'is_active', 'notes', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class InventoryReservationSerializer(serializers.ModelSerializer):
    """Serialize stock reservations created during checkout."""

    class Meta:
        model = InventoryReservation
        fields = [
            'id', 'organization', 'order', 'order_line', 'listing', 'item',
            'item_variant', 'quantity', 'status', 'reserved_at', 'released_at',
            'consumed_at', 'expires_at', 'notes'
        ]
        read_only_fields = [
            'id', 'organization', 'order', 'order_line', 'listing', 'item',
            'item_variant', 'reserved_at', 'released_at', 'consumed_at',
            'expires_at'
        ]


class CommerceShipmentSerializer(serializers.ModelSerializer):
    """Serialize manual fulfillment records."""

    class Meta:
        model = CommerceShipment
        fields = [
            'id', 'organization', 'order', 'method', 'status', 'carrier_name',
            'tracking_number', 'shipped_at', 'delivered_at', 'notes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'organization', 'shipped_at', 'delivered_at', 'created_at', 'updated_at']


class CommerceReturnRequestSerializer(serializers.ModelSerializer):
    """Serialize manual return requests."""

    class Meta:
        model = CommerceReturnRequest
        fields = [
            'id', 'organization', 'order', 'shipment', 'order_line', 'quantity',
            'reason', 'notes', 'status', 'requested_at', 'received_at', 'processed_at'
        ]
        read_only_fields = ['id', 'organization', 'requested_at', 'received_at', 'processed_at']


class CommerceRefundSerializer(serializers.ModelSerializer):
    """Serialize manual refund records."""

    class Meta:
        model = CommerceRefund
        fields = [
            'id', 'organization', 'order', 'return_request', 'amount', 'status',
            'notes', 'processed_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'organization', 'processed_at', 'created_at', 'updated_at']


class WishlistSerializer(serializers.ModelSerializer):
    """Serialize a buyer wishlist and its channel context."""

    items = serializers.SerializerMethodField()

    class Meta:
        model = Wishlist
        fields = ['id', 'user_account', 'channel', 'name', 'is_default', 'items', 'created_at', 'updated_at']
        read_only_fields = ['id', 'user_account', 'created_at', 'updated_at', 'items']

    def get_items(self, obj):
        """Return the wishlist items as nested listing snapshots."""
        return WishlistItemSerializer(obj.items.select_related('listing'), many=True).data


class WishlistItemSerializer(serializers.ModelSerializer):
    """Serialize one saved listing inside a wishlist."""

    class Meta:
        model = WishlistItem
        fields = ['id', 'wishlist', 'listing', 'created_at']
        read_only_fields = ['id', 'created_at']


class ProductReviewSerializer(serializers.ModelSerializer):
    """Serialize storefront product reviews."""

    class Meta:
        model = ProductReview
        fields = [
            'id', 'listing', 'user_account', 'rating', 'title', 'body',
            'is_approved', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user_account', 'is_approved', 'created_at', 'updated_at']


class CommerceContentPageSerializer(serializers.ModelSerializer):
    """Serialize public storefront content pages."""

    class Meta:
        model = CommerceContentPage
        fields = ['id', 'organization', 'slug', 'title', 'page_type', 'body', 'is_published', 'created_at', 'updated_at']
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class CommerceNotificationSerializer(serializers.ModelSerializer):
    """Serialize buyer notifications for the storefront inbox."""

    class Meta:
        model = CommerceNotification
        fields = [
            'id', 'user_account', 'organization', 'notification_type', 'title',
            'message', 'payload', 'is_read', 'read_at', 'created_at'
        ]
        read_only_fields = [
            'id', 'user_account', 'organization', 'notification_type', 'title',
            'message', 'payload', 'read_at', 'created_at'
        ]


class CommerceAuditEventSerializer(serializers.ModelSerializer):
    """Serialize the commerce audit trail for seller and support users."""

    class Meta:
        model = CommerceAuditEvent
        fields = [
            'id', 'actor_user', 'actor_account', 'organization', 'action',
            'entity_type', 'entity_id', 'details', 'ip_address', 'user_agent',
            'created_at'
        ]
        read_only_fields = fields
