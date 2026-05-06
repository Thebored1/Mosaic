from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from rest_framework.test import APIClient

from account.models import Organization, UserAccount
from configuration.models import ApiToken, SuperAdminToken


class PublicAuthFlowTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.cookie_client = APIClient(enforce_csrf_checks=True)
        self.signup_payload = {
            'username': 'shopper-one',
            'email': 'shopper@example.com',
            'password': 'StrongPass123!',
            'first_name': 'Asha',
            'last_name': 'Patel',
            'phone': '+919876543210',
        }
        self.organization_payload = {
            'name': 'Acme Retail',
            'trade_name': 'Acme',
            'gstin': '27AAAAA0000A1Z5',
            'address': '42 Market Street',
            'phone': '+911234567890',
            'email': 'billing@acme.example',
        }
        self.onboard_payload = {
            'organization_name': 'Bootstrap Retail',
            'organization_trade_name': 'Bootstrap',
            'organization_gstin': '29AAAAA0000A1Z5',
            'organization_address': '84 Commerce Street',
            'organization_phone': '+911111111111',
            'organization_email': 'hello@bootstrap.example',
            'owner_username': 'bootstrap-owner',
            'owner_email': 'owner@bootstrap.example',
            'owner_password': 'StrongPass123!',
            'owner_first_name': 'Ira',
            'owner_last_name': 'Shah',
            'owner_phone': '+919999999999',
        }

    def _bootstrap_csrf(self, client=None):
        active_client = client or self.cookie_client
        response = active_client.get('/v1/account/csrf/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(settings.CSRF_COOKIE_NAME, response.cookies)
        return response.cookies[settings.CSRF_COOKIE_NAME].value

    def test_signup_creates_ecommerce_account_and_token(self):
        response = self.client.post('/v1/account/signup/', self.signup_payload, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(UserAccount.objects.count(), 1)
        self.assertEqual(ApiToken.objects.count(), 1)
        self.assertIsNone(response.data['organization'])
        self.assertEqual(response.data['account']['account_type'], 'ecommerce')
        self.assertIsNone(response.data['account']['organization_id'])
        self.assertTrue(response.data['token'])

    def test_cookie_signup_sets_auth_cookie_and_hides_token(self):
        csrf_token = self._bootstrap_csrf(self.cookie_client)
        response = self.cookie_client.post(
            '/v1/account/signup/',
            self.signup_payload,
            format='json',
            HTTP_X_CSRFTOKEN=csrf_token,
            HTTP_X_AUTH_TRANSPORT='cookie',
        )

        self.assertEqual(response.status_code, 201)
        self.assertNotIn('token', response.data)
        self.assertIn(settings.AUTH_COOKIE_NAME, response.cookies)
        self.assertEqual(response.data['account']['account_type'], 'ecommerce')

    def test_ecommerce_user_is_blocked_from_org_scoped_api(self):
        signup = self.client.post('/v1/account/signup/', self.signup_payload, format='json')
        token = signup.data['token']

        response = self.client.get(
            '/v1/account/users/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn('Create or join an organization', str(response.data))

    def test_ecommerce_user_can_create_organization_and_become_owner(self):
        signup = self.client.post('/v1/account/signup/', self.signup_payload, format='json')
        token = signup.data['token']

        response = self.client.post(
            '/v1/account/create-organization/',
            self.organization_payload,
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Organization.objects.count(), 1)
        account = UserAccount.objects.get(user__username='shopper-one')
        self.assertEqual(account.account_type, 'org_user')
        self.assertEqual(account.role, 'Owner')
        self.assertIsNotNone(account.organization)

        me_response = self.client.get(
            '/v1/account/me/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.data['account']['account_type'], 'org_user')
        self.assertEqual(me_response.data['organization']['name'], 'Acme Retail')

    def test_public_onboarding_still_creates_owner_and_token(self):
        response = self.client.post('/v1/account/onboard/', self.onboard_payload, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(UserAccount.objects.count(), 1)
        self.assertEqual(ApiToken.objects.count(), 1)
        self.assertEqual(response.data['owner']['account_type'], 'org_user')
        self.assertEqual(response.data['owner']['role'], 'Owner')
        self.assertTrue(response.data['token'])

    def test_login_returns_ecommerce_account_without_organization(self):
        self.client.post('/v1/account/signup/', self.signup_payload, format='json')

        response = self.client.post('/v1/account/login/', {
            'username': 'shopper-one',
            'password': 'StrongPass123!',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['account']['account_type'], 'ecommerce')
        self.assertIsNone(response.data['organization'])
        self.assertTrue(response.data['token'])

    def test_cookie_login_and_refresh_use_auth_cookie(self):
        csrf_token = self._bootstrap_csrf(self.cookie_client)
        self.cookie_client.post(
            '/v1/account/signup/',
            self.signup_payload,
            format='json',
            HTTP_X_CSRFTOKEN=csrf_token,
            HTTP_X_AUTH_TRANSPORT='cookie',
        )

        login_response = self.cookie_client.post(
            '/v1/account/login/',
            {'username': 'shopper-one', 'password': 'StrongPass123!'},
            format='json',
            HTTP_X_CSRFTOKEN=csrf_token,
            HTTP_X_AUTH_TRANSPORT='cookie',
        )

        self.assertEqual(login_response.status_code, 200)
        self.assertNotIn('token', login_response.data)
        self.assertIn(settings.AUTH_COOKIE_NAME, login_response.cookies)

        refresh_csrf = self.cookie_client.cookies[settings.CSRF_COOKIE_NAME].value
        refresh_response = self.cookie_client.post(
            '/v1/account/refresh/',
            {},
            format='json',
            HTTP_X_CSRFTOKEN=refresh_csrf,
        )

        self.assertEqual(refresh_response.status_code, 200)
        self.assertNotIn('token', refresh_response.data)
        self.assertIn(settings.AUTH_COOKIE_NAME, refresh_response.cookies)

    def test_cookie_logout_clears_auth_cookie(self):
        csrf_token = self._bootstrap_csrf(self.cookie_client)
        self.cookie_client.post(
            '/v1/account/signup/',
            self.signup_payload,
            format='json',
            HTTP_X_CSRFTOKEN=csrf_token,
            HTTP_X_AUTH_TRANSPORT='cookie',
        )

        logout_csrf = self.cookie_client.cookies[settings.CSRF_COOKIE_NAME].value
        response = self.cookie_client.post(
            '/v1/account/logout/',
            {},
            format='json',
            HTTP_X_CSRFTOKEN=logout_csrf,
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn(settings.AUTH_COOKIE_NAME, response.cookies)
        self.assertEqual(response.cookies[settings.AUTH_COOKIE_NAME]['max-age'], 0)

    def test_cookie_auth_requires_csrf(self):
        self._bootstrap_csrf(self.cookie_client)
        response = self.cookie_client.post(
            '/v1/account/signup/',
            self.signup_payload,
            format='json',
            HTTP_X_AUTH_TRANSPORT='cookie',
        )

        self.assertEqual(response.status_code, 403)

    def test_refresh_rotates_token(self):
        signup_response = self.client.post('/v1/account/signup/', self.signup_payload, format='json')
        old_token = signup_response.data['token']

        refresh_response = self.client.post(
            '/v1/account/refresh/',
            {},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {old_token}',
        )

        self.assertEqual(refresh_response.status_code, 200)
        self.assertTrue(refresh_response.data['token'])
        self.assertNotEqual(refresh_response.data['token'], old_token)

        old_response = self.client.get(
            '/v1/account/me/',
            HTTP_AUTHORIZATION=f'Bearer {old_token}',
        )
        self.assertEqual(old_response.status_code, 401)

    def test_password_reset_confirm_rejects_invalid_uid(self):
        csrf_token = self._bootstrap_csrf(self.cookie_client)
        self.client.post('/v1/account/signup/', self.signup_payload, format='json')

        reset_response = self.cookie_client.post(
            '/v1/account/password-reset/request/',
            {'identifier': self.signup_payload['email']},
            format='json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(reset_response.status_code, 200)

        confirm_response = self.cookie_client.post(
            '/v1/account/password-reset/confirm/',
            {
                'uid': 'not-a-valid-uid',
                'token': 'not-a-valid-token',
                'new_password': 'NewStrongPass123!',
            },
            format='json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(confirm_response.status_code, 400)
        self.assertIn('uid', confirm_response.data)

    def test_logout_revokes_token(self):
        signup_response = self.client.post('/v1/account/signup/', self.signup_payload, format='json')
        token = signup_response.data['token']

        logout_response = self.client.post(
            '/v1/account/logout/',
            {},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(logout_response.status_code, 204)

        revoked_response = self.client.get(
            '/v1/account/me/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(revoked_response.status_code, 401)

    @override_settings(
        PASSWORD_RESET_FRONTEND_URL='https://frontend.example',
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    )
    def test_password_reset_request_and_confirm_flow(self):
        signup_response = self.client.post('/v1/account/signup/', self.signup_payload, format='json')
        old_token = signup_response.data['token']

        request_response = self.client.post(
            '/v1/account/password-reset/request/',
            {'identifier': 'shopper-one'},
            format='json',
        )
        self.assertEqual(request_response.status_code, 200)
        self.assertEqual(request_response.data, {'status': 'ok'})
        self.assertEqual(len(mail.outbox), 1)
        reset_url = mail.outbox[0].body.rsplit(' ', 1)[-1]
        query = parse_qs(urlparse(reset_url).query)
        uid = query['uid'][0]
        token = query['token'][0]

        confirm_response = self.client.post(
            '/v1/account/password-reset/confirm/',
            {
                'uid': uid,
                'token': token,
                'new_password': 'NewStrongPass123!',
            },
            format='json',
        )
        self.assertEqual(confirm_response.status_code, 200)
        self.assertEqual(confirm_response.data['status'], 'password_updated')

        revoked_response = self.client.get(
            '/v1/account/me/',
            HTTP_AUTHORIZATION=f'Bearer {old_token}',
        )
        self.assertEqual(revoked_response.status_code, 401)

        login_response = self.client.post('/v1/account/login/', {
            'username': 'shopper-one',
            'password': 'NewStrongPass123!',
        }, format='json')
        self.assertEqual(login_response.status_code, 200)
        self.assertTrue(login_response.data['token'])

    def test_super_admin_logout_revokes_token(self):
        super_user = User.objects.create_superuser(
            username='root',
            email='root@example.com',
            password='RootPass123!'
        )
        _, raw_token = SuperAdminToken.issue_token(super_user)

        response = self.client.post(
            '/v1/account/logout/',
            {},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {raw_token}',
        )

        self.assertEqual(response.status_code, 204)

        revoked_response = self.client.get(
            '/v1/account/me/',
            HTTP_AUTHORIZATION=f'Bearer {raw_token}',
        )
        self.assertEqual(revoked_response.status_code, 401)

    def test_user_creation_without_auth_is_rejected(self):
        payload = {
            'username': 'other-user',
            'email': 'other@example.com',
            'password': 'StrongPass123!',
            'role': 'Staff',
            'phone': '+911111111111',
        }

        response = self.client.post('/v1/account/users/', payload, format='json')

        self.assertEqual(response.status_code, 401)

    def test_expired_token_is_rejected(self):
        signup_response = self.client.post('/v1/account/signup/', self.signup_payload, format='json')
        token = signup_response.data['token']
        ApiToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        response = self.client.get(
            '/v1/account/me/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        self.assertEqual(response.status_code, 401)

    def test_bearer_header_overrides_cookie_token(self):
        first_signup = self.client.post('/v1/account/signup/', self.signup_payload, format='json')
        second_payload = {
            **self.signup_payload,
            'username': 'shopper-two',
            'email': 'shopper-two@example.com',
        }
        second_signup = self.client.post('/v1/account/signup/', second_payload, format='json')

        self.client.cookies[settings.AUTH_COOKIE_NAME] = first_signup.data['token']
        response = self.client.get(
            '/v1/account/me/',
            HTTP_AUTHORIZATION=f'Bearer {second_signup.data["token"]}',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['username'], 'shopper-two')

    def test_activate_rotates_token_instead_of_reusing_old_token(self):
        org_response = self.client.post('/v1/account/onboard/', self.onboard_payload, format='json')
        owner_token = org_response.data['token']
        create_response = self.client.post(
            '/v1/account/users/',
            {
                'username': 'staff-user',
                'email': 'staff@example.com',
                'password': 'StrongPass123!',
                'role': 'Staff',
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {owner_token}',
        )
        old_staff_token = create_response.data['token']
        staff_id = create_response.data['id']

        self.client.post(
            f'/v1/account/users/{staff_id}/deactivate/',
            {},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {owner_token}',
        )
        activate_response = self.client.post(
            f'/v1/account/users/{staff_id}/activate/',
            {},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {owner_token}',
        )

        self.assertEqual(activate_response.status_code, 200)
        self.assertNotEqual(activate_response.data['token'], old_staff_token)
