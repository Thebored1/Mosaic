from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sale', '0005_goodreceiptnote_posted_at_goodreceiptnote_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invoice',
            name='invoice_number',
            field=models.CharField(help_text='Auto-generated: GSTIN/FY/NNNNN', max_length=50, unique=True),
        ),
        migrations.AlterField(
            model_name='purchaseinvoice',
            name='invoice_number',
            field=models.CharField(max_length=50, unique=True),
        ),
    ]
