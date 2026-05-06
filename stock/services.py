from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.graphics import renderSVG

from .models import Batch, Item, ItemVariant, OpeningStock, SerialNumber, StockMovement


FIFO = 'FIFO'
LIFO = 'LIFO'

OUTBOUND_MOVEMENT_TYPES = {'Sale', 'Damage', 'TransferOut'}
INBOUND_MOVEMENT_TYPES = {'Purchase', 'Return', 'Opening', 'TransferIn'}
AUDIT_ONLY_MOVEMENT_TYPES = {'Reserve', 'Release'}


def quantize_quantity(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.0001'))


def quantize_money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'))


def get_stock_source(item, item_variant=None):
    return item_variant if item_variant is not None else item


def get_batch_ordering(source):
    valuation_method = getattr(source, 'valuation_method', FIFO)
    if valuation_method == LIFO:
        return ['-received_date', '-id']
    return ['received_date', 'id']


def movement_delta(movement_type, quantity):
    quantity = quantize_quantity(quantity)
    if movement_type in INBOUND_MOVEMENT_TYPES:
        return abs(quantity)
    if movement_type in OUTBOUND_MOVEMENT_TYPES:
        return -abs(quantity)
    if movement_type == 'Adjustment':
        return quantity
    return Decimal('0')


def movement_total_amount(quantity, rate, total_amount=None):
    if total_amount is not None:
        return quantize_money(total_amount)
    return quantize_money(quantize_quantity(quantity) * quantize_money(rate))


def source_reference(source_document_type='', source_document_id='', source_line_reference=''):
    return {
        'source_document_type': source_document_type or '',
        'source_document_id': str(source_document_id or ''),
        'source_line_reference': str(source_line_reference or ''),
    }


@transaction.atomic
def record_stock_movement(
    *,
    organization,
    movement_type,
    item,
    quantity,
    rate,
    item_variant=None,
    batch=None,
    warehouse=None,
    cgst_rate=Decimal('0'),
    sgst_rate=Decimal('0'),
    igst_rate=Decimal('0'),
    total_amount=None,
    reference_number='',
    notes='',
    status='Pending',
    posting_state='Pending',
    source_document_type='',
    source_document_id='',
    source_line_reference='',
    allocation_data=None,
):
    movement = StockMovement.objects.create(
        organization=organization,
        movement_type=movement_type,
        item=item,
        item_variant=item_variant,
        batch=batch,
        warehouse=warehouse,
        quantity=quantize_quantity(quantity),
        rate=quantize_money(rate),
        cgst_rate=quantize_money(cgst_rate),
        sgst_rate=quantize_money(sgst_rate),
        igst_rate=quantize_money(igst_rate),
        total_amount=movement_total_amount(quantity, rate, total_amount),
        reference_number=reference_number,
        status=status,
        posting_state=posting_state,
        source_document_type=source_document_type or '',
        source_document_id=str(source_document_id or ''),
        source_line_reference=str(source_line_reference or ''),
        allocation_data=allocation_data or {},
        notes=notes,
    )
    return movement


def _select_serials(source, quantity, warehouse=None):
    if Decimal(quantity) != Decimal(quantity).to_integral_value():
        raise ValidationError({'quantity': 'Serialized stock must be moved in whole numbers.'})
    queryset = SerialNumber.objects.select_for_update().filter(
        item=source.item if isinstance(source, ItemVariant) else source,
        status='Available',
    )
    if isinstance(source, ItemVariant):
        queryset = queryset.filter(item_variant=source)
    else:
        queryset = queryset.filter(item_variant__isnull=True)
    if warehouse is not None:
        queryset = queryset.filter(warehouse=warehouse)

    serials = list(queryset.order_by('created_at', 'id')[: int(quantity)])
    if len(serials) < int(quantity):
        raise ValidationError({'serial_numbers': 'Not enough available serial numbers for this stock movement.'})
    return serials


def _allocate_from_batches(source_variant, quantity, preferred_batch=None):
    if source_variant is None:
        return []

    batches = list(
        Batch.objects.select_for_update()
        .filter(item_variant=source_variant, quantity_remaining__gt=0)
        .order_by(*get_batch_ordering(source_variant))
    )
    if preferred_batch is not None:
        batches = [preferred_batch] + [batch for batch in batches if batch.pk != preferred_batch.pk]

    remaining = quantize_quantity(quantity)
    allocations = []
    for batch in batches:
        if remaining <= 0:
            break
        available = quantize_quantity(batch.quantity_remaining)
        if available <= 0:
            continue
        used = min(available, remaining)
        batch.quantity_remaining = quantize_quantity(batch.quantity_remaining - used)
        batch.save(update_fields=['quantity_remaining'])
        allocations.append({
            'batch_id': batch.id,
            'batch_number': batch.batch_number,
            'quantity': str(used),
        })
        remaining = quantize_quantity(remaining - used)

    if remaining > 0:
        raise ValidationError({'batch': 'Not enough batch quantity available to satisfy this stock movement.'})

    return allocations


def _apply_inbound_batch(movement, source, quantity):
    if movement.batch_id:
        batch = Batch.objects.select_for_update().get(pk=movement.batch_id)
        batch.quantity_received = quantize_quantity(batch.quantity_received + quantity)
        batch.quantity_remaining = quantize_quantity(batch.quantity_remaining + quantity)
        batch.cost_per_unit = quantize_money(movement.rate)
        if movement.movement_date:
            batch.received_date = movement.movement_date.date()
        batch.save(update_fields=['quantity_received', 'quantity_remaining', 'cost_per_unit', 'received_date'])
        return batch

    if isinstance(source, ItemVariant):
        batch_number_parts = [movement.reference_number or movement.source_document_id or 'BATCH']
        if movement.source_line_reference:
            batch_number_parts.append(movement.source_line_reference)
        batch_number = '-'.join(part for part in batch_number_parts if part)[:50]
        batch, _ = Batch.objects.get_or_create(
            batch_number=batch_number,
            defaults={
                'item_variant': source,
                'quantity_received': quantize_quantity(quantity),
                'quantity_remaining': quantize_quantity(quantity),
                'cost_per_unit': quantize_money(movement.rate),
                'received_date': movement.movement_date.date() if movement.movement_date else timezone.now().date(),
            },
        )
        if not _:
            batch.quantity_received = quantize_quantity(batch.quantity_received + quantity)
            batch.quantity_remaining = quantize_quantity(batch.quantity_remaining + quantity)
            batch.cost_per_unit = quantize_money(movement.rate)
            batch.save(update_fields=['quantity_received', 'quantity_remaining', 'cost_per_unit'])
        return batch

    return None


def _apply_outbound_batch_and_serials(movement, source, quantity, warehouse=None):
    batch_allocations = []
    serials = []
    transfer_to_warehouse_id = (movement.allocation_data or {}).get('transfer_to_warehouse_id')

    preferred_batch = movement.batch if movement.batch_id else None
    requires_serial_tracking = (
        source.item.requires_serial_tracking if isinstance(source, ItemVariant) else getattr(source, 'requires_serial_tracking', False)
    )
    if isinstance(source, ItemVariant):
        has_batches = preferred_batch is not None or Batch.objects.filter(item_variant=source, quantity_remaining__gt=0).exists()
        if has_batches:
            batch_allocations = _allocate_from_batches(source, quantity, preferred_batch=preferred_batch)

    if requires_serial_tracking:
        serials = _select_serials(source, quantity, warehouse=warehouse)
        for serial in serials:
            if movement.movement_type == 'TransferOut' and transfer_to_warehouse_id:
                serial.warehouse_id = transfer_to_warehouse_id
                serial.notes = movement.notes
                serial.save(update_fields=['warehouse', 'notes', 'updated_at'])
            else:
                serial.status = 'Sold'
                serial.sale_date = timezone.now()
                serial.notes = movement.notes
                serial.save(update_fields=['status', 'sale_date', 'notes', 'updated_at'])

    return batch_allocations, serials


def _apply_stock_delta(movement, reverse=False):
    source = get_stock_source(movement.item, movement.item_variant)
    if source is None:
        return {'batch_allocations': [], 'serial_numbers': []}

    source = source.__class__.objects.select_for_update().get(pk=source.pk)
    quantity = quantize_quantity(movement.quantity)
    delta = movement_delta(movement.movement_type, quantity)
    if reverse:
        delta = delta * Decimal('-1')

    batch_allocations = []
    serials = []

    if delta > 0:
        source.current_stock = quantize_quantity((source.current_stock or Decimal('0')) + delta)
        source.save(update_fields=['current_stock', 'updated_at'] if hasattr(source, 'updated_at') else ['current_stock'])
        batch = _apply_inbound_batch(movement, source, delta)
        if batch is not None:
            batch_allocations.append({
                'batch_id': batch.id,
                'batch_number': batch.batch_number,
                'quantity': str(delta),
            })
    elif delta < 0:
        outbound_quantity = abs(delta)
        current_stock = quantize_quantity(source.current_stock or Decimal('0'))
        if current_stock < outbound_quantity:
            raise ValidationError({'quantity': 'Inventory cannot go below zero.'})

        batch_allocations, serials = _apply_outbound_batch_and_serials(
            movement,
            source,
            outbound_quantity,
            warehouse=movement.warehouse,
        )
        source.current_stock = quantize_quantity(current_stock - outbound_quantity)
        source.save(update_fields=['current_stock', 'updated_at'] if hasattr(source, 'updated_at') else ['current_stock'])

    movement.allocation_data = {
        **(movement.allocation_data or {}),
        'batch_allocations': batch_allocations,
        'serial_numbers': [serial.serial_number for serial in serials],
        'applied_delta': str(delta),
    }
    movement.posting_state = 'Posted'
    movement.posted_at = timezone.now()
    movement.status = 'Approved'
    movement.save(update_fields=['allocation_data', 'posting_state', 'posted_at', 'status', 'updated_at'])
    if serials:
        movement.serial_numbers.set(serials)
    return {'batch_allocations': batch_allocations, 'serial_numbers': serials}


@transaction.atomic
def post_existing_stock_movement(movement):
    if movement.posting_state == 'Posted':
        return movement
    if movement.status == 'Rejected':
        raise ValidationError('Rejected stock movements cannot be posted.')
    _apply_stock_delta(movement)
    return movement


@transaction.atomic
def post_stock_movement(**kwargs):
    status = kwargs.pop('status', 'Approved')
    posting_state = kwargs.pop('posting_state', 'Pending')
    movement = record_stock_movement(**kwargs, status=status, posting_state=posting_state)
    _apply_stock_delta(movement)
    return movement


@transaction.atomic
def reverse_stock_movement(movement, reference_number='', notes=''):
    source = get_stock_source(movement.item, movement.item_variant)
    if source is None:
        raise ValidationError('Cannot reverse a stock movement without an inventory source.')

    source = source.__class__.objects.select_for_update().get(pk=source.pk)
    quantity = quantize_quantity(movement.quantity)
    reverse_delta = movement_delta(movement.movement_type, quantity) * Decimal('-1')

    source.current_stock = quantize_quantity((source.current_stock or Decimal('0')) + reverse_delta)
    source.save(update_fields=['current_stock', 'updated_at'] if hasattr(source, 'updated_at') else ['current_stock'])

    batch_allocations = movement.allocation_data.get('batch_allocations') or []
    if batch_allocations:
        batch_adjustment = Decimal('1') if reverse_delta > 0 else Decimal('-1')
        for allocation in batch_allocations:
            batch = Batch.objects.select_for_update().get(pk=allocation['batch_id'])
            batch.quantity_remaining = quantize_quantity(
                batch.quantity_remaining + (Decimal(str(allocation['quantity'])) * batch_adjustment)
            )
            batch.save(update_fields=['quantity_remaining'])
    elif movement.batch_id:
        batch = Batch.objects.select_for_update().get(pk=movement.batch_id)
        batch.quantity_remaining = quantize_quantity(batch.quantity_remaining + reverse_delta)
        batch.save(update_fields=['quantity_remaining'])

    serials = list(movement.serial_numbers.select_for_update())
    for serial in serials:
        serial.status = 'Available' if reverse_delta > 0 else 'Sold'
        serial.sale_date = None if reverse_delta > 0 else movement.movement_date
        if notes:
            serial.notes = notes
        serial.save(update_fields=['status', 'sale_date', 'notes', 'updated_at'])

    reversal = record_stock_movement(
        organization=movement.organization,
        movement_type='Return',
        item=movement.item,
        item_variant=movement.item_variant,
        batch=movement.batch,
        warehouse=movement.warehouse,
        quantity=quantity,
        rate=movement.rate,
        cgst_rate=movement.cgst_rate,
        sgst_rate=movement.sgst_rate,
        igst_rate=movement.igst_rate,
        total_amount=movement.total_amount,
        reference_number=reference_number or movement.reference_number,
        notes=notes or movement.notes,
        status='Approved',
        posting_state='Posted',
        source_document_type=movement.source_document_type,
        source_document_id=movement.source_document_id,
        source_line_reference=movement.source_line_reference,
        allocation_data={
            'reversal_of': movement.id,
            'batch_allocations': batch_allocations,
            'serial_numbers': [serial.serial_number for serial in serials],
            'reverse_delta': str(reverse_delta),
        },
    )
    reversal.posted_at = timezone.now()
    reversal.save(update_fields=['posted_at', 'updated_at'])
    if serials:
        reversal.serial_numbers.set(serials)

    movement.posting_state = 'Reversed'
    movement.save(update_fields=['posting_state', 'updated_at'])
    return reversal


@transaction.atomic
def approve_opening_stock(opening_stock, approved_by=None):
    if opening_stock.status == 'Approved':
        return opening_stock
    if opening_stock.status == 'Rejected':
        raise ValidationError('Rejected opening stock cannot be approved.')

    movement = post_stock_movement(
        organization=opening_stock.organization,
        movement_type='Opening',
        item=opening_stock.item,
        item_variant=opening_stock.item_variant,
        quantity=opening_stock.quantity,
        rate=opening_stock.unit_cost,
        total_amount=quantize_money(opening_stock.quantity * opening_stock.unit_cost),
        reference_number=f'OPEN-{opening_stock.pk}',
        notes=opening_stock.notes,
        source_document_type='stock.OpeningStock',
        source_document_id=opening_stock.pk,
        source_line_reference=opening_stock.pk,
    )
    opening_stock.status = 'Approved'
    opening_stock.save(update_fields=['status', 'updated_at'])
    return movement


@transaction.atomic
def reject_opening_stock(opening_stock, notes=''):
    opening_stock.status = 'Rejected'
    if notes:
        opening_stock.notes = notes
    opening_stock.save(update_fields=['status', 'notes', 'updated_at'])
    return opening_stock


@transaction.atomic
def transfer_stock_between_warehouses(
    *,
    organization,
    item,
    from_warehouse,
    to_warehouse,
    quantity,
    rate,
    item_variant=None,
    reference_number='',
    notes='',
):
    """Create linked outbound and inbound movements for a warehouse transfer."""
    if from_warehouse.id == to_warehouse.id:
        raise ValidationError({'to_warehouse': 'Source and destination warehouses must be different.'})
    if from_warehouse.organization_id != to_warehouse.organization_id:
        raise ValidationError({'to_warehouse': 'Warehouses must belong to the same organization.'})
    if organization != from_warehouse.organization:
        raise ValidationError({'from_warehouse': 'Warehouse must belong to the authenticated organization.'})

    reference_number = reference_number or f'TRF-{from_warehouse.code}-{to_warehouse.code}-{timezone.now():%Y%m%d%H%M%S}'

    out_movement = post_stock_movement(
        organization=from_warehouse.organization,
        movement_type='TransferOut',
        item=item,
        item_variant=item_variant,
        warehouse=from_warehouse,
        quantity=quantity,
        rate=rate,
        reference_number=reference_number,
        notes=notes,
        status='Approved',
        source_document_type='stock.Transfer',
        source_document_id=reference_number,
        source_line_reference='transfer-out',
        allocation_data={'transfer_to_warehouse_id': to_warehouse.id},
    )
    in_movement = post_stock_movement(
        organization=to_warehouse.organization,
        movement_type='TransferIn',
        item=item,
        item_variant=item_variant,
        warehouse=to_warehouse,
        quantity=quantity,
        rate=rate,
        reference_number=reference_number,
        notes=notes,
        status='Approved',
        source_document_type='stock.Transfer',
        source_document_id=reference_number,
        source_line_reference='transfer-in',
    )
    return {
        'reference_number': reference_number,
        'out_movement': out_movement,
        'in_movement': in_movement,
    }


def generate_barcode_svg(value, *, human_readable=True):
    """Generate a Code128 barcode as SVG."""
    drawing = createBarcodeDrawing('Code128', value=str(value), humanReadable=human_readable)
    return renderSVG.drawToString(drawing)


def generate_qr_svg(value):
    """Generate a QR code as SVG."""
    drawing = createBarcodeDrawing('QR', value=str(value))
    return renderSVG.drawToString(drawing)
