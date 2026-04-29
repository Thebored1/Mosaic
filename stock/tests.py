from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from account.models import Organization, UserAccount
from configuration.models import ApiToken
from stock.models import Category


class StockAuthTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Org One')
        self.other_organization = Organization.objects.create(name='Org Two')

        self.sales_user = User.objects.create_user(username='sales', password='password123')
        self.sales_account = UserAccount.objects.create(
            user=self.sales_user,
            organization=self.organization,
            account_type='org_user',
            role='Sales',
        )
        _, self.sales_token = ApiToken.issue_token(self.sales_account)

        self.warehouse_user = User.objects.create_user(username='warehouse', password='password123')
        self.warehouse_account = UserAccount.objects.create(
            user=self.warehouse_user,
            organization=self.organization,
            account_type='org_user',
            role='Warehouse',
        )
        _, self.warehouse_token = ApiToken.issue_token(self.warehouse_account)

        Category.objects.create(name='Org One Category', organization=self.organization)
        Category.objects.create(name='Org Two Category', organization=self.other_organization)

    def test_api_tokens_are_stored_hashed(self):
        token = self.sales_account.api_token

        self.assertEqual(token.token, '')
        self.assertTrue(token.token_hash)
        self.assertEqual(token.token_prefix, self.sales_token[:8])

    def test_missing_token_returns_401(self):
        response = self.client.get('/v1/api/categories/')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_lowercase_bearer_and_org_scoping_work(self):
        response = self.client.get(
            '/v1/api/categories/',
            HTTP_AUTHORIZATION=f'bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'Org One Category')

    def test_sales_role_cannot_create_category(self):
        response = self.client.post(
            '/v1/api/categories/',
            {'name': 'Blocked Category', 'description': '', 'is_active': True},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_warehouse_role_can_create_category_for_own_org(self):
        response = self.client.post(
            '/v1/api/categories/',
            {'name': 'Warehouse Category', 'description': '', 'is_active': True},
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.warehouse_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = Category.objects.get(name='Warehouse Category')
        self.assertEqual(created.organization, self.organization)

    def test_ecommerce_account_cannot_access_org_scoped_stock_api(self):
        ecommerce_user = User.objects.create_user(username='shopper', password='password123')
        ecommerce_account = UserAccount.objects.create(
            user=ecommerce_user,
            account_type='ecommerce',
            role='Staff',
        )
        _, ecommerce_token = ApiToken.issue_token(ecommerce_account)

        response = self.client.get(
            '/v1/api/categories/',
            HTTP_AUTHORIZATION=f'Bearer {ecommerce_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
