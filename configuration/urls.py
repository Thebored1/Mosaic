"""
Configuration App URL Configuration
===================================

This module defines all API endpoints for configuration app.

Endpoint Summary:
    /warehouses/ - Warehouse/location management
    /api-config/ - API configuration singleton
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import WarehouseViewSet, ApiConfigurationViewSet


router = DefaultRouter()

# Warehouse endpoints
# GET /warehouses/ - List all warehouses
# POST /warehouses/ - Create new warehouse
# GET /warehouses/{id}/ - Get warehouse details
# PUT /warehouses/{id}/ - Update warehouse
# DELETE /warehouses/{id}/ - Delete warehouse
# POST /warehouses/{id}/set-default/ - Set as default
router.register(r'warehouses', WarehouseViewSet, basename='warehouses')

# API Configuration endpoints
# GET /api-config/ - Get current configuration
# GET /api-config/1/ - Get configuration
# PUT /api-config/1/ - Update configuration
router.register(r'api-config', ApiConfigurationViewSet, basename='api-config')


urlpatterns = [
    path('', include(router.urls)),
]