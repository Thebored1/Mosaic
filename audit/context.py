from contextvars import ContextVar


_request_context = ContextVar('audit_request_context', default=None)
_action_context = ContextVar('audit_action_context', default=None)


def set_request_context(context):
    return _request_context.set(context)


def reset_request_context(token):
    if token is not None:
        _request_context.reset(token)


def get_request_context():
    return _request_context.get()


def set_action_context(action):
    return _action_context.set(action)


def reset_action_context(token):
    if token is not None:
        _action_context.reset(token)


def get_action_context():
    return _action_context.get()
