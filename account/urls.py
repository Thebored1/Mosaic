from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import MerchantViewSet, CustomerViewSet


router = DefaultRouter()

router.register(r'merchants', MerchantViewSet, basename='merchants')
router.register(r'customers', CustomerViewSet, basename='customers')

urlpatterns = [
    path('', include(router.urls)),
]