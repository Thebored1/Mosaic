"""Django app configuration for the audit layer."""

from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Register audit signals when Django starts the app."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'audit'

    def ready(self):
        """Import signal handlers so audit capture is active."""
        from . import signals  # noqa: F401
