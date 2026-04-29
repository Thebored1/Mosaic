"""
Configuration App URL Configuration
==================================

Endpoints:
- /states/ - Indian states for GST
- /warehouses/ - Warehouse/location management
- /api-config/ - API token configuration
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import StateViewSet, WarehouseViewSet, ApiConfigurationViewSet


router = DefaultRouter()

# States for GST
router.register(r'states', StateViewSet, basename='states')

# Warehouses
router.register(r'warehouses', WarehouseViewSet, basename='warehouses')

# API Configuration
router.register(r'api-config', ApiConfigurationViewSet, basename='api-config')


urlpatterns = [
    path('', include(router.urls)),
]