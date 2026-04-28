"""
Stock Master Models
===============

This module provides a complete inventory management system.

Model Overview:
------------
Category         - Groups items into categories (e.g., Electronics, Clothing)
Unit            - Measurement units (e.g., pcs, kg, liters) with conversion support
AttributeType   - Custom attribute types for variants (e.g., Color, Size)
AttributeValue  - Values for attribute types (e.g., Red, Large)
TaxCode         - HSN/SAC codes with tax rates
TaxComponent    - CGST/SGST/IGST rates per TaxCode
Item           - Main product/item master
ItemVariant    - Variants of items (e.g., Red Large)
ItemVariantAttribute - Links variant to attribute values
ItemImage      - Product images with WebP conversion
Batch          - Inventory batches/lots for tracking
OpeningStock   - Initial stock entries (one-time)
StockMovement  - Transaction history for stock changes
ApiConfiguration - Singleton for API token
"""

from decimal import Decimal
from django.db import models
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile


class Category(models.Model):
    """
    Categories items into logical groups.
    
    Purpose: Groups items for organization and filtering.
    
    Fields:
        name (str): Category name, unique
        description (str): Optional description
        is_active (bool): Enable/disable category
    
    API Endpoints:
        GET/POST /v1/api/categories/
        GET/PUT/DELETE /v1/api/categories/{id}/
    
    Filters:
        ?is_active=true - Only active categories
    
    Relationships:
        - One Category has many Items
        - Used for grouping and filtering items
    
    Example:
        Category: Electronics
            Items: LED TV, Mobile Phone, Laptop
    """
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = 'Categories'

    def __str__(self):
        return self.name


class Unit(models.Model):
    """
    Measurement units for items with conversion support.
    
    Purpose: Defines how items are measured (pieces, weight, volume).
    
    Fields:
        name (str): Unit name, unique (e.g., "Kilogram")
        short_code (str): Abbreviation (e.g., "kg")
        target_unit (FK): Unit this converts to (self-reference)
        conversion_factor (decimal): Factor to convert to target unit
        must_be_whole_number (bool): If true, stock must be whole numbers
        is_active (bool): Enable/disable unit
    
    Conversion Logic:
        - Base units have target_unit=None, conversion_factor=1
        - Derived units reference target_unit with conversion_factor
        - Example: box -> pieces: target_unit=pieces, factor=12
        - Example: kg -> grams: target_unit=grams, factor=1000
    
    API Endpoints:
        GET/POST /v1/api/units/
        GET/PUT/DELETE /v1/api/units/{id}/
    
    Filters:
        ?is_active=true
        ?must_be_whole_number=true
    
    Validation:
        - If unit.must_be_whole_number=True, item.current_stock must be integer
    
    Example:
        Unit: pieces (base)       -> target_unit=None, factor=1
        Unit: box                -> target_unit=pieces, factor=12
    """
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
    """
    Custom attribute types for item variants.
    
    Purpose: Defines types of attributes that items can have variants for.
    
    Fields:
        name (str): Type name, unique (e.g., "Color")
        description (str): Optional description
    
    API Endpoints:
        GET/POST /v1/api/attribute-types/
        GET/PUT/DELETE /v1/api/attribute-types/{id}/
    
    Example:
        AttributeType: Color
            AttributeValues: Red, Blue, Green
        
        AttributeType: Size
            AttributeValues: Small, Medium, Large
    """
    name = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class AttributeValue(models.Model):
    """
    Values for attribute types.
    
    Purpose: Defines possible values for each attribute type.
    
    Fields:
        attribute_type (FK): Reference to AttributeType
        value (str): Value name (e.g., "Red", "Large")
    
    Unique Together:
        (attribute_type, value) - No duplicate values per type
    
    API Endpoints:
        GET/POST /v1/api/attribute-values/
        GET/PUT/DELETE /v1/api/attribute-values/{id}/
    
    Filters:
        ?attribute_type=1
    
    Example:
        attribute_type=Color
            AttributeValue: Red
            AttributeValue: Blue
    """
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
    """
    HSN/SAC tax codes for GST calculation.
    
    Purpose: Stores tax codes (HSN for goods, SAC for services) with components.
    
    Fields:
        name (str): Display name (e.g., "Electronics - TV")
        code_type (str): HSN or SAC
        code (str): HSN/SAC code (e.g., 8528)
        is_exempt (bool): If true, nil rated
        is_active (bool): Enable/disable
    
    Unique Together:
        (code_type, code) - Unique code per type
    
    API Endpoints:
        GET/POST /v1/api/tax-codes/
        GET/PUT/DELETE /v1/api/tax-codes/{id}/
    
    Filters:
        ?is_active=true
        ?is_exempt=true
        ?code_type=HSN
    
    How Tax Rates Work:
        1. Create TaxCode with components (CGST, SGST, IGST)
        2. Attach TaxCode to Item via tax_code FK
        3. On item creation, serializer captures current rates to:
           - cgst_rate
           - sgst_rate
           - igst_rate
        4. Rates stored as SNAPSHOT - changes to TaxCode don't affect existing items
    
    Example:
        TaxCode: "Electronics - TV", HSN, 8528
            TaxComponent: CGST, 9%
            TaxComponent: SGST, 9%
            TaxComponent: IGST, 18%
    """
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
    """
    Individual tax components for a TaxCode.
    
    Purpose: Stores CGST, SGST, or IGST rates.
    
    Fields:
        tax_code (FK): Reference to TaxCode
        component (str): CGST, SGST, or IGST
        rate (decimal): Tax percentage
    
    Unique Together:
        (tax_code, component) - One rate per component
    
    API Endpoints:
        GET/POST /v1/api/tax-components/
        GET/PUT/DELETE /v1/api/tax-components/{id}/
    
    Filters:
        ?tax_code=1
        ?component=CGST
    
    How It Works:
        - Each TaxCode can have up to 3 components
        - Rates captured at Item creation time (snapshot)
        - Stored in Item.cgst_rate, sgst_rate, igst_rate
    
    Example:
        TaxCode: 8528
            TaxComponent: CGST, 9%
            TaxComponent: SGST, 9%
            TaxComponent: IGST, 18%
    """
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
    """Validate stock is not negative."""
    if value is None:
        return
    if value < 0:
        raise ValidationError('Stock cannot be negative')


class Item(models.Model):
    """
    Main product/item master.
    
    Purpose: Core model for inventory management.
    
    Fields:
        name (str): Item name
        sku (str): Stock keeping unit, unique
        description (str): Optional description
        category (FK): Reference to Category
        unit (FK): Reference to Unit
        tax_code (FK): Reference to TaxCode
        cgst_rate, sgst_rate, igst_rate: Tax rates (snapshot at creation)
        has_variants (bool): Auto-set when variants added
        current_stock (decimal): Stock quantity (ONLY for items WITHOUT variants)
        min_stock_level (int): Reorder threshold
        max_stock_level (int): Maximum capacity
        unit_price (decimal): Selling price
        cost_price (decimal): Purchase cost
        is_active (bool): Enable/disable
    
    Stock Logic:
        - WITHOUT variants: stock in current_stock field
        - WITH variants: has_variants=True, current_stock=None
          stock tracked in ItemVariant.current_stock
    
    Tax Snapshot:
        On save, if tax_code is set, current rates are captured
        to cgst_rate, sgst_rate, igst_rate fields.
        These rates persist even if TaxCode changes.
    
    Whole Number Validation:
        If unit.must_be_whole_number=True, current_stock must be integer
    
    API Endpoints:
        GET/POST /v1/api/items/
        GET/PUT/DELETE /v1/api/items/{id}/
    
    Nested Endpoints:
        GET/POST /v1/api/items/{id}/variants/
        GET/POST /v1/api/items/{id}/images/
    
    Filters:
        ?is_active=true
        ?category=1
        ?unit=1
        ?has_variants=true
        ?search=query (name, sku, description)
    
    Relationships:
        - Has many Variants (if has_variants=True)
        - Has many Images
        - Has many StockMovements
        - Has many OpeningStocks
    
    Example (Simple Item):
        Item: "LED TV", SKU: "TV-001"
            Category: Electronics
            Unit: pieces
            TaxCode: 8528
            current_stock: 100
            unit_price: 50000
    
    Example (Item with Variants):
        Item: "T-Shirt", SKU: "TSHIRT-001", has_variants=True
            Variant: TSHIRT-RED-S, current_stock: 50
            Variant: TSHIRT-BLU-M, current_stock: 30
    """
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
    """
    Variants of an item (e.g., Red Large, Blue Small).
    
    Purpose: Different versions of an item with unique attributes.
    
    Fields:
        item (FK): Reference to Item
        sku (str): Variant SKU, unique
        unit_price (decimal): Variant-specific price
        cost_price (decimal): Variant-specific cost
        current_stock (decimal): Stock for this variant
        is_active (bool): Enable/disable
    
    Auto-Management:
        - On first variant save: sets item.has_variants=True
        - On first variant save: clears item.current_stock (sets to None)
        - On last variant delete: sets item.has_variants=False
    
    API Endpoints:
        GET/POST /v1/api/items/{item_id}/variants/
        GET/PUT/DELETE /v1/api/items/{item_id}/variants/{id}/
    
    Filters:
        ?is_active=true
    
    Relationships:
        - Belongs to one Item
        - Has many Batch (for stock tracking)
        - Has many ItemVariantAttribute
        - Has many ItemImage
    
    Example:
        Item: "T-Shirt"
            Variant: "TSHIRT-RED-S" (Red, Small), current_stock: 50
            Variant: "TSHIRT-RED-M" (Red, Medium), current_stock: 30
            Variant: "TSHIRT-BLU-L" (Blue, Large), current_stock: 20
    """
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
    """
    Links ItemVariant to AttributeValue.
    
    Purpose: Defines what attributes a variant has.
    
    Fields:
        item_variant (FK): Reference to ItemVariant
        attribute_value (FK): Reference to AttributeValue
    
    Unique Together:
        (item_variant, attribute_value) - No duplicate attributes
    
    Example:
        Variant: TSHIRT-RED-L
            ItemVariantAttribute: Color - Red
            ItemVariantAttribute: Size - Large
    """
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
    """
    Inventory batch/lot tracking.
    
    Purpose: Track stock by purchase lot for FIFO/LIFO costing.
    
    Fields:
        batch_number (str): Batch number, unique
        item_variant (FK): Reference to ItemVariant
        quantity_received (decimal): Original quantity
        quantity_remaining (decimal): Remaining quantity
        cost_per_unit (decimal): Cost per unit
        received_date (date): Date received
        expiry_date (date): Expiry date (optional)
    
    Usage:
        - Created on purchase receipts
        - quantity_remaining decrements on sales
        - Used for FIFO/LIFO cost calculation
    
    API Endpoints:
        GET/POST /v1/api/batches/
        GET/PUT/DELETE /v1/api/batches/{id}/
    
    Filters:
        ?item_variant=1
    
    Example:
        Batch: PO-2024-001
            item_variant: TSHIRT-RED-M
            quantity_received: 100
            quantity_remaining: 85
            cost_per_unit: 450
            received_date: 2024-01-15
    """
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
    """
    Product images with automatic WebP conversion.
    
    Purpose: Store product images with auto-conversion to WebP.
    
    Fields:
        item (FK): Reference to Item (optional)
        item_variant (FK): Reference to ItemVariant (optional)
        image (ImageField): Image file
        is_primary (bool): Primary image flag
        display_order (int): Display order
    
    WebP Conversion:
        - Non-WebP images auto-converted to WebP on save
        - Quality: 85%
        - Falls back to original if conversion fails
    
    API Endpoints:
        GET/POST /v1/api/items/{item_id}/images/
        GET/PUT/DELETE /v1/api/items/{item_id}/images/{id}/
    
    Filters:
        ?item=1
        ?item_variant=1
        ?is_primary=true
    """
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
    """
    Initial stock entry for existing inventory.
    
    Purpose: One-time use for opening balances or cycle counts.
    
    Fields:
        item (FK): Reference to Item
        item_variant (FK): Reference to ItemVariant (optional)
        quantity (decimal): Opening quantity
        unit_cost (decimal): Cost at opening
        as_of_date (date): Effective date
        notes (str): Notes
        status (str): Pending/Approved/Rejected
    
    Workflow:
        1. Create with status=Pending
        2. Admin approves -> updates Item.current_stock or ItemVariant.current_stock
        3. One-time use after approval
    
    API Endpoints:
        GET/POST /v1/api/opening-stock/
        GET/PUT/DELETE /v1/api/opening-stock/{id}/
    
    Filters:
        ?status=Pending
        ?item=1
    """
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
    """
    Transaction history for inventory changes.
    
    Purpose: Records all stock in/out transactions.
    
    Fields:
        movement_type (str): Purchase, Sale, Adjustment, Return, Damage
        item (FK): Reference to Item
        item_variant (FK): Reference to ItemVariant (optional)
        batch (FK): Reference to Batch (optional)
        quantity (decimal): Quantity (+ for in, - for out)
        rate (decimal): Unit rate (snapshot)
        cgst_rate, sgst_rate, igst_rate: Tax rates (snapshot)
        total_amount (decimal): Calculated (quantity * rate)
        reference_number (str): PO/Invoice reference
        movement_date (datetime): Auto-set on creation
        status (str): Pending/Approved/Rejected
    
    Workflow:
        1. Create with status=Pending
        2. Admin approves -> updates stock
        3. If batch linked, decrements batch quantity_remaining
    
    API Endpoints:
        GET/POST /v1/api/stock-movements/
        GET/PUT/DELETE /v1/api/stock-movements/{id}/
    
    Filters:
        ?status=Pending
        ?movement_type=Purchase
        ?item=1
    
    Example:
        Purchase: +100 units @ 50000 each
        Sale: -5 units @ 55000 each
    """
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
    """
    Singleton for API bearer token.
    
    Purpose: Stores token for API authentication.
    
    Fields:
        api_bearer_token (str): Bearer token
        is_active (bool): Enable/disable API access
    
    Singleton Behavior:
        - save() forces pk=1 (only one row)
        - Cannot add/delete via admin
        - Admin redirects to edit page
    
    Authentication:
        - Token passed in header: Authorization: Bearer <token>
        - Validated in ApiKeyAuthentication class
        - Returns (AnonymousUser(), config) on success
    
    To regenerate:
        python manage.py create_api_config --regenerate
    
    API Access:
        All /v1/api/ endpoints require valid Bearer token
    """
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


class SerialNumber(models.Model):
    """
    Serial Number / IMEI Tracking.
    
    Purpose: Track unique serial numbers for items (phones, electronics, etc.)
    
    Fields:
        serial_number (str): Unique serial/IMEI number
        status (str): Available/Sold/Return/Damaged/Lost
        item (FK): Reference to Item
        item_variant (FK): Reference to ItemVariant (optional)
        batch (FK): Reference to Batch (optional)
        warehouse (FK): Reference to Warehouse
        purchase_date (date): When purchased
        sale_date (date): When sold
        warranty_expiry (date): Warranty expiration
        notes (str): Notes
    
    API Endpoints:
        GET/POST /v1/api/serial-numbers/
        GET/PUT/DELETE /v1/api/serial-numbers/{id}/
    
    Filters:
        ?status=Available
        ?item=1
        ?item_variant=1
        ?warehouse=1
    """
    STATUS_CHOICES = [
        ('Available', 'Available'),
        ('Sold', 'Sold'),
        ('Return', 'Return'),
        ('Damaged', 'Damaged'),
        ('Lost', 'Lost'),
    ]

    serial_number = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='Available')
    item = models.ForeignKey(
        'Item',
        on_delete=models.PROTECT,
        related_name='serial_numbers'
    )
    item_variant = models.ForeignKey(
        'ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='serial_numbers'
    )
    batch = models.ForeignKey(
        'Batch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='serial_numbers'
    )
    warehouse = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='serial_numbers'
    )
    purchase_date = models.DateTimeField(null=True, blank=True)
    sale_date = models.DateTimeField(null=True, blank=True)
    warranty_expiry = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Serial Number'
        verbose_name_plural = 'Serial Numbers'
        ordering = ['-created_at']

    def __str__(self):
        return self.serial_number