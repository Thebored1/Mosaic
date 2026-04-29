"""
Sale application views.

This module exposes the back-office sales, purchase, and reporting APIs used
by org users and super admins. The views here are the accounting backbone of
the system, so they enforce tenant boundaries and document transitions
carefully.
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db.models import Sum, Count, Q
from django.db import transaction
from django.utils import timezone
from decimal import Decimal

from configuration.models import State, Warehouse as BusinessLocation
from configuration.authentication import SUPER_ADMIN_MARKER, ECOMMERCE_MARKER, ScopedRolePermission
from .models import (
    Party,
    Order, OrderItem,
    Invoice, InvoiceItem,
    CreditNote, Receipt,
    PurchaseOrder, PurchaseOrderItem,
    GoodReceiptNote, GRNItem,
    PurchaseInvoice, PurchaseInvoiceItem,
    DebitNote, PaymentOut
)
from .serializers import (
    StateSerializer, BusinessLocationSerializer, PartySerializer,
    OrderSerializer, OrderCreateSerializer,
    InvoiceListSerializer, InvoiceDetailSerializer, InvoiceCreateSerializer,
    InvoiceItemSerializer,
    CreditNoteSerializer, ReceiptSerializer, ReceiptCreateSerializer,
    PurchaseOrderListSerializer, PurchaseOrderDetailSerializer,
    GRNListSerializer, GRNDetailSerializer,
    PurchaseInvoiceListSerializer, PurchaseInvoiceDetailSerializer,
    DebitNoteSerializer, PaymentOutSerializer
)


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
    if qs.model is State:
        return qs
    return qs.none()


def save_for_request_organization(serializer, request):
    """Save a model instance under the organization derived from the request."""
    model_fields = {field.name for field in serializer.Meta.model._meta.get_fields()}
    if 'organization' not in model_fields:
        serializer.save()
        return

    org_id = request.data.get('organization') or request.query_params.get('organization')

    if request.auth == SUPER_ADMIN_MARKER:
        if not org_id:
            raise ValidationError({'organization': 'organization is required for super admin writes'})
        serializer.save(organization_id=org_id)
        return
    if request.auth == ECOMMERCE_MARKER:
        raise ValidationError({'organization': 'Create or join an organization to access this feature'})

    serializer.save(organization=request.auth)


def validate_related_organization(request, **relations):
    """Validate that related objects belong to the same organization."""
    if request.auth == SUPER_ADMIN_MARKER:
        return
    if request.auth == ECOMMERCE_MARKER:
        raise ValidationError({'organization': 'Create or join an organization to access this feature'})

    for field_name, related_obj in relations.items():
        if related_obj is None:
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

    @action(detail=True, methods=['get'])
    def ledger(self, request, pk=None):
        """Get party ledger - all transactions with running balance."""
        party = self.get_object()

        # Sales invoices
        invoices = Invoice.objects.filter(
            party=party, is_finalized=True, is_cancelled=False
        ).values('invoice_number', 'invoice_date', 'grand_total').annotate(
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
                'party': order.party.id if order.party else None,
                'business_location': order.business_location.id,
                'due_date': request.data.get('due_date'),
                'notes': request.data.get('notes', ''),
                'order': order
            }

            # Determine billing state
            if order.party and order.party.state:
                invoice_data['billing_state'] = order.party.state.id

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
    │  1. Validates: is_finalized = False                               │
    │  2. Credit Check: party.outstanding + grand_total <= credit_limit │
    │  3. For each InvoiceItem:                                         │
    │     - Creates StockMovement (type='Sale')                         │
    │     - Reduces item.current_stock or variant.current_stock         │
    │     - If batch linked: reduces batch.quantity_remaining          │
    │  4. Updates: is_finalized = True, invoice_date = now              │
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
    │  1. Validates: is_finalized = True                                │
    │  2. Creates CreditNote automatically (optional)                   │
    │  3. For each InvoiceItem:                                          │
    │     - Creates StockMovement (type='Return')                       │
    │     - Adds stock back to inventory                                 │
    │  4. Updates: is_cancelled = True                                  │
    │  5. Keeps audit trail (invoice number preserved, marked cancel)  │
    └─────────────────────────────────────────────────────────────────────┘

    Filtering:
    - By party: /sale/invoices/?party=1
    - By status: /sale/invoices/?is_finalized=true
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
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

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
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

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
            invoice=serializer.validated_data.get('invoice'),
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

    @action(detail=True, methods=['post'])
    def create_invoice(self, request, pk=None):
        """Create purchase invoice from GRN."""
        grn = self.get_object()

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
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel purchase invoice."""
        pi = self.get_object()

        if pi.status != 'Finalized':
            return Response({'error': 'Only finalized can be cancelled'}, status=400)

        pi.status = 'Cancelled'
        pi.save()
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
    filterset_fields = ['purchase_invoice', 'supplier', 'business_location']
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

class ReportsViewSet(viewsets.ViewSet):
    """Reporting viewset for operational and GST summaries."""

    permission_classes = [ScopedRolePermission]
    permission_scope = 'reporting'

    @action(detail=False, methods=['get'])
    def daily_sales(self, request):
        """Daily sales summary."""
        date = request.query_params.get('date', timezone.now().date())

        invoices = org_filter(Invoice.objects.filter(
            invoice_date__date=date,
            is_finalized=True,
            is_cancelled=False
        ), request).aggregate(
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
            'total_sales': str(invoices['total_sales'] or 0),
            'total_tax': str(invoices['total_tax'] or 0),
            'total_receipts': str(receipts['total_receipts'] or 0)
        })

    @action(detail=False, methods=['get'])
    def gst_register(self, request):
        """GST Sales Register - grouped by tax rate."""
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')

        invoices = org_filter(Invoice.objects.filter(
            is_finalized=True,
            is_cancelled=False
        ), request)

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

        invoices = org_filter(Invoice.objects.filter(
            is_finalized=True,
            is_cancelled=False,
            invoice_type__in=['Tax Invoice', 'Export', 'SEZ']
        ), request)

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


class QuotationViewSet(viewsets.ModelViewSet):
    """Placeholder viewset for quotation CRUD."""
    # TODO: Implement Quotation/QuotationItem models in sale/models.py
    # from .models import Quotation, QuotationItem
    # from .serializers import QuotationSerializer, QuotationDetailSerializer
    pass


class PriceListViewSet(viewsets.ModelViewSet):
    """Placeholder viewset for price list CRUD."""
    # TODO: Implement PriceList/PriceListItem models in sale/models.py
    # from .models import PriceList, PriceListItem
    # from .serializers import PriceListSerializer, PriceListDetailSerializer
    pass
