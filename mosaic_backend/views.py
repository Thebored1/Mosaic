from django.conf import settings
from django.db import connection
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema, inline_serializer


class HealthCheckView(APIView):
    """Lightweight API health check for liveness probes and diagnostics."""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        responses=inline_serializer(
            name='HealthCheckResponse',
            fields={
                'status': serializers.CharField(),
                'database': serializers.CharField(),
                'celery': serializers.DictField(),
                'app': serializers.DictField(),
            },
        )
    )
    def get(self, request):
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        return Response({
            'status': 'ok',
            'database': 'ok',
            'celery': {
                'broker_url_configured': bool(getattr(settings, 'CELERY_BROKER_URL', '')),
                'result_backend_configured': bool(getattr(settings, 'CELERY_RESULT_BACKEND', '')),
                'task_always_eager': getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False),
            },
            'app': {
                'debug': settings.DEBUG,
                'timezone': settings.TIME_ZONE,
            },
        })
