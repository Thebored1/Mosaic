"""
Admin registrations for commerce models.

The commerce admin is intentionally operational rather than decorative. It is
meant to help staff inspect listings, carts, checkout orders, and buyer
addresses when supporting storefront activity.
"""

from django.contrib import admin

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
)


@admin.register(CommerceListing)
class CommerceListingAdmin(admin.ModelAdmin):
    """Admin for seller-managed product listings.

    Listings are the seller's public catalog entries, so the admin exposes the
    organization and linked inventory objects alongside the price and channel
    toggles.
    """

    list_display = ('title', 'organization', 'item', 'item_variant', 'price', 'is_active')
    list_filter = ('is_active', 'is_b2c_enabled', 'is_b2b_enabled', 'is_marketplace_enabled')
    search_fields = ('title', 'item__name', 'item__sku', 'item_variant__sku')


@admin.register(ShopperAddress)
class ShopperAddressAdmin(admin.ModelAdmin):
    """Admin for shopper addresses.

    Address records are primarily buyer-owned but still useful to staff when
    investigating checkout issues or support escalations.
    """

    list_display = ('label', 'user_account', 'city', 'postal_code', 'is_default_shipping', 'is_default_billing')
    search_fields = ('label', 'recipient_name', 'city', 'postal_code', 'user_account__user__username')


class CartItemInline(admin.TabularInline):
    """Inline cart items for cart administration.

    Carts are often inspected together with their lines, so the inline helps
    staff see the exact cart composition without leaving the parent record.
    """

    model = CartItem
    extra = 0


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    """Admin for active and historical carts.

    Carts are transient documents, but keeping them visible in admin helps
    support teams diagnose abandoned or inconsistent checkout state.
    """

    list_display = ('id', 'user_account', 'channel', 'status', 'sub_total', 'grand_total', 'updated_at')
    list_filter = ('channel', 'status')
    search_fields = ('user_account__user__username',)
    inlines = [CartItemInline]


class CommerceOrderLineInline(admin.TabularInline):
    """Inline order lines for commerce orders.

    Order lines are immutable snapshots, so the inline is read-only to avoid
    accidental mutation of historical order data.
    """

    model = CommerceOrderLine
    extra = 0
    readonly_fields = ('organization', 'title', 'sku', 'quantity', 'unit_price', 'line_total')


@admin.register(CommerceOrder)
class CommerceOrderAdmin(admin.ModelAdmin):
    """Admin for storefront orders.

    Orders are the final buyer-facing transaction records. The admin keeps the
    status and totals visible while preserving the frozen line snapshots below.
    """

    list_display = (
        'order_number', 'user_account', 'channel', 'status',
        'payment_status', 'fulfillment_status', 'grand_total', 'created_at'
    )
    list_filter = ('channel', 'status', 'payment_status', 'fulfillment_status')
    search_fields = ('order_number', 'user_account__user__username')
    inlines = [CommerceOrderLineInline]


@admin.register(CommerceSettings)
class CommerceSettingsAdmin(admin.ModelAdmin):
    """Admin for organization-level commerce settings."""

    list_display = ('organization', 'reserve_stock_on_checkout', 'prevent_oversell', 'manual_fulfillment', 'manual_returns_only')
    list_filter = ('reserve_stock_on_checkout', 'prevent_oversell', 'manual_fulfillment', 'manual_returns_only')
    search_fields = ('organization__name',)


@admin.register(CommercePriceOverride)
class CommercePriceOverrideAdmin(admin.ModelAdmin):
    """Admin for buyer-specific price overrides."""

    list_display = ('listing', 'seller_organization', 'buyer_organization', 'price', 'is_active')
    list_filter = ('is_active', 'seller_organization', 'buyer_organization')
    search_fields = ('listing__title', 'buyer_organization__name')


@admin.register(CommerceShipment)
class CommerceShipmentAdmin(admin.ModelAdmin):
    """Admin for manual fulfillment records."""

    list_display = ('order', 'organization', 'method', 'status', 'tracking_number', 'shipped_at', 'delivered_at')
    list_filter = ('method', 'status', 'organization')
    search_fields = ('order__order_number', 'tracking_number', 'carrier_name')


@admin.register(CommerceReturnRequest)
class CommerceReturnRequestAdmin(admin.ModelAdmin):
    """Admin for manual return requests."""

    list_display = ('order', 'organization', 'order_line', 'quantity', 'status', 'requested_at')
    list_filter = ('status', 'organization')
    search_fields = ('order__order_number', 'reason', 'order_line__title')


@admin.register(CommerceRefund)
class CommerceRefundAdmin(admin.ModelAdmin):
    """Admin for manual refund records."""

    list_display = ('order', 'organization', 'amount', 'status', 'processed_at')
    list_filter = ('status', 'organization')
    search_fields = ('order__order_number',)


@admin.register(Wishlist)
class WishlistAdmin(admin.ModelAdmin):
    """Admin for buyer wishlists."""

    list_display = ('user_account', 'channel', 'name', 'is_default', 'updated_at')
    list_filter = ('channel', 'is_default')
    search_fields = ('user_account__user__username', 'name')


@admin.register(WishlistItem)
class WishlistItemAdmin(admin.ModelAdmin):
    """Admin for saved wishlist items."""

    list_display = ('wishlist', 'listing', 'created_at')
    search_fields = ('wishlist__name', 'listing__title')


@admin.register(ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    """Admin for product reviews and moderation."""

    list_display = ('listing', 'user_account', 'rating', 'is_approved', 'created_at')
    list_filter = ('rating', 'is_approved')
    search_fields = ('listing__title', 'title', 'body')


@admin.register(CommerceContentPage)
class CommerceContentPageAdmin(admin.ModelAdmin):
    """Admin for storefront content pages."""

    list_display = ('title', 'organization', 'slug', 'page_type', 'is_published')
    list_filter = ('page_type', 'is_published')
    search_fields = ('title', 'slug', 'body')


@admin.register(CommerceNotification)
class CommerceNotificationAdmin(admin.ModelAdmin):
    """Admin for commerce notifications."""

    list_display = ('title', 'user_account', 'organization', 'notification_type', 'is_read', 'created_at')
    list_filter = ('notification_type', 'is_read')
    search_fields = ('title', 'message', 'user_account__user__username')


@admin.register(InventoryReservation)
class InventoryReservationAdmin(admin.ModelAdmin):
    """Admin for inventory reservations."""

    list_display = ('order', 'organization', 'listing', 'quantity', 'status', 'reserved_at')
    list_filter = ('status', 'organization')
    search_fields = ('order__order_number', 'listing__title')


@admin.register(CommerceAuditEvent)
class CommerceAuditEventAdmin(admin.ModelAdmin):
    """Admin for audit trail inspection."""

    list_display = ('action', 'entity_type', 'entity_id', 'organization', 'actor_account', 'created_at')
    list_filter = ('action', 'entity_type')
    search_fields = ('action', 'entity_type', 'entity_id')


class MarketplaceSettlementLineInline(admin.TabularInline):
    """Inline marketplace settlement lines."""

    model = MarketplaceSettlementLine
    extra = 0
    readonly_fields = ('order_line', 'quantity', 'line_total', 'commission_amount')


@admin.register(MarketplaceSettlement)
class MarketplaceSettlementAdmin(admin.ModelAdmin):
    """Admin for marketplace settlement records."""

    list_display = ('order', 'seller_organization', 'gross_amount', 'commission_amount', 'net_amount', 'status')
    list_filter = ('status', 'seller_organization')
    search_fields = ('order__order_number', 'seller_organization__name')
    inlines = [MarketplaceSettlementLineInline]


@admin.register(MarketplacePayout)
class MarketplacePayoutAdmin(admin.ModelAdmin):
    """Admin for marketplace payout records."""

    list_display = ('settlement', 'amount', 'method', 'status', 'processed_at')
    list_filter = ('method', 'status')
    search_fields = ('settlement__order__order_number', 'provider_reference')
