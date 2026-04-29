from django.contrib import admin
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


@admin.register(Party)
class PartyAdmin(admin.ModelAdmin):
    list_display = ['name', 'party_type', 'gstin', 'state', 'credit_limit', 'is_active']
    list_filter = ['party_type', 'state', 'is_active']
    search_fields = ['name', 'gstin', 'phone']


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['order_number', 'party', 'business_location', 'status', 'grand_total', 'created_at']
    list_filter = ['status', 'business_location']
    search_fields = ['order_number']
    inlines = [OrderItemInline]


class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ['invoice_number', 'party', 'business_location', 'invoice_date', 'grand_total', 'status']
    list_filter = ['status', 'business_location', 'invoice_type']
    search_fields = ['invoice_number', 'party__name']
    inlines = [InvoiceItemInline]


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ['receipt_number', 'party', 'amount', 'payment_mode', 'transaction_date']
    list_filter = ['payment_mode', 'business_location']
    search_fields = ['receipt_number']


@admin.register(CreditNote)
class CreditNoteAdmin(admin.ModelAdmin):
    list_display = ['credit_note_number', 'invoice', 'party', 'amount', 'is_stock_returned', 'created_at']
    list_filter = ['is_stock_returned']
    search_fields = ['credit_note_number']


class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ['po_number', 'supplier', 'business_location', 'order_date', 'grand_total', 'status']
    list_filter = ['status', 'business_location']
    search_fields = ['po_number']
    inlines = [PurchaseOrderItemInline]


class GRNItemInline(admin.TabularInline):
    model = GRNItem
    extra = 0


@admin.register(GoodReceiptNote)
class GRNAdmin(admin.ModelAdmin):
    list_display = ['grn_number', 'supplier', 'business_location', 'received_date', 'supplier_invoice_number']
    list_filter = ['business_location']
    search_fields = ['grn_number']
    inlines = [GRNItemInline]


class PurchaseInvoiceItemInline(admin.TabularInline):
    model = PurchaseInvoiceItem
    extra = 0


@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(admin.ModelAdmin):
    list_display = ['invoice_number', 'supplier', 'business_location', 'invoice_date', 'grand_total', 'status']
    list_filter = ['status', 'business_location']
    search_fields = ['invoice_number']
    inlines = [PurchaseInvoiceItemInline]


@admin.register(DebitNote)
class DebitNoteAdmin(admin.ModelAdmin):
    list_display = ['debit_note_number', 'supplier', 'amount', 'created_at']
    search_fields = ['debit_note_number']


@admin.register(PaymentOut)
class PaymentOutAdmin(admin.ModelAdmin):
    list_display = ['payment_number', 'supplier', 'amount', 'payment_mode', 'transaction_date']
    list_filter = ['payment_mode', 'business_location']
    search_fields = ['payment_number']