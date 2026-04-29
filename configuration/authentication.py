"""
Configuration App Authentication
=============================

API Key authentication for Organization-scoped access.
"""

from rest_framework import authentication, permissions
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from django.contrib.auth.models import AnonymousUser
from configuration.models import ApiConfiguration


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """Authenticate using bearer token linked to Organization."""
    keyword = 'Bearer'

    def authenticate(self, request):
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return None

        parts = auth_header.split()
        if len(parts) != 2 or parts[0] != self.keyword:
            return None

        token = parts[1]
        return self.authenticate_token(token)

    def authenticate_token(self, token):
        config = ApiConfiguration.objects.filter(
            api_bearer_token=token,
            is_active=True
        ).select_related('organization').first()

        if not config:
            # Return None to indicate authentication failure (will return 401)
            return None

        return (AnonymousUser(), config.organization)

    def authenticate_header(self, request):
        return self.keyword


class ApiKeyPermission(permissions.BasePermission):
    """Require valid API token."""
    message = 'Valid API token required'

    def has_permission(self, request, view):
        return request.auth is not None