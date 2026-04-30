from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand

from stock.models import Batch, Item, ItemVariant, SerialNumber, StockMovement
from stock.services import movement_delta, quantize_quantity


class Command(BaseCommand):
    help = 'Reconcile current stock, batch balances, and serial consistency against posted stock movements.'

    def add_arguments(self, parser):
        parser.add_argument('--repair', action='store_true', help='Write the computed stock balances back to the models.')

    def handle(self, *args, **options):
        repair = options['repair']
        movement_qs = StockMovement.objects.filter(status='Approved', posting_state='Posted')

        item_deltas = defaultdict(lambda: Decimal('0'))
        batch_deltas = defaultdict(lambda: Decimal('0'))
        serial_issues = []

        for movement in movement_qs.select_related('item', 'item_variant', 'batch').prefetch_related('serial_numbers'):
            source_key = (
                ('variant', movement.item_variant_id)
                if movement.item_variant_id
                else ('item', movement.item_id)
            )
            item_deltas[source_key] += movement_delta(movement.movement_type, movement.quantity)

            allocations = movement.allocation_data.get('batch_allocations') or []
            if allocations:
                for allocation in allocations:
                    batch_deltas[allocation['batch_id']] += movement_delta(movement.movement_type, allocation['quantity'])
            elif movement.batch_id:
                batch_deltas[movement.batch_id] += movement_delta(movement.movement_type, movement.quantity)

            if getattr(movement.item, 'requires_serial_tracking', False) and movement.movement_type in {'Sale', 'Damage', 'TransferOut'}:
                if movement.serial_numbers.count() < int(movement.quantity):
                    serial_issues.append(
                        f'Movement {movement.id} for {movement.item.sku} is missing serial assignments.'
                    )

        item_problems = []
        for item in Item.objects.all():
            expected = quantize_quantity(item_deltas[('item', item.id)])
            current = quantize_quantity(item.current_stock or Decimal('0'))
            if expected != current:
                item_problems.append(f'Item {item.sku}: expected {expected}, found {current}')
                if repair:
                    item.current_stock = expected
                    item.save(update_fields=['current_stock', 'updated_at'])

        for variant in ItemVariant.objects.select_related('item').all():
            expected = quantize_quantity(item_deltas[('variant', variant.id)])
            current = quantize_quantity(variant.current_stock or Decimal('0'))
            if expected != current:
                item_problems.append(f'Variant {variant.sku}: expected {expected}, found {current}')
                if repair:
                    variant.current_stock = expected
                    variant.save(update_fields=['current_stock'])

        batch_problems = []
        for batch in Batch.objects.select_related('item_variant').all():
            expected = quantize_quantity(batch.quantity_received or Decimal('0'))
            expected = quantize_quantity(expected + batch_deltas[batch.id])
            current = quantize_quantity(batch.quantity_remaining or Decimal('0'))
            if expected != current:
                batch_problems.append(f'Batch {batch.batch_number}: expected {expected}, found {current}')
                if repair:
                    batch.quantity_remaining = expected
                    batch.save(update_fields=['quantity_remaining'])

        for problem in item_problems + batch_problems + serial_issues:
            self.stdout.write(problem)

        self.stdout.write(
            f'items={len(item_problems)} batches={len(batch_problems)} serial_issues={len(serial_issues)} repair={repair}'
        )
