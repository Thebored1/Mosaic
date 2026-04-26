from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = 'Categories'

    def __str__(self):
        return self.name


class Unit(models.Model):
    name = models.CharField(max_length=50, unique=True)
    short_code = models.CharField(max_length=10)
    target_unit = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='conversions'
    )
    conversion_factor = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('1'))
    must_be_whole_number = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class AttributeType(models.Model):
    name = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class AttributeValue(models.Model):
    attribute_type = models.ForeignKey(
        AttributeType,
        on_delete=models.CASCADE,
        related_name='values'
    )
    value = models.CharField(max_length=50)

    class Meta:
        unique_together = ('attribute_type', 'value')

    def __str__(self):
        return f'{self.attribute_type.name}: {self.value}'


class TaxCode(models.Model):
    CODE_TYPE_CHOICES = [
        ('HSN', 'HSN'),
        ('SAC', 'SAC'),
    ]

    name = models.CharField(max_length=100)
    code_type = models.CharField(max_length=3, choices=CODE_TYPE_CHOICES)
    code = models.CharField(max_length=10)
    is_exempt = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('code_type', 'code')

    def __str__(self):
        return f'{self.code_type}: {self.code}'


class TaxComponent(models.Model):
    COMPONENT_CHOICES = [
        ('CGST', 'CGST'),
        ('SGST', 'SGST'),
        ('IGST', 'IGST'),
    ]

    tax_code = models.ForeignKey(
        TaxCode,
        on_delete=models.CASCADE,
        related_name='components'
    )
    component = models.CharField(max_length=5, choices=COMPONENT_CHOICES)
    rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))

    class Meta:
        unique_together = ('tax_code', 'component')

    def __str__(self):
        return f'{self.tax_code.code} - {self.component}: {self.rate}%'


def validate_stock(value):
    if value is None:
        return
    if value < 0:
        raise ValidationError('Stock cannot be negative')


class Item(models.Model):
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='items'
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='items'
    )
    tax_code = models.ForeignKey(
        TaxCode,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='items'
    )
    cgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    sgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    igst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    has_variants = models.BooleanField(default=False)
    current_stock = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        blank=True,
        null=True,
        validators=[validate_stock]
    )
    min_stock_level = models.PositiveIntegerField(default=0)
    max_stock_level = models.PositiveIntegerField(default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if not self.has_variants and self.unit and self.current_stock:
            if self.unit.must_be_whole_number and self.current_stock != int(self.current_stock):
                raise ValidationError({
                    'current_stock': f'Current stock must be a whole number for unit "{self.unit.name}"'
                })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} ({self.sku})'


class ItemVariant(models.Model):
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='variants'
    )
    sku = models.CharField(max_length=50, unique=True)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    current_stock = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('0'),
        validators=[validate_stock]
    )
    is_active = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        if self.item.unit and self.item.unit.must_be_whole_number and self.current_stock:
            if self.current_stock != int(self.current_stock):
                raise ValidationError({
                    'current_stock': f'Current stock must be a whole number for unit "{self.item.unit.name}"'
                })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        if self.item and not self.item.has_variants:
            self.item.has_variants = True
            self.item.current_stock = None
            Item.objects.filter(pk=self.item.pk).update(
                has_variants=True,
                current_stock=None
            )

    def delete(self, *args, **kwargs):
        item = self.item
        super().delete(*args, **kwargs)
        if not item.variants.exists():
            item.has_variants = False
            item.save(update_fields=['has_variants'])

    def __str__(self):
        return f'{self.item.name} - {self.sku}'


class ItemVariantAttribute(models.Model):
    item_variant = models.ForeignKey(
        ItemVariant,
        on_delete=models.CASCADE,
        related_name='attributes'
    )
    attribute_value = models.ForeignKey(
        AttributeValue,
        on_delete=models.CASCADE
    )

    class Meta:
        unique_together = ('item_variant', 'attribute_value')

    def __str__(self):
        return f'{self.item_variant.sku}: {self.attribute_value}'


class Batch(models.Model):
    batch_number = models.CharField(max_length=50, unique=True)
    item_variant = models.ForeignKey(
        ItemVariant,
        on_delete=models.CASCADE,
        related_name='batches'
    )
    quantity_received = models.DecimalField(max_digits=12, decimal_places=4)
    quantity_remaining = models.DecimalField(max_digits=12, decimal_places=4)
    cost_per_unit = models.DecimalField(max_digits=12, decimal_places=4)
    received_date = models.DateField()
    expiry_date = models.DateField(blank=True, null=True)

    def clean(self):
        super().clean()
        if self.quantity_remaining > self.quantity_received:
            raise ValidationError({
                'quantity_remaining': 'Remaining quantity cannot exceed received quantity'
            })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.batch_number} - {self.item_variant.sku}'


class ItemImage(models.Model):
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='images'
    )
    item_variant = models.ForeignKey(
        ItemVariant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='images'
    )
    image = models.ImageField(upload_to='items/')
    is_primary = models.BooleanField(default=False)
    display_order = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f'Image - {self.item.sku if self.item else self.item_variant.sku}'

    def save(self, *args, **kwargs):
        if self.image and hasattr(self.image, 'name'):
            file_name = self.image.name.lower()
            if not file_name.endswith('.webp'):
                try:
                    from io import BytesIO
                    from PIL import Image as PILImage

                    img = PILImage.open(self.image)
                    if img.mode in ('RGBA', 'LA', 'P'):
                        img = img.convert('RGB')

                    output = BytesIO()
                    img.save(output, format='WEBP', quality=85, optimize=True)
                    output.seek(0)

                    original_name = self.image.name
                    name_without_ext = original_name.rsplit('.', 1)[0]
                    new_name = f'{name_without_ext}.webp'

                    self.image.save(new_name, ContentFile(output.read()), save=False)
                except Exception:
                    pass

        super().save(*args, **kwargs)


class OpeningStock(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='opening_stocks'
    )
    item_variant = models.ForeignKey(
        ItemVariant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='opening_stocks'
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    as_of_date = models.DateField()
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Opening Stock'
        verbose_name_plural = 'Opening Stocks'

    def __str__(self):
        return f'Opening Stock - {self.item.sku}'


class StockMovement(models.Model):
    MOVEMENT_TYPE_CHOICES = [
        ('Purchase', 'Purchase'),
        ('Sale', 'Sale'),
        ('Adjustment', 'Adjustment'),
        ('Return', 'Return'),
        ('Damage', 'Damage'),
    ]

    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]

    movement_type = models.CharField(max_length=15, choices=MOVEMENT_TYPE_CHOICES)
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name='stock_movements'
    )
    item_variant = models.ForeignKey(
        ItemVariant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='stock_movements'
    )
    batch = models.ForeignKey(
        Batch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='stock_movements'
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    cgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    sgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    igst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference_number = models.CharField(max_length=50, blank=True)
    movement_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Pending')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.movement_type} - {self.item.sku} - {self.quantity}'


class ApiConfiguration(models.Model):
    api_bearer_token = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = 'API Configuration'
        verbose_name_plural = 'API Configuration'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return 'API Configuration'