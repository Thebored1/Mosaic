"""
Authentication and authorization primitives for configuration and tenant APIs.

This module handles two concerns:

1. Authentication from API tokens or browser cookies.
2. Role-based authorization against the application account model.

Supported principals:
    - organization-scoped UserAccount tokens
    - ecommerce-only UserAccount tokens
    - super admin tokens for cross-organization access

The module is intentionally shared across apps so stock, sale, pos, account,
and commerce can all enforce the same token semantics.
"""

from rest_framework import authentication, permissions
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated, PermissionDenied
from django.conf import settings

from configuration.models import ApiToken, SuperAdminToken


SUPER_ADMIN_MARKER = {'type': 'super_admin'}
ECOMMERCE_MARKER = {'type': 'ecommerce'}
ALL_ROLES = {'Owner', 'Admin', 'Manager', 'Sales', 'Delivery', 'Warehouse', 'Staff'}
ADMIN_ROLES = {'Owner', 'Admin'}
MANAGEMENT_ROLES = {'Owner', 'Admin', 'Manager'}
SALES_ROLES = {'Owner', 'Admin', 'Manager', 'Sales'}
WAREHOUSE_ROLES = {'Owner', 'Admin', 'Manager', 'Warehouse'}
POS_ROLES = {'Owner', 'Admin', 'Manager', 'Sales'}
COMMERCE_MANAGEMENT_ROLES = {'Owner', 'Admin', 'Manager', 'Sales'}
READ_ONLY_ROLES = ALL_ROLES

ACTION_ALIASES = {
    'list': 'read',
    'retrieve': 'read',
    'summary': 'read',
    'ledger': 'read',
    'print_data': 'read',
    'share': 'read',
    'reconciliation': 'read',
    'timeline': 'read',
    'request_trace': 'read',
    'entity_timeline': 'read',
    'user_activity': 'read',
    'daily_sales': 'read',
    'gst_register': 'read',
    'gstr1': 'read',
    'trial_balance': 'read',
    'general_ledger': 'read',
    'balance_sheet': 'read',
    'profit_loss': 'read',
    'aging': 'read',
    'party_statement': 'read',
    'invoice_profit': 'read',
    'expense_report': 'read',
    'combine': 'write',
    'create': 'write',
    'update': 'write',
    'partial_update': 'write',
    'set_default': 'write',
    'hold': 'write',
    'recall': 'write',
    'convert': 'write',
    'convert_to_order': 'write',
    'convert_to_invoice': 'write',
    'finalize': 'write',
    'close': 'write',
    'cancel': 'write',
    'pack': 'write',
    'ship': 'write',
    'deliver': 'write',
    'receive': 'write',
    'process': 'write',
    'ready': 'write',
    'fail': 'write',
    'send': 'write',
    'create_invoice': 'write',
    'destroy': 'delete',
}

ROLE_POLICIES = {
    'configuration_state': {
        'read': READ_ONLY_ROLES,
    },
    'configuration_warehouse': {
        'read': READ_ONLY_ROLES,
        'write': MANAGEMENT_ROLES,
        'delete': ADMIN_ROLES,
    },
    'stock_master': {
        'read': READ_ONLY_ROLES,
        'write': WAREHOUSE_ROLES,
        'delete': MANAGEMENT_ROLES,
    },
    'inventory_control': {
        'read': READ_ONLY_ROLES,
        'write': WAREHOUSE_ROLES,
        'delete': MANAGEMENT_ROLES,
    },
    'sale_state': {
        'read': READ_ONLY_ROLES,
    },
    'sale_business_location': {
        'read': READ_ONLY_ROLES,
        'write': MANAGEMENT_ROLES,
        'delete': ADMIN_ROLES,
    },
    'party_management': {
        'read': READ_ONLY_ROLES,
        'write': SALES_ROLES,
        'delete': MANAGEMENT_ROLES,
    },
    'sales_operations': {
        'read': READ_ONLY_ROLES,
        'write': SALES_ROLES,
        'delete': MANAGEMENT_ROLES,
    },
    'purchase_operations': {
        'read': WAREHOUSE_ROLES,
        'write': WAREHOUSE_ROLES,
        'delete': MANAGEMENT_ROLES,
    },
    'reporting': {
        'read': {'Owner', 'Admin', 'Manager', 'Sales', 'Warehouse'},
    },
    'accounting': {
        'read': MANAGEMENT_ROLES,
        'write': ADMIN_ROLES,
        'delete': ADMIN_ROLES,
    },
    'expense_tracking': {
        'read': MANAGEMENT_ROLES,
        'write': MANAGEMENT_ROLES,
        'delete': ADMIN_ROLES,
    },
    'pos_operations': {
        'read': POS_ROLES,
        'write': POS_ROLES,
        'delete': MANAGEMENT_ROLES,
    },
    'user_management': {
        'read': ADMIN_ROLES,
        'write': ADMIN_ROLES,
        'delete': ADMIN_ROLES,
    },
    'commerce_management': {
        'read': COMMERCE_MANAGEMENT_ROLES,
        'write': COMMERCE_MANAGEMENT_ROLES,
        'delete': MANAGEMENT_ROLES,
    },
    'commerce_settings': {
        'read': ADMIN_ROLES,
        'write': ADMIN_ROLES,
        'delete': ADMIN_ROLES,
    },
    'commerce_pricing': {
        'read': MANAGEMENT_ROLES,
        'write': MANAGEMENT_ROLES,
        'delete': ADMIN_ROLES,
    },
    'commerce_fulfillment': {
        'read': WAREHOUSE_ROLES,
        'write': WAREHOUSE_ROLES,
        'delete': MANAGEMENT_ROLES,
    },
    'commerce_after_sales': {
        'read': MANAGEMENT_ROLES,
        'write': MANAGEMENT_ROLES,
        'delete': ADMIN_ROLES,
    },
    'commerce_content': {
        'read': READ_ONLY_ROLES,
        'write': MANAGEMENT_ROLES,
        'delete': MANAGEMENT_ROLES,
    },
    'commerce_audit': {
        'read': MANAGEMENT_ROLES,
    },
    'audit_read': {
        'read': MANAGEMENT_ROLES,
    },
    'marketplace_settlement': {
        'read': MANAGEMENT_ROLES,
        'write': MANAGEMENT_ROLES,
        'delete': ADMIN_ROLES,
    },
}


def get_request_role(request):
    """
    Resolve the role for the current request.

    Super admin requests are treated as a separate privileged principal.
    Ecommerce-only accounts are blocked from organization-scoped operations.
    Organization-backed requests return the application role stored on the
    linked UserAccount.
    """
    if request.auth == SUPER_ADMIN_MARKER:
        return 'SuperAdmin'
    if request.auth == ECOMMERCE_MARKER:
        raise PermissionDenied('Create or join an organization to access this feature')

    user = getattr(request, 'user', None)
    if not user or not getattr(user, 'is_authenticated', False):
        raise NotAuthenticated('Valid API token required')

    user_account = getattr(user, 'account', None)
    if user_account is None or not user_account.is_active:
        raise PermissionDenied('Active user account required for this organization')
    if user_account.organization_id is None or user_account.account_type != 'org_user':
        raise PermissionDenied('Create or join an organization to access this feature')

    organization = getattr(request, 'auth', None)
    if hasattr(organization, 'pk') and user_account.organization_id != organization.pk:
        raise PermissionDenied('User account does not belong to the authenticated organization')

    return user_account.role


def get_policy_action(view):
    """
    Map a DRF action or HTTP method to a policy action.

    Viewsets expose named actions while plain APIViews only expose HTTP
    methods. This helper normalizes both into the read/write/delete policy
    model used below.
    """
    action = getattr(view, 'action', None)
    if action:
        return ACTION_ALIASES.get(action, action)

    method = getattr(getattr(view, 'request', None), 'method', 'GET').upper()
    if method in {'GET', 'HEAD', 'OPTIONS'}:
        return 'read'
    if method == 'DELETE':
        return 'delete'
    return 'write'


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """
    Authenticate using bearer token or the configured browser auth cookie.

    The browser-cookie path exists so the Next.js frontend can keep the token
    out of JavaScript while still using the same underlying token model.
    """
    keyword = 'Bearer'

    def authenticate(self, request):
        """
        Authenticate the incoming request and return a `(user, auth)` tuple.

        The method tries cookie transport first, then bearer header transport.
        If both are present, the header acts as an explicit override for API
        clients.
        """
        cookie_token = request.COOKIES.get(getattr(settings, 'AUTH_COOKIE_NAME', 'mosaic_auth'))
        if cookie_token:
            try:
                return self.authenticate_token(cookie_token)
            except AuthenticationFailed:
                if not request.headers.get('Authorization'):
                    raise

        auth_header = request.headers.get('Authorization')
        if not auth_header:
            if cookie_token:
                return None
            return None

        parts = auth_header.split()
        if len(parts) != 2:
            raise AuthenticationFailed('Invalid authorization header format')
        if parts[0].lower() != self.keyword.lower():
            raise AuthenticationFailed('Authorization header must use Bearer token')
        if not parts[1]:
            raise AuthenticationFailed('Authorization token is missing')

        token = parts[1]
        return self.authenticate_token(token)

    def authenticate_token(self, token):
        """
        Resolve a raw token against the hashed token tables.

        The method first attempts organization/ecommerce user tokens and then
        falls back to super admin tokens.
        """
        token_hash = ApiToken.hash_token(token)
        api_token = ApiToken.objects.filter(
            token_hash=token_hash,
            is_active=True
        ).select_related('user_account', 'user_account__organization', 'user_account__user').first()

        if api_token:
            user = api_token.user_account.user
            if not user.is_active or not api_token.user_account.is_active:
                raise AuthenticationFailed('User account is inactive')
            organization = api_token.user_account.organization
            if organization is None:
                return (user, ECOMMERCE_MARKER)
            return (user, organization)

        super_token_hash = SuperAdminToken.hash_token(token)
        super_token = SuperAdminToken.objects.filter(
            token_hash=super_token_hash,
            is_active=True
        ).select_related('user').first()

        if super_token:
            if not super_token.user.is_active or not super_token.user.is_superuser:
                raise AuthenticationFailed('Super admin account is inactive')
            return (super_token.user, SUPER_ADMIN_MARKER)

        raise AuthenticationFailed('Invalid or inactive API token')

    def authenticate_header(self, request):
        """Return the auth header keyword used for challenge responses."""
        return self.keyword


class ApiKeyPermission(permissions.BasePermission):
    """
    Require a valid API token or cookie-backed token.

    This is the base permission for authenticated endpoints. ScopedRolePermission
    builds on top of it for organization-aware authorization.
    """
    message = 'Valid API token required'

    def has_permission(self, request, view):
        """Reject requests that have not been authenticated by the token layer."""
        if request.auth is None:
            raise NotAuthenticated(self.message)
        return True


class ScopedRolePermission(ApiKeyPermission):
    """
    Permission class that maps view scopes to allowed roles.

    The permission scope is set on the view and translated into a role policy
    table. This keeps endpoint authorization declarative instead of hard-coding
    role checks in every viewset.
    """

    def has_permission(self, request, view):
        """Check the base token requirement and then enforce the role policy."""
        super().has_permission(request, view)

        if request.auth == SUPER_ADMIN_MARKER:
            return True

        scope = getattr(view, 'permission_scope', None)
        if not scope:
            return True

        role = get_request_role(request)
        allowed_roles = ROLE_POLICIES.get(scope, {}).get(get_policy_action(view), set())
        if role not in allowed_roles:
            raise PermissionDenied('You do not have permission to perform this action')
        return True

    def has_object_permission(self, request, view, obj):
        """Delegate object-level checks to the same role policy logic."""
        return self.has_permission(request, view)
