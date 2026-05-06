from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('commerce', '0003_commercesettings_marketplace_commission_percent_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='commerceorder',
            name='checkout_idempotency_key',
            field=models.CharField(blank=True, max_length=80, null=True, unique=True),
        ),
    ]
