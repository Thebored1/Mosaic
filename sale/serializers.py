"""
Sale application serializers.

This module shapes the back-office sales and purchase API contract:

1. master data such as states, business locations, and parties
2. POS order/cart payloads and invoice creation responses
3. purchase order, GRN, purchase invoice, debit note, and supplier payment data
4. GST-oriented read models for reports and printable documents

The serializers are deliberately verbose because the sale domain carries a lot
of accounting and tax metadata that must stay stable across workflows.
"""

from rest_framework import serializers
from decimal import Decimal
from django.utils import timezone
from configuration.models import State, Warehouse as BusinessLocation
from configuration.authentication import SUPER_ADMIN_MARKER
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
    PriceList, PriceListItem,
    Quotation, QuotationItem,
)


# ===================== MASTER DATA SERIALIZERS =====================

class StateSerializer(serializers.ModelSerializer):
    """
    Serialize Indian states for GST workflows.

    States are used everywhere the application needs to calculate place of
    supply or populate a billing/location dropdown.
    """
    class Meta:
        model = State
        fields = ['id', 'name', 'state_code', 'is_active']
        ref_name = 'SaleState'


class BusinessLocationSerializer(serializers.ModelSerializer):
    """
    Serialize business locations and GST registration details.

    Business locations are the source of invoice numbering and a core part of
    the sale and purchase workflows.
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
    Serialize the party master used by both customers and suppliers.

    Parties are the counterparties for nearly every sale-side workflow, so the
    serializer carries GST, credit, and contact data in one place.
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


def resolve_price_list_item(price_list, item, item_variant=None):
    """
    Resolve the matching price-list row for a stock item or variant.

    Variant-specific rows win first. If none are found, fall back to the item
    level default row.
    """
    if price_list is None:
        return None
    if item_variant is not None:
        exact = price_list.items.filter(item=item, item_variant=item_variant).first()
        if exact is not None:
            return exact
    return price_list.items.filter(item=item, item_variant__isnull=True).first()


class PriceListItemSerializer(serializers.ModelSerializer):
    """Serialize a priced item inside a price list."""

    item_name = serializers.CharField(source='item.name', read_only=True)
    item_sku = serializers.CharField(source='item.sku', read_only=True)
    variant_sku = serializers.CharField(source='item_variant.sku', read_only=True)

    class Meta:
        model = PriceListItem
        fields = ['id', 'item', 'item_name', 'item_sku', 'item_variant', 'variant_sku', 'rate', 'notes']


class PriceListListSerializer(serializers.ModelSerializer):
    """Serialize price lists for grid views."""

    organization_id = serializers.IntegerField(source='organization.id', read_only=True)

    class Meta:
        model = PriceList
        fields = [
            'id', 'organization_id', 'name', 'description',
            'effective_from', 'effective_to', 'is_active',
            'created_at', 'updated_at',
        ]


class PriceListDetailSerializer(serializers.ModelSerializer):
    """Serialize a full price list with nested price items."""

    items = PriceListItemSerializer(many=True, read_only=True)

    class Meta:
        model = PriceList
        fields = [
            'id', 'organization', 'name', 'description',
            'effective_from', 'effective_to', 'is_active',
            'notes', 'items', 'created_at', 'updated_at',
        ]


class PriceListCreateSerializer(serializers.ModelSerializer):
    """Create or update a price list with nested item overrides."""

    items = serializers.ListField(child=serializers.DictField(), write_only=True, required=False)

    class Meta:
        model = PriceList
        fields = [
            'organization', 'name', 'description', 'effective_from',
            'effective_to', 'is_active', 'notes', 'items',
        ]
        extra_kwargs = {
            'organization': {'required': False},
        }

    def _build_items(self, price_list, items_data):
        from stock.models import Item, ItemVariant

        for item_data in items_data:
            item = Item.objects.get(pk=item_data['item'])
            item_variant_id = item_data.get('item_variant')
            item_variant = ItemVariant.objects.get(pk=item_variant_id) if item_variant_id else None
            if item.organization_id != price_list.organization_id:
                raise serializers.ValidationError({'items': 'Item does not belong to the price list organization.'})
            if item_variant_id and item_variant.organization_id != price_list.organization_id:
                raise serializers.ValidationError({'items': 'Variant does not belong to the price list organization.'})
            if item_variant_id and item_variant.item_id != item.id:
                raise serializers.ValidationError({'items': 'Variant must belong to the selected item.'})
            rate = item_data.get('rate')
            if rate is None:
                raise serializers.ValidationError({'items': 'Each price list item requires a rate.'})

            PriceListItem.objects.create(
                price_list=price_list,
                item=item,
                item_variant=item_variant,
                rate=rate,
                notes=item_data.get('notes', ''),
            )

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        price_list = PriceList.objects.create(**validated_data)
        self._build_items(price_list, items_data)
        return price_list

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if items_data is not None:
            instance.items.all().delete()
            self._build_items(instance, items_data)
        return instance


class QuotationItemSerializer(serializers.ModelSerializer):
    """Serialize a quoted line item with pricing provenance."""

    item_name = serializers.CharField(source='item.name', read_only=True)
    item_sku = serializers.CharField(source='item.sku', read_only=True)
    variant_sku = serializers.CharField(source='item_variant.sku', read_only=True)
    price_list_item_rate = serializers.DecimalField(source='price_list_item.rate', max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = QuotationItem
        fields = [
            'id', 'item', 'item_name', 'item_sku', 'item_variant', 'variant_sku',
            'price_list_item', 'price_list_item_rate', 'quantity', 'unit',
            'rate', 'discount', 'line_total', 'notes',
        ]


class QuotationListSerializer(serializers.ModelSerializer):
    """Serialize quotations for list views."""

    party_name = serializers.CharField(source='party.name', read_only=True)
    business_location_name = serializers.CharField(source='business_location.legal_name', read_only=True)
    price_list_name = serializers.CharField(source='price_list.name', read_only=True)

    class Meta:
        model = Quotation
        fields = [
            'id', 'quotation_number', 'party', 'party_name',
            'business_location', 'business_location_name',
            'price_list', 'price_list_name',
            'quotation_date', 'valid_until',
            'sub_total', 'discount_amount', 'grand_total', 'status',
        ]


class QuotationDetailSerializer(serializers.ModelSerializer):
    """Serialize a full quotation with nested line items."""

    items = QuotationItemSerializer(many=True, read_only=True)
    party = PartySerializer(read_only=True)
    price_list = PriceListListSerializer(read_only=True)
    converted_order_id = serializers.IntegerField(source='converted_order.id', read_only=True)
    converted_invoice_id = serializers.IntegerField(source='converted_invoice.id', read_only=True)

    class Meta:
        model = Quotation
        fields = [
            'id', 'quotation_number', 'organization', 'party', 'business_location',
            'price_list', 'quotation_date', 'valid_until', 'items',
            'sub_total', 'discount_amount', 'discount_type', 'discount_percent',
            'grand_total', 'status', 'notes', 'terms',
            'converted_order_id', 'converted_invoice_id',
            'created_at', 'updated_at', 'created_by',
        ]


class QuotationCreateSerializer(serializers.ModelSerializer):
    """Create or update a quotation with nested line items."""

    items = serializers.ListField(child=serializers.DictField(), write_only=True, required=False)

    class Meta:
        model = Quotation
        fields = [
            'organization', 'party', 'business_location', 'price_list',
            'quotation_date', 'valid_until', 'discount_amount', 'discount_type',
            'discount_percent', 'notes', 'terms', 'status', 'items',
        ]
        extra_kwargs = {
            'organization': {'required': False},
        }

    def validate_status(self, value):
        if value == 'Converted':
            raise serializers.ValidationError('Converted status is reserved for conversion actions.')
        return value

    def _build_items(self, quotation, items_data):
        from stock.models import Item, ItemVariant, Unit

        for item_data in items_data:
            item = Item.objects.get(pk=item_data['item'])
            item_variant_id = item_data.get('item_variant')
            item_variant = ItemVariant.objects.get(pk=item_variant_id) if item_variant_id else None
            if item.organization_id != quotation.organization_id:
                raise serializers.ValidationError({'items': 'Item does not belong to the quotation organization.'})
            if item_variant_id and item_variant.organization_id != quotation.organization_id:
                raise serializers.ValidationError({'items': 'Variant does not belong to the quotation organization.'})
            if item_variant_id and item_variant.item_id != item.id:
                raise serializers.ValidationError({'items': 'Variant must belong to the selected item.'})
            unit = item_variant.item.unit if item_variant else item.unit
            price_list_item = resolve_price_list_item(quotation.price_list, item, item_variant)

            explicit_rate = item_data.get('rate')
            if explicit_rate is not None:
                rate = Decimal(explicit_rate)
            elif price_list_item is not None:
                rate = price_list_item.rate
            elif item_variant is not None:
                rate = item_variant.unit_price
            else:
                rate = item.unit_price

            quantity = Decimal(item_data.get('quantity', '1'))
            discount = Decimal(item_data.get('discount', '0'))

            QuotationItem.objects.create(
                quotation=quotation,
                item=item,
                item_variant=item_variant,
                price_list_item=price_list_item,
                quantity=quantity,
                unit=unit,
                rate=rate,
                discount=discount,
                notes=item_data.get('notes', ''),
            )

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        request = self.context.get('request')
        if request and request.user and not validated_data.get('created_by'):
            validated_data['created_by'] = request.user
        quotation = Quotation.objects.create(**validated_data)
        self._build_items(quotation, items_data)
        quotation.recalculate_totals()
        return quotation

    def update(self, instance, validated_data):
        items_data = validated_data.pop('items', None)
        if instance.status in {'Converted', 'Cancelled'}:
            raise serializers.ValidationError({'quotation': 'Converted or cancelled quotations cannot be edited.'})
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if items_data is not None:
            instance.items.all().delete()
            self._build_items(instance, items_data)
        instance.recalculate_totals()
        return instance


# ===================== ORDER SERIALIZERS =====================

class OrderItemSerializer(serializers.ModelSerializer):
    """
    Serialize POS order line items.

    Order items are the editable line-level records used while a cart is still
    in the billing or hold state.
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


class DeliveryChallanItemSerializer(serializers.ModelSerializer):
    """Serialize line items inside a delivery challan."""
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_sku = serializers.CharField(source='item.sku', read_only=True)

    class Meta:
        model = DeliveryChallanItem
        fields = ['id', 'item', 'item_name', 'item_sku', 'item_variant', 'hsn_code', 'quantity', 'unit', 'rate', 'discount', 'total']


class DeliveryChallanListSerializer(serializers.ModelSerializer):
    """Serialize delivery challans for list views."""
    party_name = serializers.CharField(source='party.name', read_only=True)

    class Meta:
        model = DeliveryChallan
        fields = ['id', 'challan_number', 'party', 'party_name', 'business_location', 'challan_date', 'status']


class DeliveryChallanDetailSerializer(serializers.ModelSerializer):
    """Serialize a full delivery challan with nested line items."""
    items = DeliveryChallanItemSerializer(many=True, read_only=True)
    party = PartySerializer(read_only=True)
    business_location = BusinessLocationSerializer(read_only=True)

    class Meta:
        model = DeliveryChallan
        fields = ['id', 'challan_number', 'party', 'business_location', 'challan_date', 'status', 'items', 'notes', 'created_at', 'created_by']


class DeliveryChallanCreateSerializer(serializers.ModelSerializer):
    """Create a delivery challan with nested line items."""
    items = serializers.ListField(child=serializers.DictField(), write_only=True)

    class Meta:
        model = DeliveryChallan
        fields = ['party', 'business_location', 'challan_date', 'notes', 'items']

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        request = self.context.get('request')
        challan = DeliveryChallan.objects.create(
            created_by=request.user if request else None,
            **validated_data
        )
        from stock.models import Item, ItemVariant
        for item_data in items_data:
            item = Item.objects.get(pk=item_data['item'])
            variant_id = item_data.get('item_variant')
            variant = ItemVariant.objects.get(pk=variant_id) if variant_id else None
            DeliveryChallanItem.objects.create(
                delivery_challan=challan,
                item=item,
                item_variant=variant,
                hsn_code=item.tax_code.code if item.tax_code else '',
                quantity=item_data.get('quantity', 1),
                unit=item.unit,
                rate=item_data.get('rate', item.unit_price),
                discount=item_data.get('discount', 0),
            )
        return challan


class OrderSerializer(serializers.ModelSerializer):
    """
    Serialize POS orders while they are still part of the billing flow.

    The order serializer exposes the working cart, hold state, and conversion
    metadata used by the POS billing screen.
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
    Create a new POS order with nested line item data.

    The serializer accepts a compact payload from the billing UI and expands it
    into the parent order plus its OrderItem rows.
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
        organization = getattr(request, 'auth', None) if request else None

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
                if organization != SUPER_ADMIN_MARKER and variant.organization_id != getattr(organization, 'pk', None):
                    raise serializers.ValidationError({'items': 'Item variant does not belong to the authenticated organization'})
                unit = item.unit
                rate = item_data.get('rate', variant.unit_price)
            else:
                item = Item.objects.get(pk=item_id)
                if organization != SUPER_ADMIN_MARKER and item.organization_id != getattr(organization, 'pk', None):
                    raise serializers.ValidationError({'items': 'Item does not belong to the authenticated organization'})
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
    Serialize sales invoice line items.

    Each line exposes the tax snapshot that was captured at invoice creation so
    later tax code edits do not rewrite accounting history.
    """
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_sku = serializers.CharField(source='item.sku', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)

    class Meta:
        model = InvoiceItem
        fields = [
            'id', 'item', 'item_name', 'item_sku',
            'item_variant', 'batch', 'source_challan', 'hsn_code',
            'quantity', 'unit', 'unit_name',
            'rate', 'discount', 'taxable_amount',
            'cost_price_snapshot', 'cost_basis', 'gross_profit',
            'cgst_rate', 'cgst_amount',
            'sgst_rate', 'sgst_amount',
            'igst_rate', 'igst_amount',
            'total'
        ]


class InvoiceListSerializer(serializers.ModelSerializer):
    """
    Serialize invoices for list views.

    The list serializer stays compact because invoice grids usually need the
    document identity, the counterparty, and the money totals only.
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
            'tcs_rate', 'tcs_amount', 'gross_profit_amount', 'grand_total', 'status'
        ]


class InvoiceDetailSerializer(serializers.ModelSerializer):
    """
    Serialize a full invoice detail payload.

    This view exposes the nested invoice items, tax breakdown, and related
    master data required by print views and back-office invoice review screens.
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
            'source_challans',
            'items', 'sub_total', 'discount_amount', 'discount_type',
            'taxable_amount', 'tcs_rate', 'tcs_amount', 'gross_profit_amount', 'cgst_amount', 'sgst_amount', 'igst_amount',
            'round_off', 'grand_total', 'tax_summary',
            'notes', 'terms', 'status',
            'e_way_bill', 'e_invoice_details', 'order', 'created_at', 'created_by', 'created_by_name'
        ]


class InvoiceCreateSerializer(serializers.ModelSerializer):
    """
    Create a sales invoice with nested line item data.

    The serializer expands the incoming request payload into a draft invoice
    and its line items. Finalization happens separately so stock and accounting
    effects are only applied when the invoice is confirmed.
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
            'due_date', 'items', 'discount_amount', 'discount_type', 'tcs_rate',
            'notes', 'terms'
        ]

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        request = self.context.get('request')
        organization = getattr(request, 'auth', None) if request else None

        invoice = Invoice.objects.create(
            business_location=validated_data.get('business_location'),
            party=validated_data.get('party'),
            billing_state=validated_data.get('billing_state'),
            invoice_type=validated_data.get('invoice_type', 'Tax Invoice'),
            due_date=validated_data.get('due_date'),
            discount_amount=validated_data.get('discount_amount', 0),
            discount_type=validated_data.get('discount_type', 'Fixed'),
            tcs_rate=validated_data.get('tcs_rate', 0),
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
                if organization != SUPER_ADMIN_MARKER and variant.organization_id != getattr(organization, 'pk', None):
                    raise serializers.ValidationError({'items': 'Item variant does not belong to the authenticated organization'})
                unit = item.unit
            else:
                item = Item.objects.get(pk=item_id)
                if organization != SUPER_ADMIN_MARKER and item.organization_id != getattr(organization, 'pk', None):
                    raise serializers.ValidationError({'items': 'Item does not belong to the authenticated organization'})
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

        invoice.calculate_totals()
        return invoice


# ===================== PAYMENT SERIALIZERS =====================

class ReceiptSerializer(serializers.ModelSerializer):
    """
    Serialize customer receipts.

    Receipts are the payment-side counterpart to invoices and are used for
    partial payments, advances, and settlement tracking.
    """
    party = PartySerializer(read_only=True)
    received_by_name = serializers.CharField(source='received_by.username', read_only=True)

    class Meta:
        model = Receipt
        fields = [
            'id', 'receipt_number', 'party',
            'business_location', 'amount', 'payment_mode',
            'reference_number', 'transaction_date', 'notes',
            'created_at', 'received_by', 'received_by_name'
        ]


class ReceiptCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Receipt
        fields = ['party', 'business_location', 'amount', 'payment_mode', 'reference_number', 'notes']


class CreditNoteSerializer(serializers.ModelSerializer):
    """
    Serialize sales credit notes.

    Credit notes represent customer returns and optionally drive stock reversal
    when the returned goods are brought back into inventory.
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
    """Serialize the line items inside a purchase order."""
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_sku = serializers.CharField(source='item.sku', read_only=True)

    class Meta:
        model = PurchaseOrderItem
        fields = ['id', 'item', 'item_name', 'item_sku', 'item_variant', 'quantity_ordered', 'quantity_received', 'unit', 'rate', 'discount', 'total']


class PurchaseOrderListSerializer(serializers.ModelSerializer):
    """Serialize purchase orders for compact list responses."""
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    business_location_name = serializers.CharField(source='business_location.legal_name', read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = ['id', 'po_number', 'supplier', 'supplier_name', 'business_location', 'business_location_name', 'order_date', 'expected_date', 'grand_total', 'status']


class PurchaseOrderDetailSerializer(serializers.ModelSerializer):
    """Serialize a full purchase order with nested line items."""
    order_items = PurchaseOrderItemSerializer(many=True, read_only=True)
    supplier = PartySerializer(read_only=True)
    business_location = BusinessLocationSerializer(read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = ['id', 'po_number', 'supplier', 'business_location', 'order_date', 'expected_date', 'order_items', 'sub_total', 'discount_amount', 'grand_total', 'status', 'terms', 'notes', 'created_at']


class GRNItemSerializer(serializers.ModelSerializer):
    """Serialize items inside a goods receipt note."""
    item_name = serializers.CharField(source='item.name', read_only=True)
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)

    class Meta:
        model = GRNItem
        fields = ['id', 'item', 'item_name', 'item_variant', 'quantity', 'unit', 'rate', 'batch', 'batch_number', 'total']


class GRNListSerializer(serializers.ModelSerializer):
    """Serialize goods receipt notes for list views."""
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)

    class Meta:
        model = GoodReceiptNote
        fields = ['id', 'grn_number', 'supplier', 'supplier_name', 'received_date', 'supplier_invoice_number', 'status']


class GRNDetailSerializer(serializers.ModelSerializer):
    """Serialize a complete goods receipt note with nested line items."""
    grn_items = GRNItemSerializer(many=True, read_only=True)
    supplier = PartySerializer(read_only=True)
    purchase_order = PurchaseOrderListSerializer(read_only=True)

    class Meta:
        model = GoodReceiptNote
        fields = ['id', 'grn_number', 'purchase_order', 'supplier', 'business_location', 'received_date', 'supplier_invoice_number', 'supplier_invoice_date', 'status', 'posted_at', 'grn_items', 'notes', 'created_at']
        read_only_fields = ['id', 'grn_number', 'status', 'posted_at', 'created_at']


class PurchaseInvoiceItemSerializer(serializers.ModelSerializer):
    """Serialize purchase invoice line items and tax breakdown."""
    item_name = serializers.CharField(source='item.name', read_only=True)

    class Meta:
        model = PurchaseInvoiceItem
        fields = ['id', 'item', 'item_name', 'item_variant', 'hsn_code', 'quantity', 'unit', 'rate', 'discount', 'taxable_amount', 'cgst_rate', 'cgst_amount', 'sgst_rate', 'sgst_amount', 'igst_rate', 'igst_amount', 'total']


class PurchaseInvoiceListSerializer(serializers.ModelSerializer):
    """Serialize purchase invoices for compact list responses."""
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = ['id', 'invoice_number', 'supplier', 'supplier_name', 'supplier_invoice_number', 'invoice_date', 'grand_total', 'status']


class PurchaseInvoiceDetailSerializer(serializers.ModelSerializer):
    """Serialize a full purchase invoice with nested purchase line items."""
    items = PurchaseInvoiceItemSerializer(many=True, read_only=True)
    supplier = PartySerializer(read_only=True)
    business_location = BusinessLocationSerializer(read_only=True)
    grn = GRNListSerializer(read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = ['id', 'invoice_number', 'supplier', 'business_location', 'grn', 'purchase_order', 'supplier_invoice_number', 'supplier_invoice_date', 'invoice_date', 'due_date', 'items', 'sub_total', 'discount_amount', 'taxable_amount', 'cgst_amount', 'sgst_amount', 'igst_amount', 'round_off', 'grand_total', 'status', 'notes', 'created_at']


class DebitNoteSerializer(serializers.ModelSerializer):
    """Serialize debit notes used for purchase returns."""
    purchase_invoice = PurchaseInvoiceListSerializer(read_only=True)
    supplier = PartySerializer(read_only=True)

    class Meta:
        model = DebitNote
        fields = ['id', 'debit_note_number', 'purchase_invoice', 'supplier', 'amount', 'reason', 'is_stock_returned', 'notes', 'created_at']


class PaymentOutSerializer(serializers.ModelSerializer):
    """Serialize payments made to suppliers."""
    supplier = PartySerializer(read_only=True)

    class Meta:
        model = PaymentOut
        fields = ['id', 'payment_number', 'supplier', 'business_location', 'amount', 'payment_mode', 'reference_number', 'transaction_date', 'notes', 'created_at']
