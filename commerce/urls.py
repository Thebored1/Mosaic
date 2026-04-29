"""
Commerce API routes.

The router exposes the buyer-facing catalog, address book, cart, and order
resources under a single versioned namespace so the Next.js client can treat
commerce as a cohesive surface.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CartItemViewSet,
    CartViewSet,
    CommerceAuditEventViewSet,
    CommerceContentPageViewSet,
    CommerceListingViewSet,
    CommerceNotificationViewSet,
    CommerceOrderViewSet,
    CommercePriceOverrideViewSet,
    CommerceRefundViewSet,
    CommerceReturnRequestViewSet,
    CommerceSettingsViewSet,
    CommerceShipmentViewSet,
    MarketplacePayoutViewSet,
    MarketplaceSettlementViewSet,
    ProductReviewViewSet,
    SellerOrderViewSet,
    ShopperAddressViewSet,
    WishlistItemViewSet,
    WishlistViewSet,
)


router = DefaultRouter()
router.register(r'listings', CommerceListingViewSet, basename='commerce-listings')
router.register(r'addresses', ShopperAddressViewSet, basename='commerce-addresses')
router.register(r'carts', CartViewSet, basename='commerce-carts')
router.register(r'cart-items', CartItemViewSet, basename='commerce-cart-items')
router.register(r'orders', CommerceOrderViewSet, basename='commerce-orders')
router.register(r'seller-orders', SellerOrderViewSet, basename='commerce-seller-orders')
router.register(r'settings', CommerceSettingsViewSet, basename='commerce-settings')
router.register(r'price-overrides', CommercePriceOverrideViewSet, basename='commerce-price-overrides')
router.register(r'shipments', CommerceShipmentViewSet, basename='commerce-shipments')
router.register(r'returns', CommerceReturnRequestViewSet, basename='commerce-returns')
router.register(r'refunds', CommerceRefundViewSet, basename='commerce-refunds')
router.register(r'wishlists', WishlistViewSet, basename='commerce-wishlists')
router.register(r'wishlist-items', WishlistItemViewSet, basename='commerce-wishlist-items')
router.register(r'reviews', ProductReviewViewSet, basename='commerce-reviews')
router.register(r'pages', CommerceContentPageViewSet, basename='commerce-pages')
router.register(r'notifications', CommerceNotificationViewSet, basename='commerce-notifications')
router.register(r'audit-events', CommerceAuditEventViewSet, basename='commerce-audit-events')
router.register(r'marketplace-settlements', MarketplaceSettlementViewSet, basename='commerce-marketplace-settlements')
router.register(r'marketplace-payouts', MarketplacePayoutViewSet, basename='commerce-marketplace-payouts')

urlpatterns = [
    path('', include(router.urls)),
]
