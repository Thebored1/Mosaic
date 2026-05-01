"""Model signal handlers that mirror business writes into audit events."""

from django.apps import apps
from django.db import models, transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from .services import record_event, serialize_instance


TARGET_APPS = {'account', 'commerce', 'pos', 'sale', 'stock', 'configuration'}


def _should_track_model(sender):
    """Return True when the sender belongs to one of the business apps."""
    app_label = getattr(sender._meta, 'app_label', '')
    return app_label in TARGET_APPS


def _before_state_key(sender):
    """Build the private attribute name used to store the prior snapshot."""
    return f'_audit_before_state_{sender._meta.label_lower}'


def _store_before_state(sender, instance):
    """Capture the pre-save snapshot for later audit comparison."""
    if instance.pk is None:
        return
    previous = sender.objects.filter(pk=instance.pk).first()
    if previous is not None:
        setattr(instance, _before_state_key(sender), serialize_instance(previous))


@receiver(pre_save)
def audit_pre_save(sender, instance, **kwargs):
    """Capture the prior state before a tracked model is saved."""
    if not _should_track_model(sender):
        return
    _store_before_state(sender, instance)


@receiver(post_save)
def audit_post_save(sender, instance, created, update_fields=None, **kwargs):
    """Write an audit event after a tracked model is saved."""
    if not _should_track_model(sender):
        return

    before_state = getattr(instance, _before_state_key(sender), {}) or {}
    after_state = serialize_instance(instance)
    event_type = f'{sender._meta.app_label}.{sender._meta.model_name}.{"create" if created else "update"}'
    metadata = {
        'created': created,
        'update_fields': sorted(update_fields) if update_fields else [],
        'signal': 'post_save',
    }
    record_event(
        event_type=event_type,
        instance=instance,
        before_state=before_state,
        after_state=after_state,
        metadata=metadata,
        source_app=sender._meta.app_label,
    )


@receiver(pre_delete)
def audit_pre_delete(sender, instance, **kwargs):
    """Capture the state before a tracked model is deleted."""
    if not _should_track_model(sender):
        return
    setattr(instance, _before_state_key(sender), serialize_instance(instance))


@receiver(post_delete)
def audit_post_delete(sender, instance, **kwargs):
    """Write an audit event after a tracked model is deleted."""
    if not _should_track_model(sender):
        return

    before_state = getattr(instance, _before_state_key(sender), {}) or serialize_instance(instance)
    record_event(
        event_type=f'{sender._meta.app_label}.{sender._meta.model_name}.delete',
        instance=instance,
        before_state=before_state,
        after_state={},
        metadata={'signal': 'post_delete'},
        outcome='success',
        source_app=sender._meta.app_label,
    )
