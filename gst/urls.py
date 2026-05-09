"""GST compliance API routes."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import GSTReportViewSet


router = DefaultRouter()
router.register(r'reports', GSTReportViewSet, basename='gst-reports')

urlpatterns = [
    path('', include(router.urls)),
]
