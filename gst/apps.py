"""GST app configuration."""

from django.apps import AppConfig


class GSTConfig(AppConfig):
    """Configuration for GST compliance features."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'gst'
