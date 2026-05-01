"""Audit event creation helpers and request logging utilities."""

from decimal import Decimal
from datetime import date, datetime
from uuid import uuid4

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from .context import get_action_context, get_request_context
from .models import AuditEvent, AuditEventLink


REQUEST_METHODS_TO_LOG = {'POST', 'PUT', 'PATCH', 'DELETE'}
SENSITIVE_PATH_MARKERS = ('/token', '/auth', '/login', '/logout', '/revoke', '/issue')


def json_safe(value):
    """Convert common Python and model values into JSON-safe primitives."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if hasattr(value, '_meta') and hasattr(value, 'pk'):
        return value.pk
    return str(value)


def serialize_instance(instance):
    """Serialize a model instance into a shallow JSON-friendly snapshot."""
    if instance is None:
        return {}

    data = {}
    for field in instance._meta.get_fields():
        if getattr(field, 'many_to_many', False):
            continue
        if getattr(field, 'one_to_many', False):
            continue
        if not getattr(field, 'concrete', False):
            continue

        name = field.name
        try:
            value = getattr(instance, name)
        except Exception:
            continue
        data[name] = json_safe(value)
    return data


def safe_object_repr(obj):
    """Return a defensive string representation for audit storage."""
    if obj is None:
        return ''
    try:
        return str(obj)[:255]
    except Exception:
        model_name = obj.__class__.__name__
        pk = getattr(obj, 'pk', None)
        return f'{model_name}({pk})' if pk is not None else model_name


def _resolve_related_value(instance, attrs):
    """Follow a related-object attribute chain and return the terminal value."""
    value = instance
    for attr in attrs:
        if value is None:
            return None
        value = getattr(value, attr, None)
    return value


def resolve_organization(instance=None, request=None, explicit=None):
    """Infer the organization from the request or related business object."""
    if explicit is not None:
        return explicit

    if request is not None:
        auth = getattr(request, 'auth', None)
        if hasattr(auth, 'pk'):
            return auth
        user = getattr(request, 'user', None)
        account = getattr(user, 'account', None) if user is not None else None
        if account is not None and getattr(account, 'organization_id', None):
            return account.organization

    if instance is None:
        return None

    direct = getattr(instance, 'organization', None)
    if direct is not None:
        return direct

    for chain in (
        ('warehouse', 'organization'),
        ('business_location', 'organization'),
        ('order', 'business_location', 'organization'),
        ('invoice', 'business_location', 'organization'),
        ('purchase_invoice', 'business_location', 'organization'),
        ('shift', 'warehouse', 'organization'),
        ('item', 'organization'),
        ('item_variant', 'organization'),
        ('listing', 'organization'),
        ('party', 'organization'),
        ('user_account', 'organization'),
        ('order_line', 'organization'),
        ('shipment', 'organization'),
    ):
        organization = _resolve_related_value(instance, chain)
        if organization is not None:
            return organization

    return None


def resolve_actor(request=None):
    """Return the authenticated Django user and linked application account."""
    if request is None:
        return None, None
    user = getattr(request, 'user', None)
    if user is None or not getattr(user, 'is_authenticated', False):
        return None, None
    account = getattr(user, 'account', None)
    return user, account if getattr(account, 'is_active', False) else None


def should_log_request(request, response=None, exception=None):
    """Decide whether a request deserves a request-level audit event."""
    if exception is not None:
        return True
    method = getattr(request, 'method', '').upper()
    if method in REQUEST_METHODS_TO_LOG:
        return True
    if response is not None and getattr(response, 'status_code', 200) >= 400:
        return True
    path = getattr(request, 'path', '').lower()
    return any(marker in path for marker in SENSITIVE_PATH_MARKERS)


def record_event(
    *,
    event_type,
    instance=None,
    related_objects=None,
    before_state=None,
    after_state=None,
    metadata=None,
    outcome='success',
    error_message='',
    request=None,
    organization=None,
    actor_user=None,
    actor_account=None,
    source_app='',
    action='',
    status_code=None,
    duration_ms=None,
    request_path='',
    request_query='',
    request_method='',
    ip_address='',
    user_agent='',
):
    """Persist one immutable audit event after the surrounding transaction commits."""
    request_context = get_request_context() or {}
    request = request or request_context.get('request')
    action = action or get_action_context() or ''

    if actor_user is None and actor_account is None:
        actor_user, actor_account = resolve_actor(request)

    if organization is None:
        organization = resolve_organization(instance=instance, request=request)

    if request is not None:
        request_path = request_path or getattr(request, 'path', '')
        request_query = request_query or getattr(request, 'META', {}).get('QUERY_STRING', '')
        request_method = request_method or getattr(request, 'method', '')
        ip_address = ip_address or request.META.get('REMOTE_ADDR', '')
        user_agent = user_agent or request.META.get('HTTP_USER_AGENT', '')

    if instance is not None and not source_app:
        source_app = instance._meta.app_label

    content_type = None
    object_id = ''
    object_repr = ''
    if instance is not None:
        content_type = ContentType.objects.get_for_model(instance, for_concrete_model=False)
        object_id = str(getattr(instance, 'pk', '') or '')
        object_repr = safe_object_repr(instance)

    metadata = metadata or {}
    before_state = before_state or {}
    after_state = after_state or {}
    trace_id = request_context.get('trace_id', '')
    correlation_id = request_context.get('trace_id', '')

    def _create():
        event = AuditEvent.objects.create(
            trace_id=trace_id,
            correlation_id=correlation_id,
            event_type=event_type,
            action=action,
            source_app=source_app,
            organization=organization,
            actor_user=actor_user,
            actor_account=actor_account,
            request_method=request_method,
            request_path=request_path,
            request_query=request_query,
            status_code=status_code,
            ip_address=ip_address or None,
            user_agent=user_agent,
            duration_ms=duration_ms,
            content_type=content_type,
            object_id=object_id,
            object_repr=object_repr,
            before_state=json_safe(before_state),
            after_state=json_safe(after_state),
            metadata=json_safe(metadata),
            outcome=outcome,
            error_message=error_message,
        )
        for related in related_objects or []:
            if related is None:
                continue
            relation_type = 'related'
            related_metadata = {}
            related_object = related
            if isinstance(related, dict):
                relation_type = related.get('relation_type', 'related')
                related_metadata = related.get('metadata') or {}
                related_object = related.get('object')
            if related_object is None:
                continue
            related_content_type = ContentType.objects.get_for_model(related_object, for_concrete_model=False)
            AuditEventLink.objects.create(
                event=event,
                relation_type=relation_type,
                content_type=related_content_type,
                object_id=str(getattr(related_object, 'pk', '') or ''),
                object_repr=safe_object_repr(related_object),
                metadata=json_safe(related_metadata),
            )
        return event

    transaction.on_commit(_create)


def record_request_event(request, response=None, exception=None, started_at=None):
    """Record the request-level audit row for a completed HTTP cycle."""
    if not should_log_request(request, response=response, exception=exception):
        return None

    duration_ms = None
    if started_at is not None:
        import time

        duration_ms = max(0, int((time.perf_counter() - started_at) * 1000))

    status_code = getattr(response, 'status_code', None)
    outcome = 'failure' if exception is not None or (status_code is not None and status_code >= 400) else 'success'

    record_event(
        event_type='http.request',
        metadata={
            'exception': str(exception) if exception is not None else '',
        },
        outcome=outcome,
        error_message=str(exception) if exception is not None else '',
        request=request,
        status_code=status_code,
        duration_ms=duration_ms,
        request_path=getattr(request, 'path', ''),
        request_query=getattr(request, 'META', {}).get('QUERY_STRING', ''),
        request_method=getattr(request, 'method', ''),
        ip_address=getattr(request, 'META', {}).get('REMOTE_ADDR', ''),
        user_agent=getattr(request, 'META', {}).get('HTTP_USER_AGENT', ''),
        source_app='request',
        action=get_action_context() or '',
    )
