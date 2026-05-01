"""Smoke tests for the audit trail."""

from django.http import HttpResponse
from django.test import RequestFactory, TransactionTestCase

from account.models import Organization
from audit.middleware import AuditContextMiddleware
from audit.models import AuditEvent
from configuration.models import State, Warehouse


class AuditTrailTests(TransactionTestCase):
    """Verify audit rows are emitted for model and request activity."""

    reset_sequences = True

    def setUp(self):
        """Create shared fixtures for the audit tests."""
        self.factory = RequestFactory()
        self.state = State.objects.create(name='Karnataka', state_code='29')
        self.organization = Organization.objects.create(name='Trace Org')

    def test_model_save_creates_audit_event(self):
        """Saving a tracked model should emit an audit event."""
        Warehouse.objects.create(
            organization=self.organization,
            state=self.state,
            gstin='29ABCDE1234F1Z5',
            name='Main Warehouse',
            code='WH-01',
            legal_name='Trace Org Pvt Ltd',
            address='Bangalore',
        )

        event = AuditEvent.objects.filter(event_type='configuration.warehouse.create').first()
        self.assertIsNotNone(event)
        self.assertEqual(event.organization_id, self.organization.id)

    def test_request_middleware_links_request_and_model_events(self):
        """One request should produce linked request and model audit rows."""
        middleware = AuditContextMiddleware(
            lambda request: self._create_org_response(request)
        )
        request = self.factory.post('/v1/account/organizations/', {})

        response = middleware(request)

        self.assertEqual(response.status_code, 201)
        self.assertTrue(getattr(request, 'audit_trace_id', ''))

        events = AuditEvent.objects.filter(trace_id=request.audit_trace_id).order_by('created_at')
        self.assertGreaterEqual(events.count(), 2)
        self.assertTrue(events.filter(event_type='http.request').exists())
        self.assertTrue(events.filter(event_type='account.organization.create').exists())

    def _create_org_response(self, request):
        """Simulate a request handler that performs a tracked write."""
        Organization.objects.create(name='Request Trace Org')
        return HttpResponse(status=201)
