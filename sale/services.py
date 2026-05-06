"""Sale-side posting helpers for receiving and PO reconciliation."""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from stock.models import Batch
from stock.services import post_stock_movement, quantize_quantity

from .models import GoodReceiptNote, Invoice, PurchaseOrder, PurchaseOrderItem


def _grn_batch_number(grn, grn_item):
    """Build the deterministic batch number used for a GRN line."""
    parts = [grn.grn_number or str(grn.pk or 'GRN')]
    if grn_item.pk:
        parts.append(str(grn_item.pk))
    return '-'.join(parts)[:50]


def _match_purchase_order_item(po, grn_item):
    """Find the PO line that matches a GRN line."""
    queryset = po.order_items.select_for_update().filter(item=grn_item.item)
    if grn_item.item_variant_id:
        exact = queryset.filter(item_variant=grn_item.item_variant)
        if exact.count() == 1:
            return exact.get()
        if exact.count() > 1:
            raise ValidationError({'purchase_order': 'Multiple PO lines match the received item variant.'})

    fallback = queryset.filter(item_variant__isnull=True)
    if fallback.count() == 1:
        return fallback.get()
    if fallback.count() > 1:
        raise ValidationError({'purchase_order': 'Multiple PO lines match the received item.'})

    if grn_item.item_variant_id:
        fallback = queryset.filter(item_variant=grn_item.item_variant)
        if fallback.exists():
            if fallback.count() == 1:
                return fallback.get()
            raise ValidationError({'purchase_order': 'Multiple PO lines match the received item variant.'})

    raise ValidationError({'purchase_order': 'No matching PO line was found for the received item.'})


def _recalculate_purchase_order_status(po):
    """Derive the PO receiving status from its line totals."""
    return po.recalculate_receiving_status()


@transaction.atomic
def post_good_receipt_note(grn, user=None):
    """Post GRN receipt quantities into stock and purchase-order receiving."""
    grn = GoodReceiptNote.objects.select_for_update().get(pk=grn.pk)
    if grn.status == 'Posted':
        return grn
    if grn.status == 'Cancelled':
        raise ValidationError({'grn': 'Cancelled GRNs cannot be posted.'})

    po = None
    if purchase_order_id := grn.purchase_order_id:
        po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order_id)

    grn_items = list(grn.grn_items.select_related('item').select_for_update())
    if not grn_items:
        raise ValidationError({'grn': 'GRN must contain at least one line item.'})

    organization = grn.business_location.organization
    posted_batches = []

    for grn_item in grn_items:
        quantity = quantize_quantity(grn_item.quantity)
        if quantity <= 0:
            raise ValidationError({'quantity': 'GRN quantity must be greater than zero.'})

        if po is not None:
            po_item = _match_purchase_order_item(po, grn_item)
            updated_received = quantize_quantity(po_item.quantity_received + quantity)
            ordered = quantize_quantity(po_item.quantity_ordered)
            if updated_received > ordered:
                raise ValidationError({
                    'quantity': f'Received quantity for {po_item.item.sku} exceeds ordered quantity.'
                })
            po_item.quantity_received = updated_received
            po_item.save(update_fields=['quantity_received'])

        movement = post_stock_movement(
            organization=organization,
            movement_type='Purchase',
            item=grn_item.item,
            item_variant=grn_item.item_variant,
            warehouse=grn.business_location,
            quantity=grn_item.quantity,
            rate=grn_item.rate,
            total_amount=grn_item.total,
            reference_number=grn.grn_number,
            status='Approved',
            source_document_type='sale.GRNItem',
            source_document_id=grn.pk,
            source_line_reference=grn_item.pk,
            notes='GRN receipt posting',
        )

        if grn_item.item_variant_id:
            batch = Batch.objects.select_for_update().get(batch_number=_grn_batch_number(grn, grn_item))
            grn_item.batch = batch
            grn_item.save(update_fields=['batch', 'total'])
            posted_batches.append(batch.pk)

    if po is not None:
        po.status = _recalculate_purchase_order_status(po)
        po.save(update_fields=['status'])

    grn.status = 'Posted'
    grn.posted_at = timezone.now()
    grn.save(update_fields=['status', 'posted_at'])
    return grn


@transaction.atomic
def generate_invoice_e_way_bill(invoice):
    """Generate and persist the invoice e-way bill payload."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    return invoice.generate_e_way_bill()


@transaction.atomic
def generate_invoice_e_invoice(invoice):
    """Generate and persist the invoice e-invoice payload."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    return invoice.generate_e_invoice()


@transaction.atomic
def generate_invoice_documents(invoice):
    """Generate and persist GST document payloads for a finalized invoice."""
    e_way_bill = generate_invoice_e_way_bill(invoice)
    e_invoice_details = generate_invoice_e_invoice(invoice)
    return {
        'e_way_bill': e_way_bill,
        'e_invoice_details': e_invoice_details,
    }
