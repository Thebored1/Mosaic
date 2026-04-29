from rest_framework import viewsets
from .models import Merchant, Customer
from .serializers import MerchantSerializer, CustomerSerializer


class MerchantViewSet(viewsets.ModelViewSet):
    serializer_class = MerchantSerializer

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return Merchant.objects.none()
        return Merchant.objects.filter(organization=self.request.auth)


class CustomerViewSet(viewsets.ModelViewSet):
    serializer_class = CustomerSerializer

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return Customer.objects.none()
        return Customer.objects.filter(organization=self.request.auth)