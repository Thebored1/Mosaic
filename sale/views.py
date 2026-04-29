"""
Sale App Views - REST API Endpoints
===================================

This module provides API endpoints for all sale app operations.

Each ViewSet includes documentation explaining:
- Purpose and functionality
- URL endpoints
- Request/Response format
- Interaction with other models/views
- Key workflows
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db.models import Sum, Count, Q
from django.db import transaction
from django.utils import timezone
from decimal import Decimal

from configuration.models import State, Warehouse as BusinessLocation
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
    """Standard pagination for all list endpoints."""
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100


def org_filter(qs, request):
    """Filter queryset by organization from auth token."""
    if not hasattr(request, 'auth') or request.auth is None:
        return qs.none()
    # Check if model has organization field
    if hasattr(qs.model, '_meta') and any(f.name == 'organization' for f in qs.model._meta.get_fields()):
        return qs.filter(organization=request.auth)
    return qs


# ===================== MASTER DATA VIEWSETS =====================

class StateViewSet(viewsets.ReadOnlyModelViewSet):
    """
    State Master API
    =================

    Purpose: List all Indian states for GST place of supply selection

    Endpoints:
    - GET /sale/states/ - List all states
    - GET /sale/states/{id}/ - Get state detail

    Usage:
    - Populated in invoice form (billing_state dropdown)
    - Used by Party model (state field)
    - Used by BusinessLocation model (state field)

    GST Logic:
    - Compare state_code from party vs business location
    - Determine IGST (inter-state) vs CGST+SGST (intra-state)
    """
    queryset = State.objects.filter(is_active=True)
    serializer_class = StateSerializer
    pagination_class = StandardPagination
    filter_backends = [SearchFilter]
    search_fields = ['name', 'state_code']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)


class BusinessLocationViewSet(viewsets.ModelViewSet):
    """
    Business Location API - Multi-GSTIN Management
    ===============================================

    Purpose: Manage multiple GST registrations (branches/entities)

    Endpoints:
    - GET /sale/business-locations/ - List all locations
    - POST /sale/business-locations/ - Create new location
    - GET /sale/business-locations/{id}/ - Get location detail
    - PUT /sale/business-locations/{id}/ - Update location
    - DELETE /sale/business-locations/{id}/ - Delete location

    Key Workflow:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  Creating a location:                                        │
    │  1. POST with GSTIN, legal_name, state, address            │
    │  2. System validates GSTIN format                                   │
    │  3. Location created with sequence counter = 0                      │
    │  4. Use as default for: /sale/invoices/?business_location=1       │
    │                                                                     │
    │  Invoice Numbering per Location:                                   │
    │  - Each location has separate invoice_sequence                     │
    │  - Invoice number format: {GSTIN}/{FY}/{NNNNN}                     │
    │  - Example: 27AAAAA0000A1Z5/2025-26/00001                          │
    └─────────────────────────────────────────────────────────────────────┘

    Filtering:
    - Default location: /sale/business-locations/?is_default=true
    - By state: /sale/business-locations/?state=1
    """
    queryset = BusinessLocation.objects.all()
    serializer_class = BusinessLocationSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_default', 'state', 'is_active']
    search_fields = ['legal_name', 'trade_name', 'gstin']
    ordering_fields = ['legal_name', 'gstin']
    ordering = ['legal_name']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)


class PartyViewSet(viewsets.ModelViewSet):
    """
    Party API - Customer & Supplier Master
    =======================================

    Purpose: Manage customers and suppliers with GST details

    Endpoints:
    - GET /sale/parties/ - List all parties
    - POST /sale/parties/ - Create new party
    - GET /sale/parties/{id}/ - Get party detail
    - PUT /sale/parties/{id}/ - Update party
    - DELETE /sale/parties/{id}/ - Deactivate party

    Additional Actions:
    - GET /sale/parties/{id}/ledger/ - Party ledger statement
    - GET /sale/parties/{id}/outstanding/ - Current outstanding

    Filtering:
    - By type: /sale/parties/?party_type=Customer
    - By state: /sale/parties/?state=1
    - Active only: /sale/parties/?is_active=true

    Credit Management Flow:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  Credit Check on Invoice Finalize:                                 │
    │  1. Invoice finalized for party                                    │
    │  2. Check: party.outstanding + invoice.grand_total                 │
    │  3. If > party.credit_limit: Warning/Block                        │
    │  4. Allow if credit_limit = 0 (no limit)                          │
    └─────────────────────────────────────────────────────────────────────┘
    """
    queryset = Party.objects.all()
    serializer_class = PartySerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['party_type', 'state', 'is_active']
    search_fields = ['name', 'gstin', 'phone', 'email']
    ordering_fields = ['name', 'party_type', 'created_at']
    ordering = ['name']

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
    """
    Order API - POS Cart & Hold Management
    =======================================

    Purpose: Handle POS orders with hold/recall functionality

    Endpoints:
    - GET /sale/orders/ - List all orders
    - POST /sale/orders/ - Create new order (cart)
    - GET /sale/orders/{id}/ - Get order detail
    - PUT /sale/orders/{id}/ - Update order
    - DELETE /sale/orders/{id}/ - Cancel/delete order

    Custom Actions:
    - POST /sale/orders/{id}/hold/ - Put order on hold
    - POST /sale/orders/{id}/recall/ - Recall held order
    - POST /sale/orders/{id}/convert/ - Convert to invoice

    Workflow:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  POS Billing Flow:                                                  │
    │                                                                     │
    │  1. Create Order: POST /sale/orders/                               │
    │     {business_location: 1, party: 1, items: [...]}               │
    │                                                                     │
    │  2. Add Items: Items added via OrderCreateSerializer             │
    │     - Auto-populated from stock.Item                              │
    │     - Rate pulled from item.unit_price                            │
    │     - Order.calculate_totals() called after each item             │
    │                                                                     │
    │  3. Hold: POST /sale/orders/{id}/hold/                            │
    │     - status = 'Hold'                                             │
    │     - hold_notes = "Customer name/phone"                          │
    │     - Order freed for next customer                               │
    │                                                                     │
    │  4. Recall: GET /sale/orders/?status=Hold                        │
    │     - POST /sale/orders/{id}/recall/                             │
    │     - status = 'Billing'                                         │
    │     - Continue billing                                             │
    │                                                                     │
    │  5. Convert: POST /sale/orders/{id}/convert/                     │
    │     - Creates Invoice from Order                                  │
    │     - Copies OrderItems to InvoiceItems                           │
    │     - Calculates GST based on state                               │
    │     - Updates Order.status = 'Invoiced'                          │
    │     - Returns Invoice ID                                          │
    └─────────────────────────────────────────────────────────────────────┘

    Filtering:
    - Active carts: /sale/orders/?status=Billing
    - Held orders: /sale/orders/?status=Hold
    - By location: /sale/orders/?business_location=1
    - By party: /sale/orders/?party=1
    """
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'party', 'business_location']
    search_fields = ['order_number', 'hold_notes']
    ordering_fields = ['created_at', 'order_number']
    ordering = ['-created_at']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_queryset(self):
        return org_filter(self.queryset, self.request)

    def get_serializer_class(self):
        if self.action == 'create':
            return OrderCreateSerializer
        return OrderSerializer

    def create(self, request, *args, **kwargs):
        """Create new POS order with items."""
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        order = serializer.save()

        return Response(
            OrderSerializer(order).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['post'])
    def hold(self, request, pk=None):
        """
        Hold Order
        ==========

        Purpose: Put current billing on hold

        Request: POST /sale/orders/{id}/hold/

        Payload: {"hold_notes": "Customer: John, Phone: 9876543210"}

        Response: Order with status = 'Hold'

        Usage: Customer steps away, next customer can be processed
        """
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
    """
    Receipt API - Payment Recording
    ===============================

    Purpose: Record payments received from customers

    Endpoints:
    - GET /sale/receipts/ - List all receipts
    - POST /sale/receipts/ - Record new payment
    - GET /sale/receipts/{id}/ - Get receipt detail
    - DELETE /sale/receipts/{id}/ - Delete receipt

    Filtering:
    - By invoice: /sale/receipts/?invoice=1
    - By party: /sale/receipts/?party=1
    - By date: /sale/receipts/?transaction_date_after=2025-04-01

    Payment Modes: Cash, Card, UPI, Bank Transfer, Credit
    """
    queryset = Receipt.objects.all()
    serializer_class = ReceiptSerializer
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
        serializer.save(received_by=self.request.user)


class CreditNoteViewSet(viewsets.ModelViewSet):
    """
    CreditNote API - Sales Returns
    ===============================

    Purpose: Manage sales returns / debit notes to customers

    Endpoints:
    - GET /sale/credit-notes/ - List all credit notes
    - POST /sale/credit-notes/ - Create return
    - GET /sale/credit-notes/{id}/ - Get detail
    """
    queryset = CreditNote.objects.all()
    serializer_class = CreditNoteSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['invoice', 'is_stock_returned']
    search_fields = ['credit_note_number']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    """
    Purchase Order API
    ===================

    Purpose: Create and manage purchase orders to suppliers

    Endpoints:
    - GET /sale/purchase-orders/ - List all POs
    - POST /sale/purchase-orders/ - Create PO
    - GET /sale/purchase-orders/{id}/ - Get PO detail
    - PUT /sale/purchase-orders/{id}/ - Update PO
    - DELETE /sale/purchase-orders/{id}/ - Delete PO

    Actions:
    - POST /sale/purchase-orders/{id}/send/ - Mark as sent to supplier

    Workflow:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  PO → GRN → Purchase Invoice Flow:                                │
    │                                                                     │
    │  1. Create PO: POST /sale/purchase-orders/                         │
    │     {supplier, business_location, items: [...]}                   │
    │     Status = 'Draft'                                               │
    │                                                                     │
    │  2. Send to Supplier: POST /sale/purchase-orders/{id}/send/       │
    │     Status = 'Sent'                                                │
    │                                                                     │
    │  3. Goods Received: Create GRN (optional link to PO)              │
    │     - PO.status = 'Partial' if partial, 'Received' if full        │
    │                                                                     │
    │  4. Bill Received: Create PurchaseInvoice linked to GRN          │
    │     - PO.status = 'Received' (all goods invoiced)                 │
    └─────────────────────────────────────────────────────────────────────┘
    """
    queryset = PurchaseOrder.objects.all()
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

    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Mark PO as sent to supplier."""
        po = self.get_object()
        po.status = 'Sent'
        po.save()
        return Response({'message': 'PO marked as sent', 'status': po.status})


class GoodReceiptNoteViewSet(viewsets.ModelViewSet):
    """
    GRN API - Good Receipt Note
    ============================

    Purpose: Record goods received from supplier

    Endpoints:
    - GET /sale/grns/ - List all GRNs
    - POST /sale/grns/ - Create GRN
    - GET /sale/grns/{id}/ - Get GRN detail

    Actions:
    - POST /sale/grns/{id}/create-invoice/ - Convert to purchase invoice

    PO Linking:
    - If linked to PO, auto-populate items
    - Updates PO items quantity_received
    """
    queryset = GoodReceiptNote.objects.all()
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
    """
    Purchase Invoice API
    ====================

    Purpose: Manage supplier bills / purchase invoices

    Endpoints:
    - GET /sale/purchase-invoices/ - List all
    - POST /sale/purchase-invoices/ - Create
    - GET /sale/purchase-invoices/{id}/ - Get detail
    - POST /sale/purchase-invoices/{id}/finalize/ - Finalize & add stock
    - POST /sale/purchase-invoices/{id}/cancel/ - Cancel
    """
    queryset = PurchaseInvoice.objects.all()
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
    """DebitNote API - Purchase Returns"""
    queryset = DebitNote.objects.all()
    serializer_class = DebitNoteSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['purchase_invoice', 'supplier']
    ordering = ['-created_at']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)


class PaymentOutViewSet(viewsets.ModelViewSet):
    """PaymentOut API - Payments to Suppliers"""
    queryset = PaymentOut.objects.all()
    serializer_class = PaymentOutSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ['purchase_invoice', 'supplier', 'business_location']
    search_fields = ['payment_number', 'reference_number']
    ordering = ['-transaction_date']

    def get_queryset(self):
        return org_filter(self.queryset, self.request)


# ===================== REPORTS =====================

class ReportsViewSet(viewsets.ViewSet):
    """
    Reports API
    ===========

    Purpose: Generate business reports

    Endpoints:
    - GET /sale/reports/daily-sales/ - Daily sales summary
    - GET /sale/reports/gst-register/ - GST sales register
    - GET /sale/reports/gstr1/ - GSTR-1 format data
    - GET /sale/reports/purchase-register/ - Purchase register
    """

    @action(detail=False, methods=['get'])
    def daily_sales(self, request):
        """Daily sales summary."""
        date = request.query_params.get('date', timezone.now().date())

        invoices = Invoice.objects.filter(
            invoice_date__date=date,
            is_finalized=True,
            is_cancelled=False
        ).aggregate(
            total_sales=Sum('grand_total'),
            total_tax=Sum('cgst_amount') + Sum('sgst_amount') + Sum('igst_amount'),
            total_items=Count('id')
        )

        receipts = Receipt.objects.filter(
            transaction_date__date=date
        ).aggregate(
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

        invoices = Invoice.objects.filter(
            is_finalized=True,
            is_cancelled=False
        )

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

        invoices = Invoice.objects.filter(
            is_finalized=True,
            is_cancelled=False,
            invoice_type__in=['Tax Invoice', 'Export', 'SEZ']
        )

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
    """ViewSet for Quotation CRUD."""
    # TODO: Implement Quotation/QuotationItem models in sale/models.py
    # from .models import Quotation, QuotationItem
    # from .serializers import QuotationSerializer, QuotationDetailSerializer
    pass


class PriceListViewSet(viewsets.ModelViewSet):
    """ViewSet for PriceList CRUD."""
    # TODO: Implement PriceList/PriceListItem models in sale/models.py
    # from .models import PriceList, PriceListItem
    # from .serializers import PriceListSerializer, PriceListDetailSerializer
    pass