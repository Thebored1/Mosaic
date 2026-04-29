"""
Configuration App URL Configuration
==================================

Endpoints:
- /states/ - Indian states for GST
- /warehouses/ - Warehouse/location management
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import StateViewSet, WarehouseViewSet


router = DefaultRouter()

router.register(r'states', StateViewSet, basename='states')
router.register(r'warehouses', WarehouseViewSet, basename='warehouses')


urlpatterns = [
    path('', include(router.urls)),
]