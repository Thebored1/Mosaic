"""
POS application views.

This module exposes the shift, checkout, and invoice presentation workflow
used by the point-of-sale screen. POS remains organization-scoped and uses the
existing sale and stock models for accounting and inventory posting.
"""

from decimal import Decimal
import logging

from django.core.exceptions import ValidationError
from rest_framework import decorators, status, viewsets
from rest_framework.response import Response
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema

from account.models import Organization
from sale.models import Invoice
from sale.serializers import InvoiceDetailSerializer, OrderSerializer, ReceiptSerializer

from configuration.authentication import (
    ECOMMERCE_MARKER,
    SUPER_ADMIN_MARKER,
    ScopedRolePermission,
)

from .models import CashTransaction, Shift
from .serializers import CashTransactionSerializer, POSCheckoutSerializer, ShiftSerializer
from .services import (
    build_invoice_document_payload,
    build_shift_reconciliation,
    checkout_pos_order,
)


logger = logging.getLogger(__name__)


def _get_request_organization(request):
    if not hasattr(request, 'auth') or request.auth is None or request.auth == ECOMMERCE_MARKER:
        return None
    if request.auth == SUPER_ADMIN_MARKER:
        org_id = request.query_params.get('organization')
        if not org_id:
            return None
        return Organization.objects.filter(pk=org_id).first()
    return request.auth


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
        except ValidationError as exc:
            detail = getattr(exc, 'message_dict', None) or getattr(exc, 'messages', None) or [str(exc)]
            return Response(
                {'detail': detail},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception:
            logger.exception('Unexpected error while closing shift %s', shift.pk)
            return Response(
                {'detail': 'Unexpected error while closing shift.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
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

    @decorators.action(detail=True, methods=['get'])
    def reconciliation(self, request, pk=None):
        """Return a POS reconciliation snapshot for the shift."""
        shift = self.get_object()
        return Response(build_shift_reconciliation(shift))


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


class POSCheckoutViewSet(viewsets.ViewSet):
    """Create a POS checkout, finalize the invoice, and return receipt data."""

    permission_classes = [ScopedRolePermission]
    permission_scope = 'pos_operations'

    @extend_schema(request=POSCheckoutSerializer, responses=OpenApiTypes.OBJECT)
    def create(self, request):
        serializer = POSCheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        validated = serializer.validated_data
        shift = validated['shift']
        business_location = validated.get('business_location') or shift.warehouse
        party = validated.get('party')

        try:
            order, invoice, receipt = checkout_pos_order(
                shift=shift,
                business_location=business_location,
                user=request.user,
                items=validated.get('items'),
                order_id=validated.get('order_id'),
                party=party,
                invoice_type=validated.get('invoice_type', 'Cash'),
                due_date=validated.get('due_date'),
                notes=validated.get('notes', ''),
                terms=validated.get('terms', ''),
                payment_mode=validated.get('payment_mode', 'Cash'),
                paid_amount=validated.get('paid_amount', Decimal('0')),
                reference_number=validated.get('reference_number', ''),
                receipt_notes=validated.get('receipt_notes', ''),
                discount_amount=validated.get('discount_amount', Decimal('0')),
                discount_type=validated.get('discount_type', 'Fixed'),
            )
        except ValidationError as exc:
            detail = exc.message_dict if hasattr(exc, 'message_dict') else {'detail': str(exc)}
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)

        payload = build_invoice_document_payload(invoice)
        response = {
            'order': OrderSerializer(order).data,
            'invoice': payload,
            'receipt': ReceiptSerializer(receipt).data if receipt else None,
        }
        return Response(response, status=status.HTTP_201_CREATED)


class POSInvoiceViewSet(viewsets.ViewSet):
    """Expose invoice print/share payloads for POS terminals."""

    queryset = Invoice.objects.select_related('party', 'business_location', 'billing_state', 'created_by').prefetch_related('items')
    serializer_class = InvoiceDetailSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'pos_operations'

    def _get_queryset(self, request):
        queryset = Invoice.objects.select_related('party', 'business_location', 'billing_state', 'created_by').prefetch_related('items')
        organization = _get_request_organization(request)
        if organization is None:
            if request.auth == SUPER_ADMIN_MARKER:
                return queryset.none()
            return queryset.none()
        return queryset.filter(business_location__organization=organization)

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def retrieve(self, request, pk=None):
        try:
            invoice = self._get_queryset(request).get(pk=pk)
        except Invoice.DoesNotExist:
            return Response({'detail': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(build_invoice_document_payload(invoice))

    @decorators.action(detail=True, methods=['get'])
    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def print_data(self, request, pk=None):
        try:
            invoice = self._get_queryset(request).get(pk=pk)
        except Invoice.DoesNotExist:
            return Response({'detail': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(build_invoice_document_payload(invoice))

    @decorators.action(detail=True, methods=['get'])
    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def share(self, request, pk=None):
        try:
            invoice = self._get_queryset(request).get(pk=pk)
        except Invoice.DoesNotExist:
            return Response({'detail': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)
        payload = build_invoice_document_payload(invoice)
        return Response({
            'invoice_id': invoice.id,
            'invoice_number': invoice.invoice_number,
            'share': payload['share'],
        })
