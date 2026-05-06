"""Celery application bootstrap for Mosaic."""

import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mosaic_backend.settings')

app = Celery('mosaic_backend')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
