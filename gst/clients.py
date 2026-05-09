"""GST provider clients."""

import json
from urllib import request as urlrequest
from urllib.error import HTTPError
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class SandboxGSTClient:
    """Small HTTP client for Sandbox GST APIs."""

    def __init__(self):
        self.base_url = getattr(settings, 'SANDBOX_GST_BASE_URL', 'https://api.sandbox.co.in').rstrip('/')
        self.api_key = getattr(settings, 'SANDBOX_API_KEY', '')
        self.authorization = getattr(settings, 'SANDBOX_AUTHORIZATION', '')
        self.timeout = getattr(settings, 'SANDBOX_GST_TIMEOUT', 30)

    @property
    def is_configured(self):
        return bool(self.api_key and self.authorization)

    def require_configured(self):
        if not self.is_configured:
            raise ImproperlyConfigured('SANDBOX_API_KEY and SANDBOX_AUTHORIZATION are required for live GST API calls.')

    def post(self, path, payload, extra_headers=None):
        self.require_configured()
        endpoint = f'{self.base_url}{path}'
        headers = {
            'Authorization': self.authorization,
            'Content-Type': 'application/json',
            'x-api-key': self.api_key,
        }
        if extra_headers:
            headers.update(extra_headers)
        body = json.dumps(payload).encode('utf-8')
        request = urlrequest.Request(endpoint, data=body, headers=headers, method='POST')
        try:
            with urlrequest.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode('utf-8')
                status_code = response.status
        except HTTPError as exc:
            raw = exc.read().decode('utf-8')
            status_code = exc.code
        try:
            response_payload = json.loads(raw)
        except ValueError:
            response_payload = {'raw': raw}
        return endpoint, status_code, response_payload
