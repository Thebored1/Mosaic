from decimal import Decimal

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from account.models import Organization, UserAccount
from configuration.models import ApiToken, State, SuperAdminToken, Warehouse
from sale.models import Party
from stock.models import Category, Item


class SaleAuthTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name='Org One')
        self.other_organization = Organization.objects.create(name='Org Two')

        self.state = State.objects.create(name='Maharashtra', state_code='27')

        self.warehouse = Warehouse.objects.create(
            organization=self.organization,
            state=self.state,
            gstin='27AAAAA0000A1Z5',
            name='Main Warehouse',
            code='WH1',
            legal_name='Org One Legal',
            address='Address 1',
        )
        self.other_warehouse = Warehouse.objects.create(
            organization=self.other_organization,
            state=self.state,
            gstin='29AAAAA0000A1Z5',
            name='Other Warehouse',
            code='WH2',
            legal_name='Org Two Legal',
            address='Address 2',
        )

        self.sales_user = User.objects.create_user(username='sales', password='password123')
        self.sales_account = UserAccount.objects.create(
            user=self.sales_user,
            organization=self.organization,
            account_type='org_user',
            role='Sales',
        )
        _, self.sales_token = ApiToken.issue_token(self.sales_account)

        self.super_user = User.objects.create_superuser(
            username='superadmin',
            email='super@example.com',
            password='password123',
        )
        _, self.super_token = SuperAdminToken.issue_token(self.super_user)

        self.party = Party.objects.create(
            organization=self.organization,
            name='Org One Party',
            party_type='Customer',
        )
        Party.objects.create(
            organization=self.other_organization,
            name='Org Two Party',
            party_type='Customer',
        )

        category = Category.objects.create(name='Category', organization=self.organization)
        self.item = Item.objects.create(
            organization=self.organization,
            name='Item One',
            sku='ITEM-001',
            category=category,
            unit_price=Decimal('100.00'),
        )

    def test_party_list_is_scoped_to_authenticated_organization(self):
        response = self.client.get(
            '/v1/sale/parties/',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'Org One Party')

    def test_super_admin_sale_queries_require_explicit_organization(self):
        response = self.client.get(
            '/v1/sale/parties/',
            HTTP_AUTHORIZATION=f'Bearer {self.super_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

    def test_order_creation_rejects_other_org_business_location(self):
        response = self.client.post(
            '/v1/sale/orders/',
            {
                'party': self.party.id,
                'business_location': self.other_warehouse.id,
                'items': [
                    {'item': self.item.id, 'quantity': '1', 'rate': '100.00'}
                ],
                'discount_amount': '0.00',
                'discount_type': 'Fixed',
            },
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {self.sales_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('business_location', response.data)

    def test_ecommerce_account_cannot_access_sale_api(self):
        ecommerce_user = User.objects.create_user(username='shopper', password='password123')
        ecommerce_account = UserAccount.objects.create(
            user=ecommerce_user,
            account_type='ecommerce',
            role='Staff',
        )
        _, ecommerce_token = ApiToken.issue_token(ecommerce_account)

        response = self.client.get(
            '/v1/sale/parties/',
            HTTP_AUTHORIZATION=f'Bearer {ecommerce_token}',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
