"""Request-local context helpers for audit correlation."""

from contextvars import ContextVar


_request_context = ContextVar('audit_request_context', default=None)
_action_context = ContextVar('audit_action_context', default=None)


def set_request_context(context):
    """Store the active request context for the current execution."""
    return _request_context.set(context)


def reset_request_context(token):
    """Restore the prior request context."""
    if token is not None:
        _request_context.reset(token)


def get_request_context():
    """Return the current request context, if any."""
    return _request_context.get()


def set_action_context(action):
    """Store the current logical audit action name."""
    return _action_context.set(action)


def reset_action_context(token):
    """Restore the prior audit action name."""
    if token is not None:
        _action_context.reset(token)


def get_action_context():
    """Return the current logical audit action name."""
    return _action_context.get()
