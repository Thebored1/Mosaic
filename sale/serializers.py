"""
Sale App Serializers - API Request/Response Handling
=======================================================

This module provides serializers for all sale app models.

Each serializer includes documentation explaining:
- Purpose and what data it handles
- How it interacts with the model
- Key fields and their usage
- Usage in API endpoints
"""

from rest_framework import serializers
from django.utils import timezone
from .models import (
    State, BusinessLocation, Party,
    Order, OrderItem,
    Invoice, InvoiceItem,
    CreditNote, Receipt,
    PurchaseOrder, PurchaseOrderItem,
    GoodReceiptNote, GRNItem,
    PurchaseInvoice, PurchaseInvoiceItem,
    DebitNote, PaymentOut
)


# ===================== MASTER DATA SERIALIZERS =====================

class StateSerializer(serializers.ModelSerializer):
    """
    State Master Serializer
    =======================

    Purpose: Serialize Indian states for GST state selection

    Endpoint: GET /sale/states/ - List all states (for dropdown)

    Key Fields:
    - state_code: 2-digit GST code (27 = Maharashtra, 29 = Karnataka)
    - Used in Invoice.billing_state, Party.state, BusinessLocation.state

    Usage:
    - States needed for IGST vs CGST+SGST determination
    - Auto-populated in forms based on party/ business location
    """
    class Meta:
        model = State
        fields = ['id', 'name', 'state_code', 'is_active']


class BusinessLocationSerializer(serializers.ModelSerializer):
    """
    Business Location Serializer - Multi-GSTIN Support
    ====================================================

    Purpose: Serialize business locations with separate GSTINs

    Endpoint: GET/POST /sale/business-locations/ - List/Create locations

    Key Fields:
    - gstin: 15-char GSTIN (used in invoice numbering)
    - is_default: Default location for new transactions

    Invoice Numbering Interaction:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  When creating invoice:                                             │
    │  1. User selects business_location from dropdown                  │
    │  2. Invoice.invoice_number uses location.gstin for prefix        │
    │  3. Each location maintains separate sequence counter            │
    │  4. Example: 27AAAAA0000A1Z5/2025-26/00001                        │
    └─────────────────────────────────────────────────────────────────────┘

    Usage:
    - All sales/purchase transactions require location selection
    - Ensures proper invoice numbering per GST registration
    - Filter transactions by location: /sale/invoices/?business_location=1
    """
    state = StateSerializer(read_only=True)

    class Meta:
        model = BusinessLocation
        fields = [
            'id', 'gstin', 'legal_name', 'trade_name', 'state',
            'address', 'phone', 'email', 'invoice_sequence',
            'purchase_invoice_sequence', 'is_default', 'is_active'
        ]
        read_only_fields = ['invoice_sequence', 'purchase_invoice_sequence']


class PartySerializer(serializers.ModelSerializer):
    """
    Party Serializer - Customer & Supplier Master
    ===============================================

    Purpose: Serialize party (customer/supplier) data

    Endpoint:
    - GET /sale/parties/ - List all parties (filter by party_type)
    - POST /sale/parties/ - Create new party

    Key Fields:
    - party_type: Customer / Supplier / Both
    - gstin: Required for B2B transactions
    - state: For GST calculation (IGST vs CGST+SGST)
    - credit_limit: Max credit allowed to customer

    Interaction with Other Models:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  Party is used in:                                                 │
    │  - Invoice.party (sales to customer)                              │
    │  - PurchaseInvoice.supplier (purchase from supplier)             │
    │  - Receipt.party (payment from customer)                          │
    │  - PaymentOut.supplier (payment to supplier)                      │
    │  - Order.party (optional for POS billing)                         │
    └─────────────────────────────────────────────────────────────────────┘

    Credit Management:
    - outstanding property calculates: invoices - receipts - credit notes
    - View party ledger: GET /sale/parties/{id}/ledger/
    """
    state = StateSerializer(read_only=True)
    state_id = serializers.PrimaryKeyRelatedField(
        queryset=State.objects.all(),
        source='state',
        write_only=True,
        required=False
    )
    outstanding = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Party
        fields = [
            'id', 'name', 'party_type', 'gstin', 'state', 'state_id',
            'address', 'shipping_address', 'phone', 'email',
            'credit_limit', 'opening_balance', 'opening_balance_date',
            'is_active', 'notes', 'outstanding', 'created_at', 'updated_at'
        ]

    def create(self, validated_data):
        if validated_data.get('gstin'):
            validated_data['gstin'] = validated_data['gstin'].upper()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if validated_data.get('gstin'):
            validated_data['gstin'] = validated_data['gstin'].upper()
        return super().update(instance, validated_data)


# ===================== ORDER SERIALIZERS =====================

class OrderItemSerializer(serializers.ModelSerializer):
    """
    OrderItem Serializer - Line Items in POS Order
    ==============================================

    Purpose: Serialize order line items for cart/hold functionality

    Endpoint:
    - POST /sale/orders/ - Create order with items
    - PUT /sale/order-items/{id}/ - Update item quantity/rate

    Key Fields:
    - item: From stock app (Item or ItemVariant)
    - quantity: Ordered quantity
    - rate: Selling price (pulled from item or overridden)
    - total: Auto-calculated (quantity × rate - discount)

    Interaction:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  OrderItem Flow:                                                   │
    │  1. User adds item to cart via search/barcode                      │
    │  2. Item details auto-populated from stock.Item                   │
    │  3. On order hold: saved in OrderItem                              │
    │  4. On order recall: OrderItem loaded to continue                 │
    │  5. On convert to invoice: OrderItem → InvoiceItem                │
    └─────────────────────────────────────────────────────────────────────┘
    """
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_sku = serializers.CharField(source='item.sku', read_only=True)
    variant_name = serializers.CharField(source='item_variant.sku', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)

    class Meta:
        model = OrderItem
        fields = [
            'id', 'item', 'item_name', 'item_sku',
            'item_variant', 'variant_name',
            'hsn_code', 'quantity', 'unit', 'unit_name',
            'rate', 'discount', 'total'
        ]


class OrderSerializer(serializers.ModelSerializer):
    """
    Order Serializer - POS Cart / Hold Management
    ==============================================

    Purpose: Serialize POS orders with hold/recall support

    Endpoints:
    - POST /sale/orders/ - Create new order (cart)
    - GET /sale/orders/ - List orders (filter by status)
    - GET /sale/orders/{id}/ - Retrieve order detail
    - POST /sale/orders/{id}/hold/ - Put order on hold
    - POST /sale/orders/{id}/recall/ - Recall held order
    - POST /sale/orders/{id}/convert/ - Convert to invoice

    Key Workflows:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  HOLD/RECALL FLOW:                                                  │
    │                                                                     │
    │  1. Customer comes to billing counter                              │
    │  2. Add items to order (creates Order with OrderItems)            │
    │  3. Customer steps away/hesitates → POST /sale/orders/{id}/hold/  │
    │  4. Order status = 'Hold', order freed for next customer          │
    │  5. Later: GET /sale/orders/?status=Hold                           │
    │  6. Recall: POST /sale/orders/{id}/recall/                         │
    │  7. Continue billing → convert to invoice                         │
    └─────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────────────────────────────────┐
    │  CONVERT TO INVOICE FLOW:                                           │
    │                                                                     │
    │  1. Order ready with all items                                     │
    │  2. POST /sale/orders/{id}/convert/                                │
    │  3. Backend:                                                        │
    │     - Creates Invoice from Order                                   │
    │     - Copies OrderItems to InvoiceItems                            │
    │     - Calculates GST based on party state vs location state        │
    │     - Updates Order.status = 'Invoiced'                           │
    │     - Returns Invoice ID for further action (finalize/print)       │
    └─────────────────────────────────────────────────────────────────────┘

    Filtering:
    - Active carts: /sale/orders/?status=Billing
    - Held orders: /sale/orders/?status=Hold
    - By party: /sale/orders/?party=1
    - By location: /sale/orders/?business_location=1
    """
    order_items = OrderItemSerializer(many=True, read_only=True)
    party_name = serializers.CharField(source='party.name', read_only=True)
    business_location_name = serializers.CharField(source='business_location.legal_name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'party', 'party_name',
            'business_location', 'business_location_name',
            'order_items', 'sub_total', 'discount_amount', 'discount_type',
            'grand_total', 'status', 'hold_notes',
            'created_at', 'created_by', 'created_by_name'
        ]
        read_only_fields = ['order_number', 'sub_total', 'grand_total', 'created_at']


class OrderCreateSerializer(serializers.ModelSerializer):
    """
    Order Create Serializer - For Creating POS Orders
    ==================================================

    Usage: POST /sale/orders/ - Create new order

    Expected Payload:
    {
        "party": 1, (optional)
        "business_location": 1,
        "items": [
            {"item": 1, "quantity": 2, "rate": 100},
            {"item_variant": 2, "quantity": 1, "rate": 50}
        ]
    }
    """
    items = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        help_text="List of items: [{item: 1, quantity: 2, rate: 100}, ...]"
    )

    class Meta:
        model = Order
        fields = ['party', 'business_location', 'items', 'discount_amount', 'discount_type', 'hold_notes']

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        request = self.context.get('request')

        # Create order
        order = Order.objects.create(
            business_location=validated_data.get('business_location'),
            party=validated_data.get('party'),
            discount_amount=validated_data.get('discount_amount', 0),
            discount_type=validated_data.get('discount_type', 'Fixed'),
            created_by=request.user if request else None
        )

        # Create order items
        from stock.models import Item, ItemVariant, Unit
        for item_data in items_data:
            item_id = item_data.get('item')
            variant_id = item_data.get('item_variant')

            if variant_id:
                variant = ItemVariant.objects.get(pk=variant_id)
                item = variant.item
                unit = item.unit
                rate = item_data.get('rate', variant.unit_price)
            else:
                item = Item.objects.get(pk=item_id)
                unit = item.unit
                rate = item_data.get('rate', item.unit_price)

            order_item = OrderItem.objects.create(
                order=order,
                item=item,
                item_variant=variant_id,
                hsn_code=item.tax_code.code if item.tax_code else '',
                quantity=item_data.get('quantity', 1),
                unit=unit,
                rate=rate,
                discount=item_data.get('discount', 0)
            )

        order.calculate_totals()
        return order


# ===================== INVOICE SERIALIZERS =====================

class InvoiceItemSerializer(serializers.ModelSerializer):
    """
    InvoiceItem Serializer - Sales Invoice Line Items
    ==================================================

    Purpose: Serialize invoice line items with tax breakdown

    Endpoint: Included in Invoice detail response

    Key Fields:
    - hsn_code: From stock.TaxCode
    - taxable_amount: quantity × rate - discount
    - cgst/sgst/igst: Auto-calculated based on state comparison
    - total: taxable + all tax amounts

    GST Calculation (explained in model):
    - Intra-state: billing_state == business_location.state → CGST + SGST
    - Inter-state: billing_state != business_location.state → IGST
    """
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_sku = serializers.CharField(source='item.sku', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)

    class Meta:
        model = InvoiceItem
        fields = [
            'id', 'item', 'item_name', 'item_sku',
            'item_variant', 'batch', 'hsn_code',
            'quantity', 'unit', 'unit_name',
            'rate', 'discount', 'taxable_amount',
            'cgst_rate', 'cgst_amount',
            'sgst_rate', 'sgst_amount',
            'igst_rate', 'igst_amount',
            'total'
        ]


class InvoiceListSerializer(serializers.ModelSerializer):
    """
    Invoice List Serializer - For Listing Invoices
    ================================================

    Purpose: Compact serializer for invoice list view

    Endpoint: GET /sale/invoices/ - List all invoices (paginated)

    Filtering:
    - By party: /sale/invoices/?party=1
    - By status: /sale/invoices/?is_finalized=true
    - By date range: /sale/invoices/?invoice_date_after=2025-04-01
    - By location: /sale/invoices/?business_location=1
    """
    party_name = serializers.CharField(source='party.name', read_only=True)
    business_location_name = serializers.CharField(source='business_location.legal_name', read_only=True)
    billing_state_name = serializers.CharField(source='billing_state.name', read_only=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'invoice_type', 'invoice_date',
            'party', 'party_name', 'billing_state_name',
            'business_location', 'business_location_name',
            'sub_total', 'cgst_amount', 'sgst_amount', 'igst_amount',
            'grand_total', 'is_finalized', 'is_cancelled'
        ]


class InvoiceDetailSerializer(serializers.ModelSerializer):
    """
    Invoice Detail Serializer - Complete Invoice with Items
    =========================================================

    Purpose: Full invoice detail with all items and tax breakdown

    Endpoint:
    - GET /sale/invoices/{id}/ - Full invoice detail
    - Response includes items array with complete tax breakdown
    - tax_summary JSON for GST reports

    Tax Summary Example:
    {
        "5": {"cgst": 100, "sgst": 100, "igst": 0},
        "18": {"cgst": 900, "sgst": 900, "igst": 0}
    }

    Actions Available:
    - POST /sale/invoices/{id}/finalize/ - Finalize & deduct stock
    - POST /sale/invoices/{id}/cancel/ - Cancel invoice
    - GET /sale/invoices/{id}/print/ - Generate printable format
    """
    items = InvoiceItemSerializer(many=True, read_only=True)
    party = PartySerializer(read_only=True)
    business_location = BusinessLocationSerializer(read_only=True)
    billing_state = StateSerializer(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'invoice_type', 'invoice_date', 'due_date',
            'party', 'billing_state', 'business_location',
            'items', 'sub_total', 'discount_amount', 'discount_type',
            'taxable_amount', 'cgst_amount', 'sgst_amount', 'igst_amount',
            'round_off', 'grand_total', 'tax_summary',
            'notes', 'terms', 'is_finalized', 'is_cancelled',
            'e_way_bill', 'order', 'created_at', 'created_by', 'created_by_name'
        ]


class InvoiceCreateSerializer(serializers.ModelSerializer):
    """
    Invoice Create Serializer - For Creating Invoices
    ==================================================

    Usage: POST /sale/invoices/ - Create new invoice

    Expected Payload:
    {
        "invoice_type": "Tax Invoice",
        "party": 1,
        "business_location": 1,
        "billing_state": 1,
        "due_date": "2025-05-30",
        "items": [
            {"item": 1, "quantity": 2, "rate": 100, "discount": 0},
            {"item_variant": 2, "quantity": 1, "rate": 50, "discount": 5}
        ],
        "discount_amount": 10,
        "discount_type": "Fixed",
        "notes": "Thank you for your business"
    }

    Workflow:
    1. User fills form (party selected, items added)
    2. Items array contains item_id + quantity + rate + discount
    3. On save, InvoiceItem records created with auto-calculated GST
    4. Invoice remains in Draft (is_finalized=False) until explicitly finalized

    Key: Items are created as Draft, not affecting stock until finalize()
    """
    items = serializers.ListField(
        child=serializers.DictField(),
        write_only=True,
        required=False
    )

    class Meta:
        model = Invoice
        fields = [
            'invoice_type', 'party', 'business_location', 'billing_state',
            'due_date', 'items', 'discount_amount', 'discount_type',
            'notes', 'terms'
        ]

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        request = self.context.get('request')

        invoice = Invoice.objects.create(
            business_location=validated_data.get('business_location'),
            party=validated_data.get('party'),
            billing_state=validated_data.get('billing_state'),
            invoice_type=validated_data.get('invoice_type', 'Tax Invoice'),
            due_date=validated_data.get('due_date'),
            discount_amount=validated_data.get('discount_amount', 0),
            discount_type=validated_data.get('discount_type', 'Fixed'),
            notes=validated_data.get('notes', ''),
            terms=validated_data.get('terms', ''),
            created_by=request.user if request else None
        )

        # Create invoice items
        from stock.models import Item, ItemVariant, Unit
        for item_data in items_data:
            item_id = item_data.get('item')
            variant_id = item_data.get('item_variant')

            if variant_id:
                variant = ItemVariant.objects.get(pk=variant_id)
                item = variant.item
                unit = item.unit
            else:
                item = Item.objects.get(pk=item_id)
                unit = item.unit

            quantity = item_data.get('quantity', 1)
            rate = item_data.get('rate', item.unit_price)
            discount = item_data.get('discount', 0)
            taxable = (quantity * rate) - discount

            inv_item = InvoiceItem.objects.create(
                invoice=invoice,
                item=item,
                item_variant=variant_id,
                hsn_code=item.tax_code.code if item.tax_code else '',
                quantity=quantity,
                unit=unit,
                rate=rate,
                discount=discount,
                taxable_amount=taxable
            )

        invoice.save()
        return invoice


# ===================== PAYMENT SERIALIZERS =====================

class ReceiptSerializer(serializers.ModelSerializer):
    """
    Receipt Serializer - Payment Received from Customer
    ====================================================

    Purpose: Serialize payment records

    Endpoints:
    - POST /sale/receipts/ - Record payment
    - GET /sale/receipts/ - List receipts

    Key Fields:
    - invoice: Link to specific invoice
    - amount: Payment amount (can be partial)
    - payment_mode: Cash/Card/UPI/Bank Transfer/Credit

    Partial Payment:
    - Multiple receipts allowed per invoice
    - Track: sum(receipts) vs invoice.grand_total

    Workflow:
    1. Customer makes payment at counter
    2. Select invoice (or create new receipt without invoice for advance)
    3. Record payment_mode and amount
    4. Party.outstanding automatically updates
    """
    invoice = InvoiceListSerializer(read_only=True)
    party = PartySerializer(read_only=True)
    received_by_name = serializers.CharField(source='received_by.username', read_only=True)

    class Meta:
        model = Receipt
        fields = [
            'id', 'receipt_number', 'invoice', 'party',
            'business_location', 'amount', 'payment_mode',
            'reference_number', 'transaction_date', 'notes',
            'created_at', 'received_by', 'received_by_name'
        ]


class ReceiptCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Receipt
        fields = ['invoice', 'party', 'business_location', 'amount', 'payment_mode', 'reference_number', 'notes']


class CreditNoteSerializer(serializers.ModelSerializer):
    """
    CreditNote Serializer - Sales Returns
    =====================================

    Purpose: Serialize sales return documents

    Endpoints:
    - POST /sale/credit-notes/ - Create return
    - GET /sale/credit-notes/ - List returns

    Key Fields:
    - invoice: Original invoice being returned
    - is_stock_returned: Check to restore inventory
    - amount: Auto-calculated from returned items
    """
    invoice = InvoiceListSerializer(read_only=True)
    party = PartySerializer(read_only=True)

    class Meta:
        model = CreditNote
        fields = [
            'id', 'credit_note_number', 'invoice', 'party',
            'amount', 'reason', 'is_stock_returned',
            'notes', 'created_at', 'created_by'
        ]


# ===================== PURCHASE SERIALIZERS =====================

class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_sku = serializers.CharField(source='item.sku', read_only=True)

    class Meta:
        model = PurchaseOrderItem
        fields = ['id', 'item', 'item_name', 'item_sku', 'item_variant', 'quantity_order', 'quantity_received', 'unit', 'rate', 'discount', 'total']


class PurchaseOrderListSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    business_location_name = serializers.CharField(source='business_location.legal_name', read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = ['id', 'po_number', 'supplier', 'supplier_name', 'business_location', 'business_location_name', 'order_date', 'expected_date', 'grand_total', 'status']


class PurchaseOrderDetailSerializer(serializers.ModelSerializer):
    order_items = PurchaseOrderItemSerializer(many=True, read_only=True)
    supplier = PartySerializer(read_only=True)
    business_location = BusinessLocationSerializer(read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = ['id', 'po_number', 'supplier', 'business_location', 'order_date', 'expected_date', 'order_items', 'sub_total', 'discount_amount', 'grand_total', 'status', 'terms', 'notes', 'created_at']


class GRNItemSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='item.name', read_only=True)

    class Meta:
        model = GRNItem
        fields = ['id', 'item', 'item_name', 'item_variant', 'quantity', 'unit', 'rate', 'total']


class GRNListSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)

    class Meta:
        model = GoodReceiptNote
        fields = ['id', 'grn_number', 'supplier', 'supplier_name', 'received_date', 'supplier_invoice_number']


class GRNDetailSerializer(serializers.ModelSerializer):
    grn_items = GRNItemSerializer(many=True, read_only=True)
    supplier = PartySerializer(read_only=True)
    purchase_order = PurchaseOrderListSerializer(read_only=True)

    class Meta:
        model = GoodReceiptNote
        fields = ['id', 'grn_number', 'purchase_order', 'supplier', 'business_location', 'received_date', 'supplier_invoice_number', 'supplier_invoice_date', 'grn_items', 'notes', 'created_at']


class PurchaseInvoiceItemSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='item.name', read_only=True)

    class Meta:
        model = PurchaseInvoiceItem
        fields = ['id', 'item', 'item_name', 'item_variant', 'hsn_code', 'quantity', 'unit', 'rate', 'discount', 'taxable_amount', 'cgst_rate', 'cgst_amount', 'sgst_rate', 'sgst_amount', 'igst_rate', 'igst_amount', 'total']


class PurchaseInvoiceListSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = ['id', 'invoice_number', 'supplier', 'supplier_name', 'supplier_invoice_number', 'invoice_date', 'grand_total', 'is_finalized']


class PurchaseInvoiceDetailSerializer(serializers.ModelSerializer):
    items = PurchaseInvoiceItemSerializer(many=True, read_only=True)
    supplier = PartySerializer(read_only=True)
    business_location = BusinessLocationSerializer(read_only=True)
    grn = GRNListSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = ['id', 'invoice_number', 'supplier', 'business_location', 'grn', 'purchase_order', 'supplier_invoice_number', 'supplier_invoice_date', 'invoice_date', 'due_date', 'items', 'sub_total', 'discount_amount', 'taxable_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'round_off', 'grand_total', 'is_finalized', 'is_cancelled', 'notes', 'created_at']


class DebitNoteSerializer(serializers.ModelSerializer):
    purchase_invoice = PurchaseInvoiceListSerializer(read_only=True)
    supplier = PartySerializer(read_only=True)

    class Meta:
        model = DebitNote
        fields = ['id', 'debit_note_number', 'purchase_invoice', 'supplier', 'amount', 'reason', 'is_stock_returned', 'notes', 'created_at']


class PaymentOutSerializer(serializers.ModelSerializer):
    purchase_invoice = PurchaseInvoiceListSerializer(read_only=True)
    supplier = PartySerializer(read_only=True)

    class Meta:
        model = PaymentOut
        fields = ['id', 'payment_number', 'purchase_invoice', 'supplier', 'business_location', 'amount', 'payment_mode', 'reference_number', 'transaction_date', 'notes', 'created_at']