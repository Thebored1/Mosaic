from rest_framework import authentication, permissions
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth.models import AnonymousUser
from .models import ApiConfiguration


class ApiKeyAuthentication(authentication.BaseAuthentication):
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
        config = ApiConfiguration.objects.filter(is_active=True).first()
        if not config:
            raise AuthenticationFailed('API access is not configured')

        if config.api_bearer_token != token:
            raise AuthenticationFailed('Invalid token')

        return (AnonymousUser(), config)


class ApiKeyPermission(permissions.BasePermission):
    message = 'Invalid or missing API token'

    def has_permission(self, request, view):
        return request.auth is not None