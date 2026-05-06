from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


def populate_expiry(apps, schema_editor):
    ApiToken = apps.get_model('configuration', 'ApiToken')
    SuperAdminToken = apps.get_model('configuration', 'SuperAdminToken')
    now = timezone.now()

    for token in ApiToken.objects.filter(expires_at__isnull=True):
        base = token.created_at or now
        token.expires_at = base + timedelta(days=30)
        token.save(update_fields=['expires_at'])

    for token in SuperAdminToken.objects.filter(expires_at__isnull=True):
        base = token.created_at or now
        token.expires_at = base + timedelta(days=7)
        token.save(update_fields=['expires_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('configuration', '0007_tenantsettings'),
    ]

    operations = [
        migrations.AddField(
            model_name='apitoken',
            name='expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='apitoken',
            name='last_used_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='superadmintoken',
            name='expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='superadmintoken',
            name='last_used_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(populate_expiry, migrations.RunPython.noop),
    ]
