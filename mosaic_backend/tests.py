from django.test import SimpleTestCase


class DatabaseSettingsTests(SimpleTestCase):
    def test_database_backend_is_postgresql_only(self):
        from django.conf import settings

        self.assertEqual(settings.DATABASES['default']['ENGINE'], 'django.db.backends.postgresql')
        self.assertIn('connect_timeout', settings.DATABASES['default']['OPTIONS'])
