import hashlib

from django.conf import settings
from django.db import migrations, models


def backfill_token_hashes(apps, schema_editor):
    api_token_model = apps.get_model('configuration', 'ApiToken')
    super_admin_token_model = apps.get_model('configuration', 'SuperAdminToken')

    for token in api_token_model.objects.all():
        raw_token = token.token or ''
        if not raw_token:
            token.delete()
            continue
        token.token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
        token.token_prefix = raw_token[:8]
        token.token = ''
        token.save(update_fields=['token_hash', 'token_prefix', 'token'])

    for token in super_admin_token_model.objects.all():
        raw_token = token.token or ''
        if not raw_token or token.user_id is None:
            token.delete()
            continue
        token.token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
        token.token_prefix = raw_token[:8]
        token.token = ''
        token.save(update_fields=['token_hash', 'token_prefix', 'token'])


class Migration(migrations.Migration):

    dependencies = [
        ('configuration', '0004_remove_superadmintoken_name_superadmintoken_user'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='apitoken',
            name='token_hash',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='apitoken',
            name='token_prefix',
            field=models.CharField(blank=True, default='', max_length=12),
        ),
        migrations.AlterField(
            model_name='apitoken',
            name='token',
            field=models.CharField(blank=True, default='', editable=False, max_length=128),
        ),
        migrations.AddField(
            model_name='superadmintoken',
            name='token_hash',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='superadmintoken',
            name='token_prefix',
            field=models.CharField(blank=True, default='', max_length=12),
        ),
        migrations.AlterField(
            model_name='superadmintoken',
            name='token',
            field=models.CharField(blank=True, default='', editable=False, max_length=128),
        ),
        migrations.RunPython(backfill_token_hashes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='apitoken',
            name='token_hash',
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name='superadmintoken',
            name='token_hash',
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name='superadmintoken',
            name='user',
            field=models.OneToOneField(help_text='User must be a superuser', on_delete=models.deletion.CASCADE, related_name='super_admin_token', to=settings.AUTH_USER_MODEL),
        ),
    ]
