"""
Account API views.

This module is the bridge between authentication, tenant setup, and the
application's user-facing account workflows. It exposes:

1. public ecommerce signup and organization onboarding
2. login, refresh, and logout flows for browser-cookie and bearer-token clients
3. the /me endpoint for a unified identity response
4. tenant-scoped CRUD for merchants, customers, and organization users

The code here intentionally keeps auth concerns centralized so the rest of the
application can focus on business logic and tenant-scoped behavior.
"""

import binascii
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.contrib.auth.models import User
from django.db import transaction
from django.utils.encoding import force_bytes, force_str
from django.utils.encoding import DjangoUnicodeDecodeError
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.db.models import Q
from rest_framework import serializers, viewsets, status
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, inline_serializer
from .models import Merchant, Customer, UserAccount
from .serializers import (
    MerchantSerializer, CustomerSerializer,
    UserAccountSerializer, UserAccountCreateSerializer, UserAccountWithTokenSerializer,
    OrganizationOnboardingSerializer, OrganizationOnboardingResponseSerializer,
    LoginSerializer, AuthSessionSerializer, EcommerceSignupSerializer,
    OrganizationCreateSerializer, AccountMeSerializer,
    PasswordResetRequestSerializer, PasswordResetRequestResponseSerializer,
    PasswordResetConfirmSerializer, PasswordResetConfirmResponseSerializer,
)
from configuration.authentication import (
    SUPER_ADMIN_MARKER, ECOMMERCE_MARKER, ScopedRolePermission, ApiKeyPermission,
    ROLE_POLICIES, get_request_role
)
from configuration.models import ApiToken
from .throttles import (
    LoginIdentifierRateThrottle,
    LoginIPRateThrottle,
    PasswordResetConfirmIdentifierRateThrottle,
    PasswordResetConfirmIPRateThrottle,
    PasswordResetRequestIdentifierRateThrottle,
    PasswordResetRequestIPRateThrottle,
)


logger = logging.getLogger(__name__)


def org_filter(qs, request):
    """
    Filter a queryset by the organization carried in the auth context.

    Ecommerce-only accounts return an empty queryset because they have no
    tenant scope yet. Super admins can optionally filter by organization id.
    """
    if not hasattr(request, 'auth') or request.auth is None:
        return qs.none()
    if request.auth == ECOMMERCE_MARKER:
        return qs.none()

    if request.auth == SUPER_ADMIN_MARKER:
        org_id = request.query_params.get('organization')
        if org_id:
            if hasattr(qs.model, '_meta') and any(f.name == 'organization' for f in qs.model._meta.get_fields()):
                return qs.filter(organization_id=org_id)
        return qs

    if hasattr(qs.model, '_meta') and any(f.name == 'organization' for f in qs.model._meta.get_fields()):
        return qs.filter(organization=request.auth)
    return qs


def save_for_request_organization(serializer, request):
    """
    Save a serializer instance under the organization derived from the request.

    Super admins must choose the organization explicitly because they can act
    across tenants. Regular org users inherit the organization from their token.
    Ecommerce-only accounts are rejected.
    """
    org_id = request.data.get('organization') or request.query_params.get('organization')

    if request.auth == SUPER_ADMIN_MARKER:
        if not org_id:
            raise ValidationError({'organization': 'organization is required for super admin writes'})
        serializer.save(organization_id=org_id)
        return
    if request.auth == ECOMMERCE_MARKER:
        raise ValidationError({'organization': 'Create or join an organization to access this feature'})

    serializer.save(organization=request.auth)


def wants_cookie_transport(request):
    """
    Return True when the client explicitly requests cookie-based auth.

    Browser clients set `X-Auth-Transport: cookie` when they want the backend
    to store the token in an HttpOnly cookie instead of returning it in JSON.
    """
    return request.headers.get('X-Auth-Transport', '').lower() == 'cookie'


def auth_cookie_name():
    """Return the configured auth cookie name."""
    return getattr(settings, 'AUTH_COOKIE_NAME', 'mosaic_auth')


def set_auth_cookie(response, raw_token):
    """
    Attach the raw auth token as an HttpOnly browser cookie.

    This is the browser session transport used by the Next.js frontend. The
    raw token never reaches JavaScript when cookie transport is selected.
    """
    response.set_cookie(
        auth_cookie_name(),
        raw_token,
        httponly=getattr(settings, 'AUTH_COOKIE_HTTPONLY', True),
        secure=getattr(settings, 'AUTH_COOKIE_SECURE', True),
        samesite=getattr(settings, 'AUTH_COOKIE_SAMESITE', 'Lax'),
        path=getattr(settings, 'AUTH_COOKIE_PATH', '/'),
        max_age=getattr(settings, 'AUTH_COOKIE_MAX_AGE', None),
    )
    return response


def clear_auth_cookie(response):
    """Remove the browser auth cookie."""
    response.delete_cookie(
        auth_cookie_name(),
        path=getattr(settings, 'AUTH_COOKIE_PATH', '/'),
    )
    return response


def revoke_user_tokens(user):
    """Revoke API tokens tied to the supplied user."""
    account = getattr(user, 'account', None)
    if account is not None:
        ApiToken.objects.filter(user_account=account).update(is_active=False)
    super_token = getattr(user, 'super_admin_token', None)
    if super_token is not None:
        super_token.revoke_token()


def resolve_password_reset_user(identifier):
    """Return the auth user matching a reset identifier."""
    return User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier)).first()


def send_password_reset_email(user, reset_url):
    """Send the password reset link without exposing the token in API output."""
    if not user.email:
        logger.info('Password reset requested for user without email user_id=%s', user.pk)
        return
    try:
        send_mail(
            subject='Reset your Mosaic password',
            message=f'Use this link to reset your password: {reset_url}',
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception('Failed to send password reset email for user_id=%s', user.pk)


def csrf_failure_response(request):
    """
    Enforce CSRF only when cookie-based browser auth is being used.

    DRF API views are csrf-exempt by default, so the browser-cookie path needs
    explicit CSRF validation to preserve Django's built-in protection.
    """
    middleware = CsrfViewMiddleware(lambda req: None)
    django_request = getattr(request, '_request', request)
    return middleware.process_view(django_request, None, (), {})


def require_cookie_csrf(request):
    """Validate CSRF for requests that use browser cookie transport."""
    if not (wants_cookie_transport(request) or request.COOKIES.get(auth_cookie_name())):
        return None
    return csrf_failure_response(request)


class MerchantViewSet(viewsets.ModelViewSet):
    """
    Manage merchants within the authenticated organization.

    Merchants are organization-scoped purchase counterparties, so this viewset
    only exposes the records visible through the current auth context.
    """

    serializer_class = MerchantSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'party_management'

    def get_queryset(self):
        """Return merchant records visible to the current auth context."""
        return org_filter(Merchant.objects.all(), self.request).order_by('id')

    def perform_create(self, serializer):
        """Persist a merchant inside the request's organization scope."""
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Persist merchant changes inside the request's organization scope."""
        save_for_request_organization(serializer, self.request)


class CustomerViewSet(viewsets.ModelViewSet):
    """
    Manage customers within the authenticated organization.

    Customers are used in sales and credit workflows, so the queryset is
    always constrained to the current tenant unless the caller is super admin.
    """

    serializer_class = CustomerSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'party_management'

    def get_queryset(self):
        """Return customer records visible to the current auth context."""
        return org_filter(Customer.objects.all(), self.request).order_by('id')

    def perform_create(self, serializer):
        """Persist a customer inside the request's organization scope."""
        save_for_request_organization(serializer, self.request)

    def perform_update(self, serializer):
        """Persist customer changes inside the request's organization scope."""
        save_for_request_organization(serializer, self.request)


class UserAccountViewSet(viewsets.ModelViewSet):
    """
    Manage users within the authenticated organization.

    This viewset is the organization admin surface for creating staff accounts,
    rotating tokens, and enabling or disabling application access.
    """

    serializer_class = UserAccountSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'user_management'

    def get_queryset(self):
        """Return user accounts visible to the current auth context."""
        return org_filter(UserAccount.objects.all(), self.request).order_by('id')

    def get_serializer_class(self):
        """Switch serializers based on the action being performed."""
        if self.action == 'create':
            return UserAccountCreateSerializer
        if self.action == 'token':
            return UserAccountWithTokenSerializer
        return UserAccountSerializer

    def create(self, request, *args, **kwargs):
        """
        Create a user account and return the issued token in the response.

        The account itself is created in the serializer, while the token is
        returned here so the frontend can immediately hand the new user a
        working session.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_account = serializer.save()

        output_serializer = UserAccountWithTokenSerializer(user_account)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def token(self, request, pk=None):
        """Rotate the token for the selected user account."""
        user_account = self.get_object()
        token = ApiToken.objects.filter(user_account=user_account).first()
        if token:
            raw_token = token.rotate_token()
        else:
            _, raw_token = ApiToken.issue_token(user_account)

        return Response({'token': raw_token})

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """Disable the selected user account and revoke its token."""
        user_account = self.get_object()
        user_account.is_active = False
        user_account.save(update_fields=['is_active'])

        ApiToken.objects.filter(user_account=user_account).update(is_active=False)

        return Response({'status': 'deactivated'})

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Re-enable the selected user account and its token."""
        user_account = self.get_object()
        user_account.is_active = True
        user_account.save(update_fields=['is_active'])

        token = ApiToken.objects.filter(user_account=user_account).first()
        if token:
            raw_token = token.rotate_token()
        else:
            _, raw_token = ApiToken.issue_token(user_account)

        return Response({'status': 'activated', 'token': raw_token})

    @action(detail=False, methods=['get'])
    @extend_schema(
        responses=inline_serializer(
            name='PermissionMatrixResponse',
            fields={
                'role': serializers.CharField(),
                'available_roles': serializers.ListField(child=serializers.CharField()),
                'scopes': serializers.DictField(),
            },
        )
    )
    def permissions(self, request):
        """Return the role and permission matrix used by the API."""
        role = 'SuperAdmin' if request.auth == SUPER_ADMIN_MARKER else get_request_role(request)
        scopes = {
            scope: {
                action: sorted(list(allowed))
                for action, allowed in policy.items()
            }
            for scope, policy in ROLE_POLICIES.items()
        }
        return Response({
            'role': role,
            'available_roles': [choice[0] for choice in UserAccount.ROLE_CHOICES],
            'scopes': scopes,
        })


class PublicOnboardingView(APIView):
    """
    Public onboarding endpoint.

    Creates the first organization owner and returns a token immediately.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        request=OrganizationOnboardingSerializer,
        responses=OrganizationOnboardingResponseSerializer,
    )
    def post(self, request):
        """
        Create the first organization owner and issue the bootstrap token.

        This is the bootstrap entry point for brand new tenants. It supports
        both bearer-token responses and cookie-based browser sessions.
        """
        csrf_response = require_cookie_csrf(request)
        if csrf_response is not None:
            return csrf_response

        serializer = OrganizationOnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_account = serializer.save()
        response_serializer = OrganizationOnboardingResponseSerializer(user_account)
        response_data = dict(response_serializer.data)

        if wants_cookie_transport(request):
            raw_token = response_data.pop('token', None)
            response = Response(response_data, status=status.HTTP_201_CREATED)
            return set_auth_cookie(response, raw_token)

        return Response(response_data, status=status.HTTP_201_CREATED)


class SignupView(APIView):
    """
    Create an ecommerce-only account with unified auth.

    This is the public sign-up surface for shoppers and B2B users before they
    create or join an organization.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        request=EcommerceSignupSerializer,
        responses=AuthSessionSerializer,
    )
    def post(self, request):
        """Create an account that can later be upgraded into an org owner."""
        csrf_response = require_cookie_csrf(request)
        if csrf_response is not None:
            return csrf_response

        serializer = EcommerceSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.save()
        response_serializer = AuthSessionSerializer(account)
        response_data = dict(response_serializer.data)

        if wants_cookie_transport(request):
            raw_token = response_data.pop('token', None)
            response = Response(response_data, status=status.HTTP_201_CREATED)
            return set_auth_cookie(response, raw_token)

        return Response(response_data, status=status.HTTP_201_CREATED)


@method_decorator(ensure_csrf_cookie, name='dispatch')
class CsrfBootstrapView(APIView):
    """
    Expose a CSRF token and set the CSRF cookie for browser clients.

    Next.js calls this endpoint before any cookie-authenticated mutating
    request so Django's CSRF middleware has a token to validate.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        responses=inline_serializer(
            name='CsrfBootstrapResponse',
            fields={'csrfToken': serializers.CharField()},
        )
    )
    def get(self, request):
        """Return a CSRF token that Next.js can forward in mutating requests."""
        return Response({'csrfToken': get_token(request)})


class LoginView(APIView):
    """
    Authenticate an existing user and return a fresh token.

    The same endpoint powers browser-cookie and bearer-token flows so the
    frontend can choose the transport layer without changing the credential
    semantics.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginIPRateThrottle, LoginIdentifierRateThrottle]

    @extend_schema(
        request=LoginSerializer,
        responses=AuthSessionSerializer,
    )
    def post(self, request):
        """Validate credentials and return organization, account, and token data."""
        csrf_response = require_cookie_csrf(request)
        if csrf_response is not None:
            return csrf_response

        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.validated_data['account']

        token = ApiToken.objects.filter(user_account=account).first()
        if token:
            raw_token = token.rotate_token()
        else:
            _, raw_token = ApiToken.issue_token(account)

        account._raw_api_token = raw_token
        response_serializer = AuthSessionSerializer(account)
        response_data = dict(response_serializer.data)

        if wants_cookie_transport(request):
            response_data.pop('token', None)
            response = Response(response_data, status=status.HTTP_200_OK)
            return set_auth_cookie(response, raw_token)

        return Response(response_data, status=status.HTTP_200_OK)


class MeView(APIView):
    """
    Return the authenticated principal's unified account profile.

    The client uses this endpoint to determine whether the current session is
    ecommerce-only, org-backed, or super-admin scoped.
    """

    permission_classes = [ApiKeyPermission]

    @extend_schema(responses=AccountMeSerializer)
    def get(self, request):
        """Return authenticated account and optional organization data."""
        if request.auth == SUPER_ADMIN_MARKER:
            return Response({
                'user': {
                    'id': request.user.id,
                    'username': request.user.username,
                    'email': request.user.email,
                    'first_name': request.user.first_name,
                    'last_name': request.user.last_name,
                },
                'account': {
                    'id': None,
                    'account_type': 'super_admin',
                    'organization_id': None,
                    'role': None,
                    'phone': '',
                    'is_active': request.user.is_active,
                },
                'organization': None,
            })

        account = getattr(request.user, 'account', None)
        if account is None:
            raise ValidationError({'account': 'User account not found.'})

        return Response(AccountMeSerializer(account).data)


class PasswordResetRequestView(APIView):
    """Issue a password-reset token pair for a username or email identifier."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetRequestIPRateThrottle, PasswordResetRequestIdentifierRateThrottle]

    @extend_schema(
        request=PasswordResetRequestSerializer,
        responses=PasswordResetRequestResponseSerializer,
    )
    def post(self, request):
        csrf_response = require_cookie_csrf(request)
        if csrf_response is not None:
            return csrf_response

        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        identifier = serializer.validated_data['identifier'].strip()
        user = resolve_password_reset_user(identifier)
        if user is None:
            return Response({'status': 'ok'})

        frontend_origin = getattr(settings, 'PASSWORD_RESET_FRONTEND_URL', '')
        if frontend_origin and user.email:
            token_generator = PasswordResetTokenGenerator()
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = token_generator.make_token(user)
            reset_url = f"{frontend_origin.rstrip('/')}/reset-password?uid={uid}&token={token}"
            send_password_reset_email(user, reset_url)
        else:
            logger.info('Password reset email skipped for user_id=%s', user.pk)

        return Response({'status': 'ok'})


class PasswordResetConfirmView(APIView):
    """Set a new password using a previously issued reset token."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetConfirmIPRateThrottle, PasswordResetConfirmIdentifierRateThrottle]

    @extend_schema(
        request=PasswordResetConfirmSerializer,
        responses=PasswordResetConfirmResponseSerializer,
    )
    def post(self, request):
        csrf_response = require_cookie_csrf(request)
        if csrf_response is not None:
            return csrf_response

        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            user_id = force_str(urlsafe_base64_decode(serializer.validated_data['uid']))
            user = User.objects.get(pk=user_id)
        except (TypeError, ValueError, OverflowError, DjangoUnicodeDecodeError, binascii.Error, User.DoesNotExist) as exc:
            raise ValidationError({'uid': 'Invalid password reset uid.'}) from exc

        token = serializer.validated_data['token']
        token_generator = PasswordResetTokenGenerator()
        if not token_generator.check_token(user, token):
            raise ValidationError({'token': 'Invalid or expired password reset token.'})

        user.set_password(serializer.validated_data['new_password'])
        user.save(update_fields=['password'])
        revoke_user_tokens(user)
        return Response({'status': 'password_updated'})


class CreateOrganizationView(APIView):
    """
    Upgrade an ecommerce-only user into an organization owner.

    This endpoint is the bridge from public ecommerce identity to a tenant
    owner profile without creating a second login.
    """

    permission_classes = [ApiKeyPermission]

    @transaction.atomic
    @extend_schema(
        request=OrganizationCreateSerializer,
        responses=inline_serializer(
            name='CreateOrganizationResponse',
            fields={
                'organization': serializers.DictField(),
                'account': serializers.DictField(),
            },
        ),
    )
    def post(self, request):
        """Create an organization for the current ecommerce account."""
        csrf_response = require_cookie_csrf(request)
        if csrf_response is not None:
            return csrf_response

        serializer = OrganizationCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        account = serializer.save()
        organization = account.organization
        return Response({
            'organization': {
                'id': organization.id,
                'name': organization.name,
                'trade_name': organization.trade_name,
                'gstin': organization.gstin,
                'address': organization.address,
                'phone': organization.phone,
                'email': organization.email,
                'is_active': organization.is_active,
                'created_at': organization.created_at,
                'updated_at': organization.updated_at,
            },
            'account': {
                'id': account.id,
                'account_type': account.account_type,
                'organization_id': account.organization_id,
                'role': account.role,
                'phone': account.phone,
                'is_active': account.is_active,
            },
        }, status=status.HTTP_201_CREATED)


class RefreshTokenView(APIView):
    """
    Rotate the current bearer token for the authenticated principal.

    Refresh applies to both organization accounts and super admin tokens. The
    response format depends on whether the caller is using cookie transport.
    """
    permission_classes = [ApiKeyPermission]

    @transaction.atomic
    @extend_schema(
        request=None,
        responses=inline_serializer(
            name='RefreshTokenResponse',
            fields={
                'token': serializers.CharField(required=False),
                'status': serializers.CharField(required=False),
            },
        )
    )
    def post(self, request):
        """Return a newly issued token and revoke the old token value."""
        csrf_response = require_cookie_csrf(request)
        if csrf_response is not None:
            return csrf_response

        if request.auth == SUPER_ADMIN_MARKER:
            token = getattr(request.user, 'super_admin_token', None)
            if token is None:
                raise ValidationError({'token': 'Super admin token not found.'})
            raw_token = token.rotate_token()
            request.user._raw_api_token = raw_token
            if wants_cookie_transport(request) or request.COOKIES.get(auth_cookie_name()):
                response = Response({'status': 'refreshed'}, status=status.HTTP_200_OK)
                return set_auth_cookie(response, raw_token)
            return Response({'token': raw_token})

        account = getattr(request.user, 'account', None)
        if account is None:
            raise ValidationError({'account': 'User account not found.'})

        token = ApiToken.objects.filter(user_account=account).first()
        if token:
            raw_token = token.rotate_token()
        else:
            _, raw_token = ApiToken.issue_token(account)

        account._raw_api_token = raw_token
        if wants_cookie_transport(request) or request.COOKIES.get(auth_cookie_name()):
            response = Response({'status': 'refreshed'}, status=status.HTTP_200_OK)
            return set_auth_cookie(response, raw_token)
        return Response({'token': raw_token})


class LogoutView(APIView):
    """
    Revoke the current bearer token for the authenticated principal.

    Logout revokes the underlying token record and clears the browser cookie if
    cookie transport is in use.
    """
    permission_classes = [ApiKeyPermission]

    @transaction.atomic
    @extend_schema(request=None, responses={status.HTTP_204_NO_CONTENT: None})
    def post(self, request):
        """Disable the active token so the client must log in again."""
        csrf_response = require_cookie_csrf(request)
        if csrf_response is not None:
            return csrf_response

        if request.auth == SUPER_ADMIN_MARKER:
            token = getattr(request.user, 'super_admin_token', None)
            if token is None:
                raise ValidationError({'token': 'Super admin token not found.'})
            token.revoke_token()
            return clear_auth_cookie(Response(status=status.HTTP_204_NO_CONTENT))

        account = getattr(request.user, 'account', None)
        if account is None:
            raise ValidationError({'account': 'User account not found.'})

        token = ApiToken.objects.filter(user_account=account).first()
        if token is None:
            raise ValidationError({'token': 'API token not found.'})

        token.revoke_token()
        return clear_auth_cookie(Response(status=status.HTTP_204_NO_CONTENT))
