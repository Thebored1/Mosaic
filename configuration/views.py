"""
Configuration App Views
======================

This module provides API views for configuration models.

Views:
------
WarehouseViewSet - CRUD operations for Warehouse model
TenantSettingsViewSet - tenant-wide operational settings
"""

from django.db import transaction
from django.db.models import Q
from rest_framework import viewsets, decorators, status
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from .models import State, Warehouse, TenantSettings
from .serializers import StateSerializer, WarehouseSerializer, TenantSettingsSerializer
from configuration.authentication import SUPER_ADMIN_MARKER, ECOMMERCE_MARKER, ScopedRolePermission


def org_filter(qs, request):
    """Filter queryset by organization from auth token."""
    if not hasattr(request, 'auth') or request.auth is None:
        return qs.none()
    if request.auth == ECOMMERCE_MARKER:
        return qs.none()

    if qs.model is State:
        if request.auth == SUPER_ADMIN_MARKER:
            org_id = request.query_params.get('organization')
            if org_id:
                return qs.filter(Q(organization_id=org_id) | Q(organization__isnull=True))
            return qs
        return qs.filter(Q(organization=request.auth) | Q(organization__isnull=True))

    if request.auth == SUPER_ADMIN_MARKER:
        org_id = request.query_params.get('organization')
        if org_id:
            if hasattr(qs.model, '_meta') and any(f.name == 'organization' for f in qs.model._meta.get_fields()):
                return qs.filter(organization_id=org_id)
        return qs

    if hasattr(qs.model, '_meta') and any(f.name == 'organization' for f in qs.model._meta.get_fields()):
        return qs.filter(organization=request.auth)
    return qs


def save_for_request_organization(serializer, request):
    org_id = request.data.get('organization') or request.query_params.get('organization')

    if request.auth == SUPER_ADMIN_MARKER:
        if not org_id:
            raise ValidationError({'organization': 'organization is required for super admin writes'})
        serializer.save(organization_id=org_id)
        return
    if request.auth == ECOMMERCE_MARKER:
        raise ValidationError({'organization': 'Create or join an organization to access this feature'})

    serializer.save(organization=request.auth)


class StateViewSet(viewsets.ReadOnlyModelViewSet):
    """Indian States for GST."""
    serializer_class = StateSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'configuration_state'
    filterset_fields = ['is_active']
    search_fields = ['name', 'state_code']
    ordering = ['name']

    def get_queryset(self):
        return org_filter(State.objects.all(), self.request)


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
    permission_classes = [ScopedRolePermission]
    permission_scope = 'configuration_warehouse'
    filterset_fields = ['is_active', 'is_default']
    search_fields = ['name', 'code', 'gstin', 'legal_name']
    ordering_fields = ['name', 'code', 'created_at']
    ordering = ['name']

    def get_queryset(self):
        return org_filter(Warehouse.objects.all(), self.request)

    def perform_create(self, serializer):
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        save_for_request_organization(serializer, self.request)

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


class TenantSettingsViewSet(viewsets.ModelViewSet):
    """CRUD settings surface for tenant-wide operational defaults."""

    serializer_class = TenantSettingsSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'tenant_settings'
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']

    def get_queryset(self):
        return org_filter(TenantSettings.objects.select_related('organization', 'default_warehouse').all(), self.request)

    def get_object(self):
        org = self.request.query_params.get('organization') if self.request.auth == SUPER_ADMIN_MARKER else self.request.auth
        if org is None:
            raise ValidationError({'organization': 'organization is required'})
        settings = TenantSettings.objects.select_related('organization', 'default_warehouse').filter(organization=org).first()
        if settings is None:
            settings = TenantSettings.objects.create(organization=org)
        return settings

    def create(self, request, *args, **kwargs):
        with transaction.atomic():
            instance = self.get_object()
            serializer = self.get_serializer(instance, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        with transaction.atomic():
            instance = self.get_object()
            serializer = self.get_serializer(instance, data=request.data, partial=False)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

    def partial_update(self, request, *args, **kwargs):
        with transaction.atomic():
            instance = self.get_object()
            serializer = self.get_serializer(instance, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)
