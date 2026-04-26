from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CategoryViewSet, UnitViewSet,
    AttributeTypeViewSet, AttributeValueViewSet,
    TaxCodeViewSet, TaxComponentViewSet,
    ItemViewSet, ItemVariantViewSet, ItemImageViewSet,
    BatchViewSet, OpeningStockViewSet, StockMovementViewSet
)

router = DefaultRouter()
router.register(r'categories', CategoryViewSet)
router.register(r'units', UnitViewSet)
router.register(r'attribute-types', AttributeTypeViewSet)
router.register(r'attribute-values', AttributeValueViewSet)
router.register(r'tax-codes', TaxCodeViewSet)
router.register(r'tax-components', TaxComponentViewSet)
router.register(r'items', ItemViewSet)
router.register(r'batches', BatchViewSet)
router.register(r'opening-stock', OpeningStockViewSet)
router.register(r'stock-movements', StockMovementViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('items/<int:item_pk>/variants/', ItemVariantViewSet.as_view({'get': 'list', 'post': 'create'}), name='item-variants'),
    path('items/<int:item_pk>/variants/<int:pk>/', ItemVariantViewSet.as_view({'get': 'retrieve', 'put': 'update', 'delete': 'destroy'}), name='item-variant-detail'),
    path('items/<int:item_pk>/images/', ItemImageViewSet.as_view({'get': 'list', 'post': 'create'}), name='item-images'),
    path('items/<int:item_pk>/images/<int:pk>/', ItemImageViewSet.as_view({'get': 'retrieve', 'put': 'update', 'delete': 'destroy'}), name='item-image-detail'),
]