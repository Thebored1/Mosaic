import time
from uuid import uuid4

from .context import reset_request_context, set_request_context
from .services import record_request_event


class AuditContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        trace_id = uuid4().hex
        request.audit_trace_id = trace_id
        started_at = time.perf_counter()
        token = set_request_context({'request': request, 'trace_id': trace_id, 'started_at': started_at})
        try:
            response = self.get_response(request)
        except Exception as exc:
            record_request_event(request, exception=exc, started_at=started_at)
            reset_request_context(token)
            raise

        record_request_event(request, response=response, started_at=started_at)
        reset_request_context(token)
        return response
