from rest_framework.throttling import SimpleRateThrottle


class RequestFieldThrottle(SimpleRateThrottle):
    """Throttle a public auth endpoint by one normalized request field."""

    field_name = ''

    def get_cache_key(self, request, view):
        value = (request.data.get(self.field_name) or '').strip().lower()
        if not value:
            value = self.get_ident(request)
        return self.cache_format % {
            'scope': self.scope,
            'ident': value,
        }


class LoginIPRateThrottle(SimpleRateThrottle):
    scope = 'auth_login_ip'

    def get_cache_key(self, request, view):
        return self.cache_format % {
            'scope': self.scope,
            'ident': self.get_ident(request),
        }


class LoginIdentifierRateThrottle(RequestFieldThrottle):
    scope = 'auth_login_identifier'
    field_name = 'username'


class PasswordResetRequestIPRateThrottle(SimpleRateThrottle):
    scope = 'password_reset_request_ip'

    def get_cache_key(self, request, view):
        return self.cache_format % {
            'scope': self.scope,
            'ident': self.get_ident(request),
        }


class PasswordResetRequestIdentifierRateThrottle(RequestFieldThrottle):
    scope = 'password_reset_request_identifier'
    field_name = 'identifier'


class PasswordResetConfirmIPRateThrottle(SimpleRateThrottle):
    scope = 'password_reset_confirm_ip'

    def get_cache_key(self, request, view):
        return self.cache_format % {
            'scope': self.scope,
            'ident': self.get_ident(request),
        }


class PasswordResetConfirmIdentifierRateThrottle(RequestFieldThrottle):
    scope = 'password_reset_confirm_identifier'
    field_name = 'uid'
