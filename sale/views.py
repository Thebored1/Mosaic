"""
Sale application views.

This module exposes the back-office sales, purchase, and reporting APIs used
by org users and super admins. The views here are the accounting backbone of
the system, so they enforce tenant boundaries and document transitions
carefully.
"""

import csv
import io
import logging
from rest_framework import serializers, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db.models import Sum, Count, Q
from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from django.http import HttpResponse
from decimal import Decimal
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer

from configuration.models import State, Warehouse as BusinessLocation
from configuration.authentication import SUPER_ADMIN_MARKER, ECOMMERCE_MARKER, ScopedRolePermission
from .models import (
    Party,
    Order, OrderItem,
    DeliveryChallan, DeliveryChallanItem,
    Invoice, InvoiceItem,
    CreditNote, Receipt,
    PurchaseOrder, PurchaseOrderItem,
    GoodReceiptNote, GRNItem,
    PurchaseInvoice, PurchaseInvoiceItem,
    DebitNote, PaymentOut,
    PriceList, Quotation
)
from .services import (
    generate_invoice_documents,
    generate_invoice_e_invoice,
    generate_invoice_e_way_bill,
    post_good_receipt_note,
)
from .serializers import (
    StateSerializer, BusinessLocationSerializer, PartySerializer,
    OrderSerializer, OrderCreateSerializer,
    DeliveryChallanListSerializer, DeliveryChallanDetailSerializer, DeliveryChallanCreateSerializer,
    InvoiceListSerializer, InvoiceDetailSerializer, InvoiceCreateSerializer,
    InvoiceItemSerializer,
    PriceListListSerializer, PriceListDetailSerializer, PriceListCreateSerializer,
    QuotationListSerializer, QuotationDetailSerializer, QuotationCreateSerializer,
    CreditNoteSerializer, ReceiptSerializer, ReceiptCreateSerializer,
    PurchaseOrderListSerializer, PurchaseOrderDetailSerializer,
    GRNListSerializer, GRNDetailSerializer,
    PurchaseInvoiceListSerializer, PurchaseInvoiceDetailSerializer,
    DebitNoteSerializer, PaymentOutSerializer
)


logger = logging.getLogger(__name__)


def _validation_detail(exc):
    """Normalize Django/DRF validation exceptions into response-safe payloads."""
    return getattr(exc, 'message_dict', None) or getattr(exc, 'detail', None) or getattr(exc, 'messages', None) or {'detail': str(exc)}


def csv_response(filename, rows):
    """Return a CSV export response."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    for row in rows:
        writer.writerow(row)
    response = HttpResponse(buffer.getvalue(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


class StandardPagination(PageNumberPagination):
    """Standard pagination for sale endpoints."""
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


def org_filter(qs, request):
    """Filter a queryset by the organization in the auth context."""
    if not hasattr(request, 'auth') or request.auth is None:
        return qs.none()
    if request.auth == ECOMMERCE_MARKER:
        return qs.none()

    model_fields = {field.name for field in qs.model._meta.get_fields()}

    if qs.model is State:
        if request.auth == SUPER_ADMIN_MARKER:
            org_id = request.query_params.get('organization')
            if org_id:
                return qs.filter(Q(organization_id=org_id) | Q(organization__isnull=True))
            return qs
        return qs.filter(Q(organization=request.auth) | Q(organization__isnull=True))

    if request.auth == SUPER_ADMIN_MARKER:
        org_id = request.query_params.get('organization')
        if not org_id:
            return qs.none()

        if 'organization' in model_fields:
            return qs.filter(organization_id=org_id)
        if 'business_location' in model_fields:
            return qs.filter(business_location__organization_id=org_id)
        if 'invoice' in model_fields:
            return qs.filter(invoice__business_location__organization_id=org_id)
        if 'purchase_invoice' in model_fields:
            return qs.filter(purchase_invoice__business_location__organization_id=org_id)
        return qs

    if 'organization' in model_fields:
        return qs.filter(organization=request.auth)
    if 'business_location' in model_fields:
        return qs.filter(business_location__organization=request.auth)
    if 'invoice' in model_fields:
        return qs.filter(invoice__business_location__organization=request.auth)
    if 'purchase_invoice' in model_fields:
        return qs.filter(purchase_invoice__business_location__organization=request.auth)
    return qs.none()


def finalized_invoices(qs):
    """Limit a queryset to finalized sales invoices."""
    return qs.filter(status='Finalized')


def format_report_decimal(value):
    """Format report totals without trailing zeroes for whole numbers."""
    if value is None:
        return '0'
    text = format(Decimal(value), 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def save_for_request_organization(serializer, request):
    """Save a model instance under the organization derived from the request."""
    model_fields = {field.name for field in serializer.Meta.model._meta.get_fields()}
    if 'organization' not in model_fields:
        return serializer.save()

    org_id = request.data.get('organization') or request.query_params.get('organization')

    if request.auth == SUPER_ADMIN_MARKER:
        if not org_id:
            raise ValidationError({'organization': 'organization is required for super admin writes'})
        return serializer.save(organization_id=org_id)
    if request.auth == ECOMMERCE_MARKER:
        raise ValidationError({'organization': 'Create or join an organization to access this feature'})

    return serializer.save(organization=request.auth)


def validate_related_organization(request, **relations):
    """Validate that related objects belong to the same organization."""
    if request.auth == SUPER_ADMIN_MARKER:
        return
    if request.auth == ECOMMERCE_MARKER:
        raise ValidationError({'organization': 'Create or join an organization to access this feature'})

    for field_name, related_obj in relations.items():
        if related_obj is None:
            continue

        if field_name == 'organization':
            related_org_id = getattr(related_obj, 'pk', related_obj)
            if related_org_id != request.auth.pk:
                raise ValidationError({field_name: f'{field_name} does not belong to the authenticated organization'})
            continue

        related_org_id = getattr(related_obj, 'organization_id', None)
        if related_org_id is None and hasattr(related_obj, 'business_location_id'):
            business_location = getattr(related_obj, 'business_location', None)
            related_org_id = getattr(business_location, 'organization_id', None)
        if related_org_id is None and hasattr(related_obj, 'invoice_id'):
            invoice = getattr(related_obj, 'invoice', None)
            business_location = getattr(invoice, 'business_location', None)
            related_org_id = getattr(business_location, 'organization_id', None)
        if related_org_id is None and hasattr(related_obj, 'purchase_invoice_id'):
            purchase_invoice = getattr(related_obj, 'purchase_invoice', None)
            business_location = getattr(purchase_invoice, 'business_location', None)
            related_org_id = getattr(business_location, 'organization_id', None)
        if related_org_id is not None and related_org_id != request.auth.pk:
            raise ValidationError({field_name: f'{field_name} does not belong to the authenticated organization'})


# ===================== MASTER DATA VIEWSETS =====================

class StateViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only GST state master."""
    queryset = State.objects.filter(is_active=True)
    serializer_class = StateSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'sale_state'
    pagination_class = StandardPagination
    filter_backends = [SearchFilter]
    search_fields = ['name', 'state_code']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)


class BusinessLocationViewSet(viewsets.ModelViewSet):
    """CRUD viewset for GST-registered business locations."""
    queryset = BusinessLocation.objects.all()
    serializer_class = BusinessLocationSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'sale_business_location'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_default', 'state', 'is_active']
    search_fields = ['legal_name', 'trade_name', 'gstin']
    ordering_fields = ['legal_name', 'gstin']
    ordering = ['legal_name']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def perform_create(self, serializer):
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        save_for_request_organization(serializer, self.request)


class PartyViewSet(viewsets.ModelViewSet):
    """CRUD viewset for the party master."""
    queryset = Party.objects.all()
    serializer_class = PartySerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'party_management'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['party_type', 'state', 'is_active']
    search_fields = ['name', 'gstin', 'phone', 'email']
    ordering_fields = ['name', 'party_type', 'created_at']
    ordering = ['name']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def perform_create(self, serializer):
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        save_for_request_organization(serializer, self.request)

    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export party masters as CSV."""
        rows = [['name', 'party_type', 'gstin', 'phone', 'email', 'is_active', 'credit_limit', 'opening_balance']]
        for party in self.get_queryset():
            rows.append([
                party.name,
                party.party_type,
                party.gstin,
                party.phone,
                party.email,
                str(party.is_active),
                str(party.credit_limit),
                str(party.opening_balance),
            ])
        return csv_response('parties.csv', rows)

    @action(detail=False, methods=['get'])
    def import_template(self, request):
        """Return a CSV template for party imports."""
        rows = [[
            'name', 'party_type', 'gstin', 'phone', 'email', 'address',
            'shipping_address', 'credit_limit', 'opening_balance', 'is_active'
        ]]
        return csv_response('parties-template.csv', rows)

    @action(detail=False, methods=['post'])
    def bulk_import(self, request):
        """Import party rows in a transaction with validation feedback."""
        rows = request.data.get('parties')
        if not isinstance(rows, list):
            raise ValidationError({'parties': 'parties must be a list of row objects'})

        errors = []
        prepared = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append({'row': index, 'error': 'Each row must be an object'})
                continue
            name = (row.get('name') or '').strip()
            if not name:
                errors.append({'row': index, 'error': 'name is required'})
                continue
            try:
                prepared.append({
                    'row': index,
                    'name': name,
                    'party_type': row.get('party_type', 'Customer'),
                    'gstin': (row.get('gstin') or '').upper(),
                    'phone': row.get('phone', ''),
                    'email': row.get('email', ''),
                    'address': row.get('address', ''),
                    'shipping_address': row.get('shipping_address', ''),
                    'credit_limit': Decimal(str(row.get('credit_limit', '0'))),
                    'opening_balance': Decimal(str(row.get('opening_balance', '0'))),
                    'is_active': str(row.get('is_active', True)).lower() not in {'false', '0', 'no'},
                })
            except Exception as exc:
                errors.append({'row': index, 'error': str(exc)})

        if errors:
            raise ValidationError({'rows': errors})

        imported = []
        org = request.auth if request.auth != SUPER_ADMIN_MARKER else None
        with transaction.atomic():
            for row in prepared:
                defaults = {
                    'party_type': row['party_type'],
                    'gstin': row['gstin'],
                    'phone': row['phone'],
                    'email': row['email'],
                    'address': row['address'],
                    'shipping_address': row['shipping_address'],
                    'credit_limit': row['credit_limit'],
                    'opening_balance': row['opening_balance'],
                    'is_active': row['is_active'],
                }
                party, _ = Party.objects.update_or_create(name=row['name'], organization=org, defaults=defaults)
                imported.append(PartySerializer(party).data)
        return Response({'imported': imported}, status=201)

    @action(detail=True, methods=['get'])
    def ledger(self, request, pk=None):
        """Get party ledger - all transactions with running balance."""
        party = self.get_object()

        # Sales invoices
        invoices = finalized_invoices(Invoice.objects.filter(party=party)).values(
            'invoice_number', 'invoice_date', 'grand_total'
        ).annotate(
            type=('Sales Invoice'), debit=('grand_total'), credit=(0)
        )

        # Receipts
        receipts = Receipt.objects.filter(
            party=party
        ).values('receipt_number', 'transaction_date', 'amount').annotate(
            type=('Receipt'), debit=(0), credit=('amount')
        )

        # Credit notes
        credit_notes = CreditNote.objects.filter(
            party=party, is_stock_returned=True
        ).values('credit_note_number', 'created_at', 'amount').annotate(
            type=('Credit Note'), debit=(0), credit=('amount')
        )

        # Combine and sort
        ledger = []
        running_balance = party.opening_balance

        for inv in invoices:
            running_balance += inv['grand_total']
            ledger.append({
                'date': inv['invoice_date'],
                'type': 'Sales Invoice',
                'reference': inv['invoice_number'],
                'debit': str(inv['grand_total']),
                'credit': '0.00',
                'balance': str(running_balance)
            })

        for rcpt in receipts:
            running_balance -= rcpt['amount']
            ledger.append({
                'date': rcpt['transaction_date'],
                'type': 'Receipt',
                'reference': rcpt['receipt_number'],
                'debit': '0.00',
                'credit': str(rcpt['amount']),
                'balance': str(running_balance)
            })

        for cn in credit_notes:
            running_balance -= cn['amount']
            ledger.append({
                'date': cn['created_at'],
                'type': 'Credit Note',
                'reference': cn['credit_note_number'],
                'debit': '0.00',
                'credit': str(cn['amount']),
                'balance': str(running_balance)
            })

        ledger.sort(key=lambda x: x['date'], reverse=True)

        return Response({
            'party': PartySerializer(party).data,
            'opening_balance': str(party.opening_balance),
            'current_outstanding': str(party.outstanding),
            'ledger': ledger
        })


# ===================== ORDER VIEWSETS =====================

class OrderViewSet(viewsets.ModelViewSet):
    """CRUD viewset for POS orders."""
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'sales_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'party', 'business_location']
    search_fields = ['order_number', 'hold_notes']
    ordering_fields = ['created_at', 'order_number']
    ordering = ['-created_at']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'create':
            return OrderCreateSerializer
        return OrderSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """Create a new POS order with nested items."""
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        validate_related_organization(
            request,
            business_location=serializer.validated_data.get('business_location'),
            party=serializer.validated_data.get('party'),
        )
        order = serializer.save()

        return Response(
            OrderSerializer(order).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['post'])
    def hold(self, request, pk=None):
        """Put the current POS order on hold."""
        order = self.get_object()
        if order.status != 'Billing':
            return Response(
                {'error': 'Only billing orders can be put on hold'},
                status=status.HTTP_400_BAD_REQUEST
            )

        order.status = 'Hold'
        order.hold_notes = request.data.get('hold_notes', '')
        order.save()

        return Response(OrderSerializer(order).data)

    @action(detail=True, methods=['post'])
    def recall(self, request, pk=None):
        """
        Recall Order
        ===========

        Purpose: Resume a held order for billing

        Request: POST /sale/orders/{id}/recall/

        Response: Order with status = 'Billing'

        Usage: Customer returns, continue billing process
        """
        order = self.get_object()
        if order.status != 'Hold':
            return Response(
                {'error': 'Only held orders can be recalled'},
                status=status.HTTP_400_BAD_REQUEST
            )

        order.status = 'Billing'
        order.save()

        return Response(OrderSerializer(order).data)

    @action(detail=True, methods=['post'])
    def convert(self, request, pk=None):
        """
        Convert Order to Invoice
        ========================

        Purpose: Finalize POS order as invoice

        Request: POST /sale/orders/{id}/convert/

        Optional Payload:
        {
            "invoice_type": "Tax Invoice",
            "due_date": "2025-05-30",
            "notes": "Thank you for your business"
        }

        Workflow:
        ┌─────────────────────────────────────────────────────────────────────┐
        │  1. Creates Invoice with:                                          │
        │     - business_location from order                                │
        │     - party from order                                             │
        │     - invoice_type (default: Tax Invoice)                         │
        │                                                                     │
        │  2. For each OrderItem:                                             │
        │     - Creates InvoiceItem                                          │
        │     - Calculates taxable_amount = qty × rate - discount           │
        │     - Determines CGST/SGST vs IGST based on state                 │
        │     - Calculates tax amounts                                        │
        │                                                                     │
        │  3. Invoice totals calculated                                      │
        │  4. Order.status = 'Invoiced'                                      │
        │  5. Returns Invoice ID for finalization                           │
        └─────────────────────────────────────────────────────────────────────┘

        Response: {"invoice_id": 123, "invoice_number": "27AAAAA0000A1Z5/2025-26/00001"}
        """
        order = self.get_object()
        if order.status not in ['Billing', 'Hold']:
            return Response(
                {'error': 'Only billing or held orders can be converted'},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # Create invoice
            invoice_data = {
                'invoice_type': request.data.get('invoice_type', 'Tax Invoice'),
                'party': order.party,
                'business_location': order.business_location,
                'due_date': request.data.get('due_date'),
                'tcs_rate': request.data.get('tcs_rate', 0),
                'notes': request.data.get('notes', ''),
                'order': order
            }

            # Determine billing state
            if order.party and order.party.state:
                invoice_data['billing_state'] = order.party.state
            elif getattr(order.business_location, 'state_id', None):
                invoice_data['billing_state'] = order.business_location.state
            else:
                raise ValidationError({
                    'billing_state': 'A billing state is required to convert a walk-in order.'
                })

            invoice = Invoice.objects.create(**invoice_data)

            # Create invoice items from order items
            for order_item in order.order_items.all():
                taxable = (order_item.quantity * order_item.rate) - order_item.discount

                inv_item = InvoiceItem.objects.create(
                    invoice=invoice,
                    item=order_item.item,
                    item_variant=order_item.item_variant,
                    hsn_code=order_item.hsn_code,
                    quantity=order_item.quantity,
                    unit=order_item.unit,
                    rate=order_item.rate,
                    discount=order_item.discount,
                    taxable_amount=taxable
                )

            invoice.save()

            # Update order status
            order.status = 'Invoiced'
            order.save()

        return Response({
            'invoice_id': invoice.id,
            'invoice_number': invoice.invoice_number,
            'message': 'Order converted to invoice. Finalize to complete.'
        })


# ===================== CHALLAN VIEWSETS =====================

class DeliveryChallanViewSet(viewsets.ModelViewSet):
    """CRUD viewset for delivery challans that can be consolidated into invoices."""

    queryset = DeliveryChallan.objects.all()
    permission_classes = [ScopedRolePermission]
    permission_scope = 'sales_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['party', 'business_location', 'status']
    search_fields = ['challan_number', 'party__name']
    ordering_fields = ['challan_date', 'challan_number']
    ordering = ['-challan_date', '-id']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'list':
            return DeliveryChallanListSerializer
        if self.action in ['create', 'update', 'partial_update']:
            return DeliveryChallanCreateSerializer
        return DeliveryChallanDetailSerializer

    def _ensure_editable(self, challan):
        """Block edits once a challan has been invoiced or cancelled."""
        if challan.status in {'Invoiced', 'Cancelled'}:
            raise ValidationError({'challan': 'Invoiced or cancelled challans cannot be edited.'})

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validate_related_organization(
            request,
            party=serializer.validated_data.get('party'),
            business_location=serializer.validated_data.get('business_location'),
        )
        challan = save_for_request_organization(serializer, request)
        return Response(DeliveryChallanDetailSerializer(challan).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        raise ValidationError({'challan': 'Delivery challans are immutable after creation.'})

    def partial_update(self, request, *args, **kwargs):
        raise ValidationError({'challan': 'Delivery challans are immutable after creation.'})

    def destroy(self, request, *args, **kwargs):
        challan = self.get_object()
        self._ensure_editable(challan)
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['post'])
    @transaction.atomic
    def combine(self, request):
        """Combine multiple challans into a single invoice."""
        challan_ids = request.data.get('challans', [])
        if not challan_ids:
            raise ValidationError({'challans': 'At least one challan is required.'})
        if len(set(str(cid) for cid in challan_ids)) != len(challan_ids):
            raise ValidationError({'challans': 'Duplicate challan ids are not allowed.'})

        challans = list(self.get_queryset().filter(id__in=challan_ids).prefetch_related('items'))
        if len(challans) != len(set(int(cid) for cid in challan_ids)):
            raise ValidationError({'challans': 'One or more challans were not found.'})
        if any(ch.status == 'Invoiced' for ch in challans):
            raise ValidationError({'challans': 'Already invoiced challans cannot be combined again.'})

        party = challans[0].party
        business_location = challans[0].business_location
        if any(ch.party_id != party.id for ch in challans):
            raise ValidationError({'challans': 'All challans must belong to the same party.'})
        if any(ch.business_location_id != business_location.id for ch in challans):
            raise ValidationError({'challans': 'All challans must belong to the same business location.'})
        if party is None:
            raise ValidationError({'challans': 'A party is required to combine challans.'})
        if business_location is None:
            raise ValidationError({'challans': 'A business location is required to combine challans.'})

        billing_state = party.state or business_location.state
        if billing_state is None:
            raise ValidationError({'billing_state': 'A billing state is required to combine challans into an invoice.'})

        invoice = Invoice.objects.create(
            invoice_type=request.data.get('invoice_type', 'Tax Invoice'),
            party=party,
            business_location=business_location,
            billing_state=billing_state,
            due_date=request.data.get('due_date'),
            notes=request.data.get('notes', ''),
            terms=request.data.get('terms', ''),
            tcs_rate=request.data.get('tcs_rate', 0),
            created_by=request.user,
        )

        for challan in challans:
            for challan_item in challan.items.all():
                InvoiceItem.objects.create(
                    invoice=invoice,
                    item=challan_item.item,
                    item_variant=challan_item.item_variant,
                    source_challan=challan,
                    hsn_code=challan_item.hsn_code,
                    quantity=challan_item.quantity,
                    unit=challan_item.unit,
                    rate=challan_item.rate,
                    discount=challan_item.discount,
                )
            challan.status = 'Invoiced'
            challan.save(update_fields=['status'])

        invoice.source_challans.set(challans)
        invoice.calculate_totals()
        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_201_CREATED)


# ===================== INVOICE VIEWSETS =====================

class InvoiceViewSet(viewsets.ModelViewSet):
    """
    Invoice API - Sales Tax Invoice / Bill of Supply
    ================================================

    Purpose: Manage sales invoices with GST compliance

    Endpoints:
    - GET /sale/invoices/ - List all invoices (paginated)
    - POST /sale/invoices/ - Create new invoice (draft)
    - GET /sale/invoices/{id}/ - Get invoice detail
    - PUT /sale/invoices/{id}/ - Update draft invoice
    - DELETE /sale/invoices/{id}/ - Delete draft invoice

    Custom Actions:
    - POST /sale/invoices/{id}/finalize/ - Finalize & deduct stock
    - POST /sale/invoices/{id}/cancel/ - Cancel finalized invoice
    - GET /sale/invoices/{id}/print/ - Get print-friendly data

    Finalization Workflow:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  Finalize Invoice: POST /sale/invoices/{id}/finalize/            │
    │                                                                     │
    │  1. Validates: status = 'Draft'                                 │
    │  2. Credit Check: party.outstanding + grand_total <= credit_limit │
    │  3. For each InvoiceItem:                                         │
    │     - Creates StockMovement (type='Sale')                         │
    │     - Reduces item.current_stock or variant.current_stock         │
    │     - If batch linked: reduces batch.quantity_remaining          │
    │  4. Updates: status = 'Finalized', invoice_date = now           │
    │  5. Updates Order: if linked, status = 'Invoiced'                │
    │                                                                     │
    │  After Finalize:                                                   │
    │  - Stock reduced, cannot edit                                      │
    │  - Can create Receipt against this invoice                        │
    │  - Can create CreditNote for returns                              │
    └─────────────────────────────────────────────────────────────────────┘

    Cancellation Workflow:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  Cancel Invoice: POST /sale/invoices/{id}/cancel/                 │
    │                                                                     │
    │  1. Validates: status = 'Finalized'                              │
    │  2. Creates CreditNote automatically (optional)                   │
    │  3. For each InvoiceItem:                                          │
    │     - Creates StockMovement (type='Return')                       │
    │     - Adds stock back to inventory                                 │
    │  4. Updates: status = 'Cancelled'                                │
    │  5. Keeps audit trail (invoice number preserved, marked cancel)  │
    └─────────────────────────────────────────────────────────────────────┘

    Filtering:
    - By party: /sale/invoices/?party=1
    - By status: /sale/invoices/?status=Finalized
    - By date: /sale/invoices/?invoice_date_after=2025-04-01
    - By location: /sale/invoices/?business_location=1
    - Search: /sale/invoices/?search=INV001
    """
    queryset = Invoice.objects.all()
    permission_classes = [ScopedRolePermission]
    permission_scope = 'sales_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['party', 'business_location', 'billing_state', 'status', 'invoice_type']
    search_fields = ['invoice_number', 'party__name']
    ordering_fields = ['invoice_date', 'invoice_number', 'grand_total']
    ordering = ['-invoice_date', '-id']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'list':
            return InvoiceListSerializer
        if self.action == 'create':
            return InvoiceCreateSerializer
        return InvoiceDetailSerializer

    def perform_create(self, serializer):
        validate_related_organization(
            self.request,
            business_location=serializer.validated_data.get('business_location'),
            party=serializer.validated_data.get('party'),
        )
        serializer.save()

    @action(detail=True, methods=['post'])
    def finalize(self, request, pk=None):
        """
        Finalize Invoice
        ================

        Purpose: Complete sale, deduct stock, make invoice valid

        Request: POST /sale/invoices/{id}/finalize/

        Response: {"message": "Invoice finalized successfully", "invoice_number": "..."}

        Side Effects:
        - StockMovement records created for each item
        - Item/variant current_stock reduced
        - Party outstanding updated
        """
        invoice = self.get_object()

        if invoice.status != 'Draft':
            return Response(
                {'error': f'Cannot finalize invoice with status {invoice.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Credit check
        if invoice.party and invoice.party.credit_limit > 0:
            projected = invoice.party.outstanding + invoice.grand_total
            if projected > invoice.party.credit_limit:
                return Response(
                    {'warning': f'Party exceeds credit limit. Outstanding: {invoice.party.outstanding}, Invoice: {invoice.grand_total}, Limit: {invoice.party.credit_limit}'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        try:
            invoice.finalize()
            return Response({
                'message': 'Invoice finalized successfully',
                'invoice_number': invoice.invoice_number
            })
        except (ValidationError, DjangoValidationError) as exc:
            return Response(
                _validation_detail(exc),
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception:
            logger.exception(
                'Unexpected error finalizing invoice %s for organization %s',
                invoice.pk,
                getattr(invoice.business_location, 'organization_id', None),
            )
            return Response({'detail': 'Unexpected error while finalizing invoice.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """
        Cancel Invoice
        ==============

        Purpose: Cancel a finalized invoice

        Request: POST /sale/invoices/{id}/cancel/

        Side Effects:
        - Creates StockMovement (Return) to restore inventory
        - Updates party outstanding
        - Invoice remains in system (marked cancelled)
        """
        invoice = self.get_object()

        if invoice.status != 'Finalized':
            return Response(
                {'error': 'Only finalized invoices can be cancelled'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            invoice.cancel(request.user)
            return Response({'message': 'Invoice cancelled successfully'})
        except (ValidationError, DjangoValidationError) as exc:
            return Response(
                _validation_detail(exc),
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception:
            logger.exception(
                'Unexpected error cancelling invoice %s for organization %s',
                invoice.pk,
                getattr(invoice.business_location, 'organization_id', None),
            )
            return Response({'detail': 'Unexpected error while cancelling invoice.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'])
    def print_data(self, request, pk=None):
        """Get print-friendly invoice data."""
        invoice = self.get_object()
        serializer = InvoiceDetailSerializer(invoice)

        # Add print-specific calculations
        data = serializer.data
        data['print'] = {
            'amount_in_words': self._number_to_words(invoice.grand_total),
            'company_name': invoice.business_location.legal_name,
            'company_gstin': invoice.business_location.gstin,
            'company_address': invoice.business_location.address,
        }

        return Response(data)

    @action(detail=True, methods=['post'])
    @extend_schema(
        responses=inline_serializer(
            name='InvoiceEWayBillResponse',
            fields={
                'message': serializers.CharField(),
                'e_way_bill': serializers.CharField(),
            },
        )
    )
    def generate_e_way_bill(self, request, pk=None):
        """Generate and store a placeholder e-way bill number."""
        invoice = self.get_object()
        try:
            e_way_bill = generate_invoice_e_way_bill(invoice)
        except (ValidationError, DjangoValidationError) as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        return Response({'message': 'E-way bill generated successfully', 'e_way_bill': e_way_bill})

    @action(detail=True, methods=['post'])
    @extend_schema(
        responses=inline_serializer(
            name='InvoiceEInvoiceResponse',
            fields={
                'message': serializers.CharField(),
                'e_invoice_details': serializers.JSONField(),
            },
        )
    )
    def generate_e_invoice(self, request, pk=None):
        """Generate and store a placeholder e-invoice payload."""
        invoice = self.get_object()
        try:
            details = generate_invoice_e_invoice(invoice)
        except (ValidationError, DjangoValidationError) as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        return Response({'message': 'E-invoice generated successfully', 'e_invoice_details': details})

    @action(detail=True, methods=['post'])
    @extend_schema(
        responses=inline_serializer(
            name='InvoiceDocumentsResponse',
            fields={
                'message': serializers.CharField(),
                'e_way_bill': serializers.CharField(),
                'e_invoice_details': serializers.JSONField(),
            },
        )
    )
    def generate_documents(self, request, pk=None):
        """Generate both e-way bill and e-invoice data in one call."""
        invoice = self.get_object()
        try:
            payload = generate_invoice_documents(invoice)
        except ValidationError as exc:
            raise ValidationError(getattr(exc, 'message_dict', exc.messages))
        return Response({
            'message': 'GST documents generated successfully',
            'e_way_bill': payload['e_way_bill'],
            'e_invoice_details': payload['e_invoice_details'],
        })

    def _number_to_words(self, number):
        """Convert number to words for bill printing."""
        # Simple implementation - can be enhanced
        ones = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine']
        tens = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']
        words = []

        num = int(number)
        if num == 0:
            return 'Zero'

        if num < 10:
            words.append(ones[num])
        elif num < 20:
            words.append(['Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen'][num - 10])
        else:
            words.append(tens[num // 10])
            if num % 10:
                words.append(ones[num % 10])

        return ' '.join(words) + ' Only'


class ReceiptViewSet(viewsets.ModelViewSet):
    """CRUD viewset for customer receipts."""
    queryset = Receipt.objects.all()
    serializer_class = ReceiptSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'sales_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['party', 'payment_mode', 'business_location']
    search_fields = ['receipt_number']
    ordering_fields = ['transaction_date', 'receipt_number']
    ordering = ['-transaction_date']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'create':
            return ReceiptCreateSerializer
        return ReceiptSerializer

    def perform_create(self, serializer):
        validate_related_organization(
            self.request,
            party=serializer.validated_data.get('party'),
            business_location=serializer.validated_data.get('business_location'),
        )
        serializer.save(received_by=self.request.user)


class CreditNoteViewSet(viewsets.ModelViewSet):
    """CRUD viewset for sales credit notes."""
    queryset = CreditNote.objects.all()
    serializer_class = CreditNoteSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'sales_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['invoice', 'is_stock_returned']
    search_fields = ['credit_note_number']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    """CRUD viewset for purchase orders."""
    queryset = PurchaseOrder.objects.all()
    permission_classes = [ScopedRolePermission]
    permission_scope = 'purchase_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['supplier', 'business_location', 'status']
    search_fields = ['po_number', 'supplier__name']
    ordering_fields = ['order_date', 'po_number']
    ordering = ['-order_date']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'list':
            return PurchaseOrderListSerializer
        return PurchaseOrderDetailSerializer

    def perform_create(self, serializer):
        validate_related_organization(
            self.request,
            supplier=serializer.validated_data.get('supplier'),
            business_location=serializer.validated_data.get('business_location'),
        )
        serializer.save()

    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Mark PO as sent to supplier."""
        po = self.get_object()
        po.status = 'Sent'
        po.save()
        return Response({'message': 'PO marked as sent', 'status': po.status})


class GoodReceiptNoteViewSet(viewsets.ModelViewSet):
    """CRUD viewset for goods receipt notes."""
    queryset = GoodReceiptNote.objects.all()
    permission_classes = [ScopedRolePermission]
    permission_scope = 'purchase_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['supplier', 'business_location', 'purchase_order']
    search_fields = ['grn_number', 'supplier_invoice_number']
    ordering_fields = ['received_date', 'grn_number']
    ordering = ['-received_date']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'list':
            return GRNListSerializer
        return GRNDetailSerializer

    def perform_create(self, serializer):
        validate_related_organization(
            self.request,
            supplier=serializer.validated_data.get('supplier'),
            business_location=serializer.validated_data.get('business_location'),
            purchase_order=serializer.validated_data.get('purchase_order'),
        )
        serializer.save()

    def update(self, request, *args, **kwargs):
        grn = self.get_object()
        if grn.status != 'Draft':
            raise ValidationError({'grn': 'Posted GRNs cannot be edited.'})
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        grn = self.get_object()
        if grn.status != 'Draft':
            raise ValidationError({'grn': 'Posted GRNs cannot be deleted.'})
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def create_invoice(self, request, pk=None):
        """Create purchase invoice from GRN."""
        grn = self.get_object()
        if grn.purchase_invoices.exists():
            return Response({'error': 'GRN already has a purchase invoice.'}, status=400)

        with transaction.atomic():
            post_good_receipt_note(grn, user=request.user)

            # Create purchase invoice from GRN
            pi = PurchaseInvoice.objects.create(
                supplier=grn.supplier,
                business_location=grn.business_location,
                grn=grn,
                purchase_order=grn.purchase_order,
                supplier_invoice_number=request.data.get('supplier_invoice_number', grn.supplier_invoice_number),
                supplier_invoice_date=request.data.get('supplier_invoice_date', grn.supplier_invoice_date),
                created_by=request.user
            )

            # Copy items from GRN
            for grn_item in grn.grn_items.all():
                taxable = (grn_item.quantity * grn_item.rate)

                PurchaseInvoiceItem.objects.create(
                    purchase_invoice=pi,
                    item=grn_item.item,
                    item_variant=grn_item.item_variant,
                    quantity=grn_item.quantity,
                    unit=grn_item.unit,
                    rate=grn_item.rate,
                    taxable_amount=taxable
                )

            pi.calculate_totals()
            pi.save()

        return Response({
            'purchase_invoice_id': pi.id,
            'purchase_invoice_number': pi.invoice_number
        })


class PurchaseInvoiceViewSet(viewsets.ModelViewSet):
    """CRUD viewset for purchase invoices."""
    queryset = PurchaseInvoice.objects.all()
    permission_classes = [ScopedRolePermission]
    permission_scope = 'purchase_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['supplier', 'business_location', 'status']
    search_fields = ['invoice_number', 'supplier_invoice_number']
    ordering_fields = ['invoice_date', 'invoice_number']
    ordering = ['-invoice_date']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'list':
            return PurchaseInvoiceListSerializer
        return PurchaseInvoiceDetailSerializer

    def perform_create(self, serializer):
        validate_related_organization(
            self.request,
            supplier=serializer.validated_data.get('supplier'),
            business_location=serializer.validated_data.get('business_location'),
            grn=serializer.validated_data.get('grn'),
            purchase_order=serializer.validated_data.get('purchase_order'),
        )
        serializer.save()

    @action(detail=True, methods=['post'])
    def finalize(self, request, pk=None):
        """Finalize purchase invoice - add stock."""
        pi = self.get_object()

        if pi.status != 'Draft':
            return Response({'error': f'Cannot finalize with status {pi.status}'}, status=400)

        try:
            pi.finalize()
            return Response({'message': 'Purchase invoice finalized'})
        except ValidationError as exc:
            return Response(_validation_detail(exc), status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception(
                'Unexpected error finalizing purchase invoice %s for organization %s',
                pi.pk,
                getattr(pi.business_location, 'organization_id', None),
            )
            return Response({'detail': 'Unexpected error while finalizing purchase invoice.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel purchase invoice."""
        pi = self.get_object()

        if pi.status != 'Finalized':
            return Response({'error': 'Only finalized can be cancelled'}, status=400)

        try:
            pi.cancel(user=request.user)
        except ValidationError as exc:
            return Response(_validation_detail(exc), status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception(
                'Unexpected error cancelling purchase invoice %s for organization %s',
                pi.pk,
                getattr(pi.business_location, 'organization_id', None),
            )
            return Response({'detail': 'Unexpected error while cancelling purchase invoice.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({'message': 'Purchase invoice cancelled'})


class DebitNoteViewSet(viewsets.ModelViewSet):
    """CRUD viewset for debit notes used in purchase returns."""
    queryset = DebitNote.objects.all()
    serializer_class = DebitNoteSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'purchase_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['purchase_invoice', 'supplier']
    ordering = ['-created_at']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)


class PaymentOutViewSet(viewsets.ModelViewSet):
    """CRUD viewset for payments made to suppliers."""
    queryset = PaymentOut.objects.all()
    serializer_class = PaymentOutSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'purchase_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['supplier', 'business_location']
    search_fields = ['payment_number', 'reference_number']
    ordering = ['-transaction_date']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def perform_create(self, serializer):
        validate_related_organization(
            self.request,
            supplier=serializer.validated_data.get('supplier'),
            business_location=serializer.validated_data.get('business_location'),
            purchase_invoice=serializer.validated_data.get('purchase_invoice'),
        )
        serializer.save()


# ===================== REPORTS =====================

@extend_schema_view(
    daily_sales=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gst_register=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gstr1=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gstr2=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    gst_liability=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
    itc_reconciliation=extend_schema(request=None, responses=OpenApiTypes.OBJECT),
)
class ReportsViewSet(viewsets.ViewSet):
    """Reporting viewset for operational and GST summaries."""

    permission_classes = [ScopedRolePermission]
    permission_scope = 'reporting'

    @action(detail=False, methods=['get'])
    def daily_sales(self, request):
        """Daily sales summary."""
        date = request.query_params.get('date', timezone.now().date())

        invoices = org_filter(finalized_invoices(Invoice.objects.filter(
            invoice_date__date=date
        )), request).aggregate(
            total_sales=Sum('grand_total'),
            total_tax=Sum('cgst_amount') + Sum('sgst_amount') + Sum('igst_amount'),
            total_items=Count('id')
        )

        receipts = org_filter(Receipt.objects.filter(
            transaction_date__date=date
        ), request).aggregate(
            total_receipts=Sum('amount')
        )

        return Response({
            'date': str(date),
            'invoice_count': invoices['total_items'] or 0,
            'total_sales': format_report_decimal(invoices['total_sales']),
            'total_tax': format_report_decimal(invoices['total_tax']),
            'total_receipts': format_report_decimal(receipts['total_receipts']),
        })

    @action(detail=False, methods=['get'])
    def gst_register(self, request):
        """GST Sales Register - grouped by tax rate."""
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')

        invoices = org_filter(finalized_invoices(Invoice.objects.all()), request)

        if start_date:
            invoices = invoices.filter(invoice_date__date__gte=start_date)
        if end_date:
            invoices = invoices.filter(invoice_date__date__lte=end_date)

        register = invoices.values('invoice_type').annotate(
            count=Count('id'),
            total=Sum('grand_total'),
            taxable=Sum('taxable_amount'),
            cgst=Sum('cgst_amount'),
            sgst=Sum('sgst_amount'),
            igst=Sum('igst_amount')
        )

        return Response({'register': list(register)})

    @action(detail=False, methods=['get'])
    def gstr1(self, request):
        """GSTR-1 format export."""
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')

        invoices = org_filter(finalized_invoices(Invoice.objects.filter(
            invoice_type__in=['Tax Invoice', 'Export', 'SEZ']
        )), request)

        if start_date:
            invoices = invoices.filter(invoice_date__date__gte=start_date)
        if end_date:
            invoices = invoices.filter(invoice_date__date__lte=end_date)

        data = []
        for inv in invoices:
            for item in inv.items.all():
                data.append({
                    'invoice_number': inv.invoice_number,
                    'invoice_date': inv.invoice_date.strftime('%Y-%m-%d'),
                    'party_gstin': inv.party.gstin if inv.party else '',
                    'party_name': inv.party.name if inv.party else '',
                    'place_of_supply': inv.billing_state.name if inv.billing_state else '',
                    'hsn_code': item.hsn_code,
                    'quantity': str(item.quantity),
                    'rate': str(item.rate),
                    'taxable_value': str(item.taxable_amount),
                    'cgst_rate': str(item.cgst_rate),
                    'cgst_amount': str(item.cgst_amount),
                    'sgst_rate': str(item.sgst_rate),
                    'sgst_amount': str(item.sgst_amount),
                    'igst_rate': str(item.igst_rate),
                    'igst_amount': str(item.igst_amount),
                })

        return Response({'gstr1_data': data})

    @action(detail=False, methods=['get'])
    def gstr2(self, request):
        """GSTR-2 format inward supply summary."""
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        purchases = org_filter(PurchaseInvoice.objects.filter(status='Finalized'), request)
        if start_date:
            purchases = purchases.filter(invoice_date__date__gte=start_date)
        if end_date:
            purchases = purchases.filter(invoice_date__date__lte=end_date)

        rows = []
        for pi in purchases.select_related('supplier'):
            rows.append({
                'invoice_number': pi.invoice_number,
                'invoice_date': pi.invoice_date.strftime('%Y-%m-%d'),
                'supplier_name': pi.supplier.name if pi.supplier else '',
                'supplier_gstin': pi.supplier.gstin if pi.supplier else '',
                'taxable_amount': str(pi.taxable_amount),
                'cgst_amount': str(pi.cgst_amount),
                'sgst_amount': str(pi.sgst_amount),
                'igst_amount': str(pi.igst_amount),
                'grand_total': str(pi.grand_total),
            })
        return Response({'gstr2_data': rows})

    @action(detail=False, methods=['get'])
    def gst_liability(self, request):
        """Return a simple GST liability summary."""
        sales = org_filter(finalized_invoices(Invoice.objects.all()), request).aggregate(
            taxable=Sum('taxable_amount'),
            cgst=Sum('cgst_amount'),
            sgst=Sum('sgst_amount'),
            igst=Sum('igst_amount'),
        )
        purchases = org_filter(PurchaseInvoice.objects.filter(status='Finalized'), request).aggregate(
            taxable=Sum('taxable_amount'),
            cgst=Sum('cgst_amount'),
            sgst=Sum('sgst_amount'),
            igst=Sum('igst_amount'),
        )
        sales_tax = (sales['cgst'] or Decimal('0')) + (sales['sgst'] or Decimal('0')) + (sales['igst'] or Decimal('0'))
        input_tax = (purchases['cgst'] or Decimal('0')) + (purchases['sgst'] or Decimal('0')) + (purchases['igst'] or Decimal('0'))
        return Response({
            'sales_tax': str(sales_tax),
            'input_tax_credit': str(input_tax),
            'net_liability': str(sales_tax - input_tax),
        })

    @action(detail=False, methods=['get'])
    def itc_reconciliation(self, request):
        """Return a basic input tax credit reconciliation summary."""
        purchases = org_filter(PurchaseInvoice.objects.filter(status='Finalized'), request)
        debit_notes = org_filter(DebitNote.objects.all(), request)
        itc = purchases.aggregate(
            cgst=Sum('cgst_amount'),
            sgst=Sum('sgst_amount'),
            igst=Sum('igst_amount'),
        )
        reversed_itc = debit_notes.aggregate(
            cgst=Sum('amount'),
        )
        total_itc = (itc['cgst'] or Decimal('0')) + (itc['sgst'] or Decimal('0')) + (itc['igst'] or Decimal('0'))
        return Response({
            'eligible_itc': str(total_itc),
            'reversed_itc': str(reversed_itc['cgst'] or Decimal('0')),
            'net_itc': str(total_itc - (reversed_itc['cgst'] or Decimal('0'))),
        })


class PriceListViewSet(viewsets.ModelViewSet):
    """CRUD viewset for tenant price lists."""

    queryset = PriceList.objects.all()
    permission_classes = [ScopedRolePermission]
    permission_scope = 'sales_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'effective_from', 'effective_to']
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'effective_from', 'created_at']
    ordering = ['-is_active', 'name']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'list':
            return PriceListListSerializer
        if self.action in ['create', 'update', 'partial_update']:
            return PriceListCreateSerializer
        return PriceListDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validate_related_organization(
            request,
            organization=serializer.validated_data.get('organization'),
        )
        price_list = save_for_request_organization(serializer, request)
        return Response(PriceListDetailSerializer(price_list).data, status=status.HTTP_201_CREATED)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        validate_related_organization(
            request,
            organization=serializer.validated_data.get('organization', instance.organization),
        )
        price_list = save_for_request_organization(serializer, request)
        return Response(PriceListDetailSerializer(price_list).data)


class QuotationViewSet(viewsets.ModelViewSet):
    """CRUD viewset for quotations and conversion actions."""

    queryset = Quotation.objects.all()
    permission_classes = [ScopedRolePermission]
    permission_scope = 'sales_operations'
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'party', 'business_location', 'price_list']
    search_fields = ['quotation_number', 'party__name', 'notes']
    ordering_fields = ['quotation_date', 'quotation_number', 'grand_total']
    ordering = ['-quotation_date', '-id']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'list':
            return QuotationListSerializer
        if self.action in ['create', 'update', 'partial_update']:
            return QuotationCreateSerializer
        return QuotationDetailSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validate_related_organization(
            request,
            organization=serializer.validated_data.get('organization'),
            party=serializer.validated_data.get('party'),
            business_location=serializer.validated_data.get('business_location'),
            price_list=serializer.validated_data.get('price_list'),
        )
        quotation = save_for_request_organization(serializer, request)
        return Response(QuotationDetailSerializer(quotation).data, status=status.HTTP_201_CREATED)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        if instance.status in ['Converted', 'Cancelled']:
            raise ValidationError({'quotation': 'Converted or cancelled quotations cannot be edited.'})
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        validate_related_organization(
            request,
            organization=serializer.validated_data.get('organization', instance.organization),
            party=serializer.validated_data.get('party', instance.party),
            business_location=serializer.validated_data.get('business_location', instance.business_location),
            price_list=serializer.validated_data.get('price_list', instance.price_list),
        )
        quotation = save_for_request_organization(serializer, request)
        return Response(QuotationDetailSerializer(quotation).data)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status in ['Converted', 'Cancelled']:
            raise ValidationError({'quotation': 'Converted or cancelled quotations cannot be deleted.'})
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def convert_to_order(self, request, pk=None):
        quotation = self.get_object()
        if quotation.status not in ['Draft', 'Sent', 'Accepted']:
            return Response({'error': f'Cannot convert quotation with status {quotation.status}'}, status=status.HTTP_400_BAD_REQUEST)
        if quotation.converted_order_id or quotation.converted_invoice_id:
            return Response({'error': 'Quotation has already been converted.'}, status=status.HTTP_400_BAD_REQUEST)
        if not quotation.items.exists():
            return Response({'error': 'Quotation must contain at least one item.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            order = Order.objects.create(
                party=quotation.party,
                business_location=quotation.business_location,
                discount_amount=quotation.discount_amount,
                discount_type=quotation.discount_type,
                discount_percent=quotation.discount_percent,
                created_by=request.user,
            )

            for quotation_item in quotation.items.select_related('item', 'item_variant', 'unit').all():
                OrderItem.objects.create(
                    order=order,
                    item=quotation_item.item,
                    item_variant=quotation_item.item_variant,
                    hsn_code=quotation_item.item.tax_code.code if quotation_item.item.tax_code else '',
                    quantity=quotation_item.quantity,
                    unit=quotation_item.unit,
                    rate=quotation_item.rate,
                    discount=quotation_item.discount,
                )

            quotation.converted_order = order
            quotation.status = 'Converted'
            quotation.save(update_fields=['converted_order', 'status', 'updated_at'])

        return Response({
            'quotation_id': quotation.id,
            'quotation_number': quotation.quotation_number,
            'order_id': order.id,
            'order_number': order.order_number,
            'message': 'Quotation converted to order.',
        })

    @action(detail=True, methods=['post'])
    def convert_to_invoice(self, request, pk=None):
        quotation = self.get_object()
        if quotation.status not in ['Draft', 'Sent', 'Accepted']:
            return Response({'error': f'Cannot convert quotation with status {quotation.status}'}, status=status.HTTP_400_BAD_REQUEST)
        if quotation.converted_order_id or quotation.converted_invoice_id:
            return Response({'error': 'Quotation has already been converted.'}, status=status.HTTP_400_BAD_REQUEST)
        if not quotation.items.exists():
            return Response({'error': 'Quotation must contain at least one item.'}, status=status.HTTP_400_BAD_REQUEST)

        billing_state = quotation.party.state if quotation.party and quotation.party.state else quotation.business_location.state
        if billing_state is None:
            return Response({'error': 'A billing state is required to convert the quotation.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            invoice = Invoice.objects.create(
                invoice_type=request.data.get('invoice_type', 'Tax Invoice'),
                party=quotation.party,
                billing_state=billing_state,
                business_location=quotation.business_location,
                due_date=request.data.get('due_date'),
                discount_amount=quotation.discount_amount,
                discount_type='Fixed',
                notes=request.data.get('notes', quotation.notes),
                terms=request.data.get('terms', quotation.terms),
                created_by=request.user,
            )

            for quotation_item in quotation.items.select_related('item', 'item_variant', 'unit').all():
                InvoiceItem.objects.create(
                    invoice=invoice,
                    item=quotation_item.item,
                    item_variant=quotation_item.item_variant,
                    hsn_code=quotation_item.item.tax_code.code if quotation_item.item.tax_code else '',
                    quantity=quotation_item.quantity,
                    unit=quotation_item.unit,
                    rate=quotation_item.rate,
                    discount=quotation_item.discount,
                )

            invoice.calculate_totals()
            quotation.converted_invoice = invoice
            quotation.status = 'Converted'
            quotation.save(update_fields=['converted_invoice', 'status', 'updated_at'])

        return Response({
            'quotation_id': quotation.id,
            'quotation_number': quotation.quotation_number,
            'invoice_id': invoice.id,
            'invoice_number': invoice.invoice_number,
            'message': 'Quotation converted to invoice.',
        })
