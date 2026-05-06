from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('configuration', '0006_warehouse_state'),
        ('account', '0003_useraccount_account_type_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='TenantSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email_notifications_enabled', models.BooleanField(default=True)),
                ('sms_notifications_enabled', models.BooleanField(default=False)),
                ('invoice_print_template', models.CharField(choices=[('standard', 'Standard'), ('compact', 'Compact'), ('thermal', 'Thermal')], default='standard', max_length=20)),
                ('receipt_print_template', models.CharField(choices=[('standard', 'Standard'), ('compact', 'Compact'), ('thermal', 'Thermal')], default='standard', max_length=20)),
                ('delivery_note_print_template', models.CharField(choices=[('standard', 'Standard'), ('compact', 'Compact'), ('thermal', 'Thermal')], default='standard', max_length=20)),
                ('fiscal_year_start_month', models.PositiveSmallIntegerField(default=4)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('default_warehouse', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='default_for_tenant_settings', to='configuration.warehouse')),
                ('organization', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='tenant_settings', to='account.organization')),
            ],
            options={
                'verbose_name': 'Tenant Settings',
                'verbose_name_plural': 'Tenant Settings',
            },
        ),
    ]
