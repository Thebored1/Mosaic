from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter, SearchFilter

from configuration.authentication import ECOMMERCE_MARKER, SUPER_ADMIN_MARKER, ScopedRolePermission

from .models import AuditEvent
from .serializers import AuditEventSerializer


class AuditEventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AuditEvent.objects.select_related(
        'organization',
        'actor_user',
        'actor_account',
        'content_type',
    ).prefetch_related('links')
    serializer_class = AuditEventSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'audit_read'
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['organization', 'actor_user', 'actor_account', 'event_type', 'source_app', 'outcome']
    search_fields = ['event_type', 'action', 'request_path', 'object_repr', 'metadata']
    ordering_fields = ['created_at', 'event_type', 'status_code']
    ordering = ['-created_at']

    def get_queryset(self):
        queryset = self.queryset
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return queryset.none()
        if self.request.auth == ECOMMERCE_MARKER:
            return queryset.none()
        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            return queryset.filter(organization_id=org_id) if org_id else queryset
        return queryset.filter(Q(organization=self.request.auth) | Q(organization__isnull=True))

    @action(detail=False, methods=['get'])
    def timeline(self, request):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page or queryset, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def request_trace(self, request):
        trace_id = request.query_params.get('trace_id') or request.query_params.get('correlation_id')
        queryset = self.get_queryset()
        if trace_id:
            queryset = queryset.filter(Q(trace_id=trace_id) | Q(correlation_id=trace_id))
        return Response(self.get_serializer(self.filter_queryset(queryset), many=True).data)

    @action(detail=False, methods=['get'])
    def entity_timeline(self, request):
        content_type_id = request.query_params.get('content_type')
        app_label = request.query_params.get('app_label')
        model = request.query_params.get('model')
        object_id = request.query_params.get('object_id')

        queryset = self.get_queryset()
        if content_type_id:
            queryset = queryset.filter(content_type_id=content_type_id)
        elif app_label and model:
            content_type = ContentType.objects.filter(app_label=app_label, model=model).first()
            if content_type is not None:
                queryset = queryset.filter(content_type=content_type)
        if object_id:
            queryset = queryset.filter(object_id=str(object_id))
        return Response(self.get_serializer(self.filter_queryset(queryset), many=True).data)

    @action(detail=False, methods=['get'])
    def user_activity(self, request):
        user_id = request.query_params.get('user')
        account_id = request.query_params.get('account')
        queryset = self.get_queryset()
        if user_id:
            queryset = queryset.filter(actor_user_id=user_id)
        if account_id:
            queryset = queryset.filter(actor_account_id=account_id)
        return Response(self.get_serializer(self.filter_queryset(queryset), many=True).data)

