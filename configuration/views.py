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
from .models import State, Warehouse, ApiConfiguration
from .serializers import StateSerializer, WarehouseSerializer, ApiConfigurationSerializer


class StateViewSet(viewsets.ReadOnlyModelViewSet):
    """Indian States for GST."""
    serializer_class = StateSerializer
    filterset_fields = ['is_active']
    search_fields = ['name', 'state_code']
    ordering = ['name']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return State.objects.none()
        return State.objects.filter(organization=self.request.auth)


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
    serializer_class = WarehouseSerializer
    filterset_fields = ['is_active', 'is_default']
    search_fields = ['name', 'code', 'gstin', 'legal_name']
    ordering_fields = ['name', 'code', 'created_at']
    ordering = ['name']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return Warehouse.objects.none()
        return Warehouse.objects.filter(organization=self.request.auth)

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
    """API token configuration per organization."""
    serializer_class = ApiConfigurationSerializer
    
    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return ApiConfiguration.objects.none()
        return ApiConfiguration.objects.filter(organization=self.request.auth)
    
    def get_object(self):
        return self.get_object()
        
    def destroy(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Cannot delete API configuration.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED
        )