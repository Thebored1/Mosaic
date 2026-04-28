"""
Configuration App Views
======================

This module provides API views for configuration models.

Views:
------
WarehouseViewSet - CRUD operations for Warehouse model
ApiConfigurationViewSet - CRUD operations for ApiConfiguration model
"""

from rest_framework import viewsets, status, decorators
from rest_framework.response import Response
from django.db.models import Sum
from .models import Warehouse, ApiConfiguration
from .serializers import WarehouseSerializer, ApiConfigurationSerializer


class WarehouseViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Warehouse CRUD operations.
    
    Provides full CRUD capability for warehouse/location management.
    
    Actions:
        list - GET /warehouses/ - List all warehouses
        create - POST /warehouses/ - Create new warehouse
        retrieve - GET /warehouses/{id}/ - Get warehouse details
        update - PUT /warehouses/{id}/ - Update warehouse
        partial_update - PATCH /warehouses/{id}/ - Partial update
        destroy - DELETE /warehouses/{id}/ - Delete warehouse
        
    Query Parameters:
        ?is_active=true - Filter by active status
        ?is_default=true - Filter by default warehouse
        ?search=query - Search by name, code, GSTIN
    
    Ordering:
        ?ordering=name - Order by name (default)
        ?ordering=code - Order by code
    """
    queryset = Warehouse.objects.all()
    serializer_class = WarehouseSerializer
    filterset_fields = ['is_active', 'is_default']
    search_fields = ['name', 'code', 'gstin', 'legal_name']
    ordering_fields = ['name', 'code', 'created_at']
    ordering = ['name']

    @decorators.action(detail=True, methods=['post'])
    def set_default(self, request, pk=None):
        """
        Set warehouse as default.
        
        Action: POST /warehouses/{id}/set_default/
        
        Makes this warehouse the default for new transactions.
        Automatically unsets other default warehouses.
        
        Returns:
            Updated warehouse with is_default=True
        """
        warehouse = self.get_object()
        Warehouse.objects.filter(is_default=True).exclude(pk=warehouse.pk).update(is_default=False)
        warehouse.is_default = True
        warehouse.save(update_fields=['is_default'])
        
        serializer = self.get_serializer(warehouse)
        return Response(serializer.data)


class ApiConfigurationViewSet(viewsets.ModelViewSet):
    """
    ViewSet for API Configuration operations.
    
    Provides CRUD for API token configuration.
    Due to singleton pattern, only one instance exists.
    
    Actions:
        list - GET /api-config/ - Get current configuration
        retrieve - GET /api-config/1/ - Get configuration details
        partial_update - PATCH /api-config/1/ - Update token
        update - PUT /api-config/1/ - Update configuration
    
    Note:
        token value is hidden in responses for security.
        Use PUT/PATCH with new token to regenerate.
    """
    queryset = ApiConfiguration.objects.all()
    serializer_class = ApiConfigurationSerializer
    
    def get_object(self):
        """Return singleton instance."""
        obj, created = ApiConfiguration.objects.get_or_create(pk=1)
        return obj
    
    def list(self, request, *args, **kwargs):
        """Return singleton instead of list."""
        return self.retrieve(request, *args, **kwargs)
    
    def create(self, request, *args, **kwargs):
        """
        Create or update configuration.
        
        POST to this endpoint will update the existing singleton
        rather than creating a new one.
        """
        return self.partial_update(request, *args, **kwargs)
    
    def destroy(self, request, *args, **kwargs):
        """
        Prevent deletion of singleton.
        
        Returns:
            405 Method Not Allowed
        """
        return Response(
            {'detail': 'Cannot delete API configuration.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED
        )