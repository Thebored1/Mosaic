from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from account.models import Organization, UserAccount
from configuration.models import ApiToken, TenantSettings
from mosaic_backend.celery import app as celery_app


class ConfigurationSurfaceTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Config Org')
        user = User.objects.create_user(username='config-owner', password='password123')
        self.account = UserAccount.objects.create(
            user=user,
            organization=self.organization,
            account_type='org_user',
            role='Owner',
        )
        _, self.token = ApiToken.issue_token(self.account)

    def test_health_endpoint_reports_ok(self):
        response = self.client.get('/v1/health/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'ok')
        self.assertEqual(response.data['database'], 'ok')
        self.assertIn('celery', response.data)
        self.assertIn('app', response.data)

    def test_openapi_schema_and_swagger_docs_are_exposed(self):
        schema_response = self.client.get('/v1/schema/?format=json')
        self.assertEqual(schema_response.status_code, status.HTTP_200_OK)
        self.assertEqual(schema_response.data['info']['title'], 'Mosaic Backend API')
        self.assertIn('/v1/health/', schema_response.data['paths'])
        schemas = schema_response.data.get('components', {}).get('schemas', {})
        self.assertIn('ConfigurationState', schemas)
        self.assertIn('SaleState', schemas)
        self.assertNotIn('State', schemas)
        self.assertIn('QuotationStatusEnum', schemas)
        self.assertIn('DocumentLifecycleStatusEnum', schemas)
        self.assertIn('UserRoleEnum', schemas)
        self.assertIn('ReceiptPaymentModeEnum', schemas)
        self.assertIn('TenantPrintTemplateEnum', schemas)
        self.assertNotIn('Status111Enum', schemas)
        self.assertNotIn('PaymentMode89bEnum', schemas)
        self.assertNotIn('Role84aEnum', schemas)

        docs_response = self.client.get('/v1/docs/')
        self.assertEqual(docs_response.status_code, status.HTTP_200_OK)

    def test_celery_app_is_configured(self):
        self.assertEqual(celery_app.main, 'mosaic_backend')

    def test_tenant_settings_can_be_created_and_updated(self):
        response = self.client.post(
            '/v1/configuration/tenant-settings/',
            {
                'email_notifications_enabled': True,
                'sms_notifications_enabled': True,
                'invoice_print_template': 'compact',
                'receipt_print_template': 'thermal',
                'delivery_note_print_template': 'standard',
                'fiscal_year_start_month': 4,
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(TenantSettings.objects.filter(organization=self.organization).exists())

        patch_response = self.client.post(
            '/v1/configuration/tenant-settings/',
            {'sms_notifications_enabled': False},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )
        self.assertIn(patch_response.status_code, {status.HTTP_200_OK, status.HTTP_201_CREATED})
        self.assertFalse(patch_response.data['sms_notifications_enabled'])

    def test_permission_matrix_is_exposed(self):
        response = self.client.get(
            '/v1/account/users/permissions/',
            HTTP_AUTHORIZATION=f'Bearer {self.token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('available_roles', response.data)
        self.assertIn('scopes', response.data)
