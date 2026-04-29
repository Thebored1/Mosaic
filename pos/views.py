"""
POS application views.

This module exposes the shift and cash transaction workflow used by the
point-of-sale screen. POS remains organization-scoped and is intentionally
separate from the storefront commerce layer.
"""

from rest_framework import viewsets, status, decorators
from rest_framework.response import Response
from django.db.models import Sum
from decimal import Decimal
from .models import Shift, CashTransaction
from .serializers import ShiftSerializer, CashTransactionSerializer
from configuration.authentication import SUPER_ADMIN_MARKER, ECOMMERCE_MARKER, ScopedRolePermission


class ShiftViewSet(viewsets.ModelViewSet):
    """CRUD viewset for POS shifts."""
    queryset = Shift.objects.all()
    serializer_class = ShiftSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'pos_operations'
    filterset_fields = ['status', 'warehouse', 'user']
    ordering_fields = ['shift_number', 'opening_time', 'closing_time']
    ordering = ['-opening_time']

    def get_queryset(self):
        """Return shifts visible to the current organization."""
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return Shift.objects.none()
        if self.request.auth == ECOMMERCE_MARKER:
            return Shift.objects.none()

        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            if org_id:
                queryset = Shift.objects.filter(warehouse__organization_id=org_id)
            else:
                queryset = Shift.objects.all()
        else:
            queryset = Shift.objects.filter(warehouse__organization=self.request.auth)
        
        date = self.request.query_params.get('date')
        if date:
            from django.utils import timezone
            from datetime import datetime, timedelta
            
            try:
                filter_date = datetime.strptime(date, '%Y-%m-%d').date()
                start = timezone.make_aware(datetime.combine(filter_date, datetime.min.time()))
                end = timezone.make_aware(datetime.combine(filter_date, datetime.max.time()))
                queryset = queryset.filter(opening_time__gte=start, opening_time__lte=end)
            except ValueError:
                pass
        
        return queryset

    def perform_create(self, serializer):
        """Open a shift for the authenticated user."""
        serializer.save(user=self.request.user)

    @decorators.action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        """Close a shift and return variance details."""
        shift = self.get_object()
        
        closing_cash = request.data.get('closing_cash')
        if closing_cash is None:
            return Response(
                {'detail': 'closing_cash is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            closing_cash = Decimal(str(closing_cash))
        except (ValueError, TypeError):
            return Response(
                {'detail': 'Invalid closing_cash value'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            result = shift.close(closing_cash)
        except Exception as e:
            return Response(
                {'detail': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        serializer = self.get_serializer(shift)
        return Response({
            'message': 'Shift closed successfully',
            'shift': serializer.data,
            'variance_details': result
        })

    @decorators.action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
        """Return a shift summary with sales and cash details."""
        shift = self.get_object()
        
        return Response({
            'shift_number': shift.shift_number,
            'user': shift.user.username,
            'warehouse': shift.warehouse.name,
            'status': shift.status,
            'opening_time': shift.opening_time,
            'closing_time': shift.closing_time,
            'opening_cash': shift.opening_cash,
            'closing_cash': shift.closing_cash,
            'expected_cash': shift.expected_cash,
            'variance': shift.variance,
            'sales_total': shift.sales_total,
            'cash_transactions': shift.transaction_summary
        })


class CashTransactionViewSet(viewsets.ModelViewSet):
    """CRUD viewset for cash transactions during a shift."""
    queryset = CashTransaction.objects.all()
    serializer_class = CashTransactionSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'pos_operations'
    filterset_fields = ['shift', 'transaction_type']
    ordering_fields = ['created_at', 'amount']
    ordering = ['-created_at']

    def get_queryset(self):
        """Return cash transactions visible to the current organization."""
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return CashTransaction.objects.none()
        if self.request.auth == ECOMMERCE_MARKER:
            return CashTransaction.objects.none()

        if self.request.auth == SUPER_ADMIN_MARKER:
            org_id = self.request.query_params.get('organization')
            if org_id:
                queryset = CashTransaction.objects.filter(
                    shift__warehouse__organization_id=org_id
                )
            else:
                queryset = CashTransaction.objects.all()
        else:
            queryset = CashTransaction.objects.filter(
                shift__warehouse__organization=self.request.auth
            )

        shift_id = self.kwargs.get('shift_pk')
        if shift_id:
            queryset = queryset.filter(shift_id=shift_id)
        return queryset

    def perform_create(self, serializer):
        """Attach the authenticated user and shift context on create."""
        shift_id = self.kwargs.get('shift_pk')
        if shift_id:
            serializer.save(shift_id=shift_id, created_by=self.request.user)
        else:
            serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """Prevent deleting transactions from closed shifts."""
        transaction = self.get_object()
        if transaction.shift.status == 'Closed':
            return Response(
                {'detail': 'Cannot delete transaction from closed shift'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)
