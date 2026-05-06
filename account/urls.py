from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    MerchantViewSet, CustomerViewSet, UserAccountViewSet,
    PublicOnboardingView, SignupView, CsrfBootstrapView, LoginView,
    MeView, CreateOrganizationView, RefreshTokenView, LogoutView,
    PasswordResetRequestView, PasswordResetConfirmView,
)


router = DefaultRouter()

router.register(r'merchants', MerchantViewSet, basename='merchants')
router.register(r'customers', CustomerViewSet, basename='customers')
router.register(r'users', UserAccountViewSet, basename='users')

urlpatterns = [
    path('csrf/', CsrfBootstrapView.as_view(), name='csrf'),
    path('onboard/', PublicOnboardingView.as_view(), name='onboard'),
    path('signup/', SignupView.as_view(), name='signup'),
    path('login/', LoginView.as_view(), name='login'),
    path('me/', MeView.as_view(), name='me'),
    path('create-organization/', CreateOrganizationView.as_view(), name='create-organization'),
    path('refresh/', RefreshTokenView.as_view(), name='refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('password-reset/request/', PasswordResetRequestView.as_view(), name='password-reset-request'),
    path('password-reset/confirm/', PasswordResetConfirmView.as_view(), name='password-reset-confirm'),
    path('', include(router.urls)),
]
