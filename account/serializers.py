"""
Account serializers.

The serializers in this module shape the application identity and tenant
workflow:

1. Merchant and Customer serializers expose organization-scoped masters.
2. User account serializers manage organization users and token handoff.
3. Onboarding serializers create the first organization and owner together.
4. Auth/session serializers describe the browser and bearer-token responses.
5. Ecommerce signup and organization upgrade serializers support the unified
   account model introduced for B2C, B2B, and org-backed users.
"""

from django.db import transaction
from django.contrib.auth import authenticate
from rest_framework import serializers
from django.contrib.auth.models import User
from .models import Merchant, Customer, UserAccount, Organization
from configuration.models import ApiToken
from configuration.authentication import SUPER_ADMIN_MARKER, ECOMMERCE_MARKER


class MerchantSerializer(serializers.ModelSerializer):
    """
    Serialize merchants for organization-scoped CRUD operations.

    Merchant payloads are intentionally compact because they are typically used
    in dropdowns, purchase forms, and summary lists.
    """

    class Meta:
        model = Merchant
        fields = [
            'id', 'name', 'trade_name', 'gstin', 'address',
            'phone', 'email', 'logo', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CustomerSerializer(serializers.ModelSerializer):
    """
    Serialize customers for organization-scoped CRUD operations.

    Customers carry the details required for invoicing, credit tracking, and
    checkout flows, but the serializer still keeps the contract simple.
    """

    class Meta:
        model = Customer
        fields = [
            'id', 'name', 'role', 'gstin', 'address',
            'phone', 'email', 'credit_limit', 'opening_balance',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class UserAccountSerializer(serializers.ModelSerializer):
    """
    Read-only view of an application user account.

    This serializer is used when listing users inside an organization and also
    when returning a compact account summary to the client.
    """

    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    organization_id = serializers.IntegerField(source='organization.id', read_only=True)

    class Meta:
        model = UserAccount
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name',
            'account_type', 'organization_id', 'role', 'phone',
            'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class UserAccountCreateSerializer(serializers.ModelSerializer):
    """
    Create a tenant-scoped user and issue an API token for that account.

    Organization owners and admins use this serializer to provision staff
    accounts. The serializer intentionally blocks ecommerce-only principals from
    creating new organization users.
    """

    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False)
    password = serializers.CharField(write_only=True, min_length=8)
    role = serializers.ChoiceField(choices=UserAccount.ROLE_CHOICES, default='Staff')
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    organization = serializers.PrimaryKeyRelatedField(
        queryset=Organization.objects.all(), 
        required=False
    )

    class Meta:
        model = UserAccount
        fields = ['username', 'email', 'password', 'role', 'phone', 'organization']

    def create(self, validated_data):
        """
        Create the Django user, attach a UserAccount, and issue an API token.

        The organization is derived from the authenticated principal unless the
        request comes from a super admin, in which case the target organization
        must be supplied explicitly.
        """
        username = validated_data.pop('username')
        email = validated_data.pop('email', '')
        password = validated_data.pop('password')
        role = validated_data.pop('role', 'Staff')
        phone = validated_data.pop('phone', '')
        provided_org = validated_data.pop('organization', None)

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password
        )

        auth = self.context['request'].auth
        
        if auth == SUPER_ADMIN_MARKER:
            if not provided_org:
                raise serializers.ValidationError(
                    "Super admin must provide an organization when creating a user."
                )
            organization = provided_org
        elif auth == ECOMMERCE_MARKER:
            raise serializers.ValidationError(
                "Create or join an organization before creating organization users."
            )
        elif hasattr(auth, 'pk'):
            organization = auth
        else:
            raise serializers.ValidationError(
                "User must be authenticated with a valid organization token."
            )

        user_account = UserAccount.objects.create(
            user=user,
            organization=organization,
            account_type='org_user',
            role=role,
            phone=phone
        )

        _, raw_token = ApiToken.issue_token(user_account)
        user_account._raw_api_token = raw_token

        return user_account


class UserAccountWithTokenSerializer(serializers.ModelSerializer):
    """
    Return a created user account together with the freshly issued token.

    This is only used immediately after provisioning a new org user. The token
    is included so the frontend can authenticate the new account without a
    separate login step.
    """

    username = serializers.CharField(source='user.username')
    email = serializers.EmailField(source='user.email')
    token = serializers.SerializerMethodField()

    class Meta:
        model = UserAccount
        fields = ['id', 'username', 'email', 'role', 'phone', 'is_active', 'token']

    def get_token(self, obj):
        return getattr(obj, '_raw_api_token', None)


class OrganizationOnboardingSerializer(serializers.Serializer):
    """
    Create the first organization owner and issue the initial API token.

    This is the bootstrap flow for a brand new tenant. It creates the
    organization, the owner auth user, the corresponding UserAccount, and the
    first API token in a single transaction.
    """

    organization_name = serializers.CharField(max_length=200)
    organization_trade_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    organization_gstin = serializers.CharField(max_length=15, required=False, allow_blank=True)
    organization_address = serializers.CharField(required=False, allow_blank=True)
    organization_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    organization_email = serializers.EmailField(required=False, allow_blank=True)

    owner_username = serializers.CharField(max_length=150)
    owner_email = serializers.EmailField(required=False, allow_blank=True)
    owner_password = serializers.CharField(write_only=True, min_length=8)
    owner_first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    owner_last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    owner_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_owner_username(self, value):
        """Reject duplicate usernames before entering the transaction."""
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError('A user with this username already exists.')
        return value

    def validate_owner_email(self, value):
        """Reject duplicate owner email addresses before onboarding."""
        if value and User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return value

    @transaction.atomic
    def create(self, validated_data):
        """
        Create the organization and its owner atomically.

        If any step fails, the transaction rolls back so a partial tenant is
        never left behind.
        """
        organization = Organization.objects.create(
            name=validated_data['organization_name'],
            trade_name=validated_data.get('organization_trade_name', ''),
            gstin=validated_data.get('organization_gstin', ''),
            address=validated_data.get('organization_address', ''),
            phone=validated_data.get('organization_phone', ''),
            email=validated_data.get('organization_email', ''),
        )

        user = User.objects.create_user(
            username=validated_data['owner_username'],
            email=validated_data.get('owner_email', ''),
            password=validated_data['owner_password'],
            first_name=validated_data.get('owner_first_name', ''),
            last_name=validated_data.get('owner_last_name', ''),
        )

        user_account = UserAccount.objects.create(
            user=user,
            organization=organization,
            account_type='org_user',
            role='Owner',
            phone=validated_data.get('owner_phone', ''),
        )

        _, raw_token = ApiToken.issue_token(user_account)
        user_account._raw_api_token = raw_token
        user_account._onboarding_organization = organization
        return user_account


class OrganizationOnboardingResponseSerializer(serializers.Serializer):
    """
    Shape the onboarding response with organization, owner, and token data.

    This response is tailored for the frontend bootstrap flow so the client
    can immediately render the newly created tenant and authenticate the owner.
    """

    organization = serializers.SerializerMethodField()
    owner = serializers.SerializerMethodField()
    token = serializers.SerializerMethodField()

    def get_organization(self, obj):
        """Return a stable organization payload for onboarding responses."""
        organization = getattr(obj, '_onboarding_organization', obj.organization)
        return {
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
        }

    def get_owner(self, obj):
        """Return the freshly created owner account payload."""
        user = obj.user
        return {
            'id': obj.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'account_type': obj.account_type,
            'role': obj.role,
            'phone': obj.phone,
            'is_active': obj.is_active,
            'created_at': obj.created_at,
            'updated_at': obj.updated_at,
        }

    def get_token(self, obj):
        """Return the raw token attached during onboarding."""
        return getattr(obj, '_raw_api_token', None)


class AuthSessionSerializer(serializers.Serializer):
    """
    Shape the login response with organization, account, and token data.

    This serializer backs both browser-cookie and bearer-token session flows.
    """

    organization = serializers.SerializerMethodField()
    account = serializers.SerializerMethodField()
    token = serializers.SerializerMethodField()

    def get_organization(self, obj):
        """Return organization details or None for ecommerce-only accounts."""
        organization = obj.organization
        if organization is None:
            return None
        return {
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
        }

    def get_account(self, obj):
        """Return the authenticated account profile."""
        user = obj.user
        return {
            'id': obj.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'account_type': obj.account_type,
            'organization_id': obj.organization_id,
            'role': obj.role,
            'phone': obj.phone,
            'is_active': obj.is_active,
            'created_at': obj.created_at,
            'updated_at': obj.updated_at,
        }

    def get_token(self, obj):
        """Return the raw token generated during login or refresh."""
        return getattr(obj, '_raw_api_token', None)


class LoginSerializer(serializers.Serializer):
    """
    Validate username/password credentials against an existing account.

    Login is intentionally a credential check only. The session/token issuance
    happens in the view so the same serializer can be reused for both cookie and
    bearer response styles.
    """

    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        """Authenticate the supplied credentials and ensure an account exists."""
        user = authenticate(
            username=attrs.get('username'),
            password=attrs.get('password')
        )
        if not user:
            raise serializers.ValidationError('Invalid username or password.')

        account = getattr(user, 'account', None)
        if account is None:
            raise serializers.ValidationError('This user does not have an account profile.')
        if not user.is_active or not account.is_active:
            raise serializers.ValidationError('This account is inactive.')

        attrs['user'] = user
        attrs['account'] = account
        return attrs


class EcommerceSignupSerializer(serializers.Serializer):
    """
    Create an ecommerce-only account with no organization membership.

    This is the public signup flow for shoppers and B2B buyers before they
    create or join an organization.
    """

    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=8)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_username(self, value):
        """Reject duplicate usernames before creating the account."""
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError('A user with this username already exists.')
        return value

    def validate_email(self, value):
        """Reject duplicate email addresses before creating the account."""
        if value and User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return value

    @transaction.atomic
    def create(self, validated_data):
        """
        Create an ecommerce-only user account and issue a token.

        The resulting UserAccount has no organization yet. The user can later
        upgrade the same account into an organization owner through the
        organization creation flow.
        """
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data.get('email', ''),
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
        )
        user_account = UserAccount.objects.create(
            user=user,
            organization=None,
            account_type='ecommerce',
            role='Staff',
            phone=validated_data.get('phone', ''),
        )
        _, raw_token = ApiToken.issue_token(user_account)
        user_account._raw_api_token = raw_token
        return user_account


class OrganizationCreateSerializer(serializers.Serializer):
    """
    Upgrade an ecommerce user into an organization owner.

    This serializer is the bridge between ecommerce-only identity and the
    back-office tenant model. It creates the organization and promotes the
    current account into the owner role without replacing the underlying user.
    """

    name = serializers.CharField(max_length=200)
    trade_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    gstin = serializers.CharField(max_length=15, required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)

    def validate(self, attrs):
        """Ensure the current authenticated account is eligible for upgrade."""
        request = self.context['request']
        if request.auth == SUPER_ADMIN_MARKER:
            raise serializers.ValidationError('Super admin accounts cannot be upgraded into organization owners.')

        account = getattr(request.user, 'account', None)
        if account is None or not account.is_active:
            raise serializers.ValidationError('Active user account required.')
        if account.organization_id is not None:
            raise serializers.ValidationError('This account already belongs to an organization.')
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        """
        Create the organization and promote the current account to owner.

        The same UserAccount is reused so the user's identity, credentials, and
        token history remain intact across the upgrade.
        """
        request = self.context['request']
        account = request.user.account
        organization = Organization.objects.create(**validated_data)
        account.organization = organization
        account.account_type = 'org_user'
        account.role = 'Owner'
        account.save(update_fields=['organization', 'account_type', 'role', 'updated_at'])
        account._created_organization = organization
        return account


class AccountMeSerializer(serializers.Serializer):
    """
    Return the authenticated principal's account shape.

    The /me endpoint uses this serializer to give the frontend a single source
    of truth for the user's identity, account type, and optional organization.
    """

    user = serializers.SerializerMethodField()
    account = serializers.SerializerMethodField()
    organization = serializers.SerializerMethodField()

    def get_user(self, obj):
        """Return the underlying Django auth user details."""
        return {
            'id': obj.id,
            'username': obj.user.username,
            'email': obj.user.email,
            'first_name': obj.user.first_name,
            'last_name': obj.user.last_name,
        }

    def get_account(self, obj):
        """Return the application account details."""
        return {
            'id': obj.id,
            'account_type': obj.account_type,
            'organization_id': obj.organization_id,
            'role': obj.role,
            'phone': obj.phone,
            'is_active': obj.is_active,
        }

    def get_organization(self, obj):
        """Return the organization payload when the account belongs to one."""
        if obj.organization is None:
            return None
        return {
            'id': obj.organization.id,
            'name': obj.organization.name,
            'trade_name': obj.organization.trade_name,
            'gstin': obj.organization.gstin,
            'is_active': obj.organization.is_active,
        }
