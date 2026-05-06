"""Celery tasks for commerce lifecycle cleanup."""

from datetime import timedelta
import logging

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from account.models import UserAccount

from .models import Cart, InventoryReservation
from .models import (
    CommerceNotification,
    CommerceOrder,
    CommerceReturnRequest,
    CommerceShipment,
    MarketplacePayout,
    MarketplaceSettlement,
)

logger = logging.getLogger(__name__)


def _notify_accounts(accounts, *, organization, notification_type, title, message='', payload=None):
    """Create one notification per account."""
    created = 0
    for account in accounts:
        CommerceNotification.objects.create(
            user_account=account,
            organization=organization,
            notification_type=notification_type,
            title=title,
            message=message,
            payload=payload or {},
        )
        created += 1
    return created


@shared_task
def cleanup_stale_carts(days=7):
    """Mark old open carts as abandoned."""
    logger.info('Starting stale cart cleanup days=%s', days)
    cutoff = timezone.now() - timedelta(days=days)
    result = {
        'abandoned_carts': Cart.objects.filter(status='open', updated_at__lt=cutoff).update(status='abandoned'),
    }
    logger.info('Finished stale cart cleanup result=%s', result)
    return result


@shared_task
def expire_inventory_reservations(hours=24):
    """Release and expire old inventory reservations."""
    logger.info('Starting inventory reservation expiry hours=%s', hours)
    cutoff = timezone.now() - timedelta(hours=hours)
    queryset = InventoryReservation.objects.filter(status='reserved').filter(
        Q(expires_at__lt=timezone.now()) | Q(expires_at__isnull=True, reserved_at__lt=cutoff)
    )
    expired = 0
    for reservation in queryset.select_related('listing', 'order', 'order_line'):
        reservation.release(notes='Reservation expired by background job')
        reservation.status = 'expired'
        reservation.save(update_fields=['status'])
        expired += 1
    result = {'expired_reservations': expired}
    logger.info('Finished inventory reservation expiry result=%s', result)
    return result


@shared_task
def notify_order_placed(order_id):
    """Create buyer and seller notifications for a placed order."""
    logger.info('Creating order placed notifications order_id=%s', order_id)
    order = CommerceOrder.objects.select_related('user_account').prefetch_related('lines').get(pk=order_id)
    buyer_count = _notify_accounts(
        [order.user_account],
        organization=order.user_account.organization,
        notification_type='order_placed',
        title=f'Order placed: {order.order_number}',
        message='Your order has been placed successfully.',
        payload={'order_id': order.pk, 'order_number': order.order_number},
    )
    seller_org_ids = list(order.lines.values_list('organization_id', flat=True).distinct())
    seller_count = 0
    for organization_id in seller_org_ids:
        seller_accounts = UserAccount.objects.filter(organization_id=organization_id, is_active=True)
        seller_count += _notify_accounts(
            seller_accounts,
            organization=order.user_account.organization,
            notification_type='order_received',
            title=f'New order received: {order.order_number}',
            message='A storefront order now contains one of your listings.',
            payload={'order_id': order.pk, 'order_number': order.order_number},
        )
    result = {'buyer_notifications': buyer_count, 'seller_notifications': seller_count}
    logger.info('Created order placed notifications order_id=%s result=%s', order_id, result)
    return result


@shared_task
def notify_shipment_update(shipment_id, event='shipped'):
    """Create order shipment notifications."""
    logger.info('Creating shipment update notification shipment_id=%s event=%s', shipment_id, event)
    shipment = CommerceShipment.objects.select_related('order', 'order__user_account').get(pk=shipment_id)
    title = f'Order {shipment.order.order_number} {event}'
    return _notify_accounts(
        [shipment.order.user_account],
        organization=shipment.organization,
        notification_type=f'shipment_{event}',
        title=title,
        message=f'Your order has been {event}.',
        payload={'shipment_id': shipment.pk, 'order_id': shipment.order_id, 'event': event},
    )


@shared_task
def notify_return_update(return_request_id, event='received'):
    """Create return lifecycle notifications."""
    logger.info('Creating return update notification return_request_id=%s event=%s', return_request_id, event)
    return_request = CommerceReturnRequest.objects.select_related('order', 'order__user_account').get(pk=return_request_id)
    title = f'Return {event}: {return_request.order.order_number}'
    return _notify_accounts(
        [return_request.order.user_account],
        organization=return_request.organization,
        notification_type=f'return_{event}',
        title=title,
        message=f'Your return request has been {event}.',
        payload={'return_request_id': return_request.pk, 'order_id': return_request.order_id, 'event': event},
    )


@shared_task
def retry_marketplace_settlement_ready(settlement_id, notes='Retried by background job'):
    """Retry marking a marketplace settlement as ready."""
    logger.info('Running settlement ready task settlement_id=%s', settlement_id)
    settlement = MarketplaceSettlement.objects.get(pk=settlement_id)
    if settlement.status == 'pending':
        settlement.mark_ready(notes=notes)
    result = {'status': settlement.status}
    logger.info('Finished settlement ready task settlement_id=%s result=%s', settlement_id, result)
    return result


@shared_task
def retry_marketplace_payout_process(payout_id, notes='Retried by background job'):
    """Retry processing a marketplace payout."""
    logger.info('Running payout process task payout_id=%s', payout_id)
    payout = MarketplacePayout.objects.get(pk=payout_id)
    if payout.status == 'pending':
        payout.process(notes=notes)
    result = {'status': payout.status}
    logger.info('Finished payout process task payout_id=%s result=%s', payout_id, result)
    return result
