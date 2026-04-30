from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from account.models import Organization
from sale.models import (
    Party,
    Order,
    OrderItem,
    Invoice,
    InvoiceItem,
    Receipt,
    ReceiptAllocation,
)
from sale.serializers import InvoiceDetailSerializer
from stock.models import Item, ItemVariant, StockMovement

from .models import CashTransaction, Shift


def quantize_money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def quantize_quantity(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.0001'))


def get_walk_in_party(organization, business_location):
    party, created = Party.objects.get_or_create(
        organization=organization,
        name='Walk-in Customer',
        defaults={
            'party_type': 'Customer',
            'state': business_location.state,
        },
    )
    if not created:
        dirty = False
        if party.party_type != 'Customer':
            party.party_type = 'Customer'
            dirty = True
        if business_location.state_id and party.state_id != business_location.state_id:
            party.state = business_location.state
            dirty = True
        if dirty:
            party.save(update_fields=['party_type', 'state', 'updated_at'])
    return party


def _validate_scope(shift, business_location):
    if shift.status != 'Open':
        raise ValidationError({'shift': 'Shift must be open before checkout.'})
    if shift.warehouse_id != business_location.id:
        raise ValidationError({'business_location': 'Checkout location must match the active shift.'})
    if shift.warehouse.organization_id != business_location.organization_id:
        raise ValidationError({'business_location': 'Shift and business location must belong to the same organization.'})


def _build_order_items(order, items_data, organization):
    for item_data in items_data:
        item = Item.objects.select_related('tax_code', 'unit').get(pk=item_data['item'])
        item_variant_id = item_data.get('item_variant')
        item_variant = ItemVariant.objects.select_related('item').get(pk=item_variant_id) if item_variant_id else None

        if item.organization_id != organization.id:
            raise ValidationError({'items': 'Item does not belong to the checkout organization.'})
        if item_variant_id:
            if item_variant.organization_id != organization.id:
                raise ValidationError({'items': 'Variant does not belong to the checkout organization.'})
            if item_variant.item_id != item.id:
                raise ValidationError({'items': 'Variant must belong to the selected item.'})

        unit = item_variant.item.unit if item_variant else item.unit
        rate = quantize_money(item_data.get('rate', item_variant.unit_price if item_variant else item.unit_price))
        discount = quantize_money(item_data.get('discount', '0'))
        quantity = quantize_quantity(item_data.get('quantity', '1'))

        OrderItem.objects.create(
            order=order,
            item=item,
            item_variant=item_variant,
            hsn_code=item.tax_code.code if item.tax_code else '',
            quantity=quantity,
            unit=unit,
            rate=rate,
            discount=discount,
        )


def _build_invoice_from_order(order, *, invoice_type, due_date, notes, terms, created_by):
    billing_state = order.party.state if order.party and order.party.state_id else order.business_location.state
    if billing_state is None:
        raise ValidationError({'billing_state': 'A billing state is required for POS invoices.'})

    invoice = Invoice.objects.create(
        order=order,
        party=order.party,
        billing_state=billing_state,
        business_location=order.business_location,
        invoice_type=invoice_type,
        due_date=due_date,
        discount_amount=order.discount_amount,
        discount_type=order.discount_type,
        notes=notes,
        terms=terms,
        created_by=created_by,
        salesperson=created_by,
    )

    for order_item in order.order_items.select_related('item', 'item_variant', 'unit').all():
        taxable_amount = quantize_money((order_item.quantity * order_item.rate) - order_item.discount)
        if order_item.item.tax_code and order_item.item.tax_code.code:
            hsn_code = order_item.item.tax_code.code
        else:
            hsn_code = ''

        InvoiceItem.objects.create(
            invoice=invoice,
            item=order_item.item,
            item_variant=order_item.item_variant,
            batch=None,
            hsn_code=hsn_code,
            quantity=order_item.quantity,
            unit=order_item.unit,
            rate=order_item.rate,
            discount=order_item.discount,
            taxable_amount=taxable_amount,
        )

    invoice.calculate_totals()
    return invoice


@transaction.atomic
def checkout_pos_order(*, shift, business_location, user, items=None, order_id=None, party=None, invoice_type='Cash', due_date=None, notes='', terms='', payment_mode='Cash', paid_amount=Decimal('0'), reference_number='', receipt_notes='', discount_amount=Decimal('0'), discount_type='Fixed'):
    _validate_scope(shift, business_location)
    organization = shift.warehouse.organization

    order = None
    if order_id is not None:
        order = Order.objects.select_for_update().prefetch_related('order_items').get(pk=order_id)
        if order.business_location_id != business_location.id:
            raise ValidationError({'order_id': 'Order business location does not match the active shift.'})
        if order.status == 'Cancelled':
            raise ValidationError({'order_id': 'Cancelled orders cannot be checked out.'})
        if not order.order_items.exists():
            raise ValidationError({'order_id': 'Order must contain at least one item before checkout.'})
    else:
        if not items:
            raise ValidationError({'items': 'At least one item is required for POS checkout.'})
        order = Order.objects.create(
            party=party,
            business_location=business_location,
            discount_amount=quantize_money(discount_amount),
            discount_type=discount_type,
            created_by=user,
        )
        _build_order_items(order, items, organization)
        order.calculate_totals()

    if order.party_id is None:
        order.party = get_walk_in_party(organization, business_location)
        order.save(update_fields=['party'])

    invoice = _build_invoice_from_order(
        order,
        invoice_type=invoice_type,
        due_date=due_date,
        notes=notes,
        terms=terms,
        created_by=user,
    )
    invoice.finalize()

    receipt = None
    paid_amount = quantize_money(paid_amount)
    if paid_amount > 0:
        if paid_amount > invoice.grand_total:
            raise ValidationError({'paid_amount': 'Paid amount cannot exceed the invoice total.'})

        receipt = Receipt.objects.create(
            party=order.party,
            business_location=business_location,
            amount=paid_amount,
            payment_mode=payment_mode,
            reference_number=reference_number,
            notes=receipt_notes or notes,
            received_by=user,
        )
        ReceiptAllocation.objects.create(
            receipt=receipt,
            invoice=invoice,
            amount_allocated=paid_amount,
        )
        if payment_mode == 'Cash':
            CashTransaction.objects.create(
                shift=shift,
                transaction_type='CashIn',
                amount=paid_amount,
                reason=f'POS sale {invoice.invoice_number}',
                reference=receipt.receipt_number,
                created_by=user,
            )

    return order, invoice, receipt


def build_invoice_document_payload(invoice):
    data = InvoiceDetailSerializer(invoice).data
    data['print'] = {
        'company_name': invoice.business_location.legal_name,
        'company_gstin': invoice.business_location.gstin,
        'company_address': invoice.business_location.address,
        'amount_in_words': str(invoice.grand_total),
    }
    data['share'] = {
        'title': f'Invoice {invoice.invoice_number}',
        'text': f'Invoice {invoice.invoice_number} for {invoice.grand_total} at {invoice.business_location.legal_name}.',
    }
    return data


def build_shift_reconciliation(shift):
    start = shift.opening_time
    end = shift.closing_time or timezone.now()
    invoices = Invoice.objects.filter(
        business_location=shift.warehouse,
        status='Finalized',
        created_by=shift.user,
        invoice_date__gte=start,
        invoice_date__lte=end,
    )
    receipts = Receipt.objects.filter(
        business_location=shift.warehouse,
        received_by=shift.user,
        transaction_date__gte=start,
        transaction_date__lte=end,
    )
    stock_movements = StockMovement.objects.filter(
        warehouse=shift.warehouse,
        movement_type='Sale',
        status='Approved',
        posting_state='Posted',
        posted_at__gte=start,
        posted_at__lte=end,
    )

    invoice_count = invoices.count()
    invoice_total = sum((invoice.grand_total for invoice in invoices), Decimal('0'))
    receipt_total = sum((receipt.amount for receipt in receipts), Decimal('0'))
    cash_in = sum((txn.amount for txn in shift.transactions.filter(transaction_type='CashIn')), Decimal('0'))
    cash_out = sum((txn.amount for txn in shift.transactions.filter(transaction_type='CashOut')), Decimal('0'))
    stock_total = sum((movement.total_amount for movement in stock_movements), Decimal('0'))

    exceptions = []
    if invoice_count != stock_movements.count():
        exceptions.append('Invoice count does not match posted stock movement count.')
    if shift.status == 'Closed' and shift.variance is not None and shift.variance != (shift.closing_cash - (shift.opening_cash + cash_in - cash_out)):
        exceptions.append('Shift variance does not match recorded cash activity.')

    return {
        'shift_number': shift.shift_number,
        'status': shift.status,
        'opening_cash': str(shift.opening_cash),
        'closing_cash': str(shift.closing_cash) if shift.closing_cash is not None else None,
        'expected_cash': str(shift.expected_cash if shift.expected_cash is not None else (shift.opening_cash + cash_in - cash_out)),
        'variance': str(shift.variance) if shift.variance is not None else None,
        'invoice_count': invoice_count,
        'invoice_total': str(quantize_money(invoice_total)),
        'receipt_count': receipts.count(),
        'receipt_total': str(quantize_money(receipt_total)),
        'cash_in': str(quantize_money(cash_in)),
        'cash_out': str(quantize_money(cash_out)),
        'stock_movement_count': stock_movements.count(),
        'stock_movement_total': str(quantize_money(stock_total)),
        'exceptions': exceptions,
    }
