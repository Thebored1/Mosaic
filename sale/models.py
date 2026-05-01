"""
Sale domain models.

This module defines the accounting and transactional backbone of the system:

1. party masters for customers and suppliers
2. POS orders and hold/recall workflows
3. sales invoices and GST-aware tax calculation
4. purchase orders, GRNs, purchase invoices, debit notes, and payments
5. business location anchored numbering and place-of-supply behavior

The sale app remains the back-office ERP layer behind commerce and POS, so the
models carry the tax and accounting state needed by those workflows.
"""

from decimal import Decimal
from django.db import models, transaction
from django.db.models import F
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.utils import timezone
from django.contrib.auth.models import User


class Party(models.Model):
    """
    Party Master - Customer & Supplier Management
    ================================================

    Purpose:
    - Unified master for both Customers and Suppliers
    - Stores GST details for B2B transactions
    - Tracks credit limits and opening balances

    Interaction:
    - FK in Invoice (customer)
    - FK in CreditNote (customer for sales return)
    - FK in Receipt (customer payment)
    - FK in PurchaseOrder (supplier)
    - FK in PurchaseInvoice (supplier)
    - FK in DebitNote (supplier for purchase return)
    - FK in PaymentOut (supplier payment)
    - FK in Order (optional customer for POS)

    Credit Management:
    - credit_limit: Maximum credit allowed to customer
    - outstanding: Calculated from invoices - receipts - credit notes
    - On invoice finalization, checks if party exceeds credit limit
    - Warning/Block if outstanding > credit_limit

    GST Compliance:
    - gstin: Required for B2B transactions (Tax Invoice, Export)
    - For B2C (Cash sales), gstin can be blank
    - state: Determines IGST vs CGST+SGST calculation

    Endpoint Interaction:
    - GET/POST /sale/parties/ - List/Create parties
    - Filtering by party_type (Customer/Supplier/Both)
    - Used as dropdown in invoice/purchase forms
    """
    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='%(class)s_set',
        null=True,
        blank=True
    )
    PARTY_TYPE_CHOICES = [
        ('Customer', 'Customer'),
        ('Supplier', 'Supplier'),
        ('Both', 'Both'),
    ]

    name = models.CharField(max_length=200, help_text="Business or individual name")
    party_type = models.CharField(
        max_length=20,
        choices=PARTY_TYPE_CHOICES,
        default='Customer'
    )
    gstin = models.CharField(
        max_length=15,
        blank=True,
        help_text="GSTIN for B2B transactions (15 characters)"
    )
    state = models.ForeignKey(
        'configuration.State',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='parties'
    )
    address = models.TextField(blank=True, help_text="Billing address")
    shipping_address = models.TextField(
        blank=True,
        help_text="Delivery address (if different from billing)"
    )
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    credit_limit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0'),
        help_text="Maximum credit allowed (0 = no limit)"
    )
    opening_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0'),
        help_text="Opening balance (positive = receivable, negative = payable)"
    )
    opening_balance_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date as of which opening balance is calculated"
    )
    is_active = models.BooleanField(default=True)
    loyalty_points = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Party'
        verbose_name_plural = 'Parties'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.party_type})"

    def clean(self):
        super().clean()
        if self.gstin:
            self.gstin = self.gstin.upper()
            if len(self.gstin) != 15:
                raise ValidationError({'gstin': 'GSTIN must be exactly 15 characters'})

    @property
    def outstanding(self):
        """
        Calculate current outstanding amount.

        Logic:
        - Sales side: Invoices (receivable) - Receipts - Credit Notes
        - Purchase side: Purchase Invoices - Payments - Debit Notes

        Used for:
        - Credit limit warning on invoice
        - Party ledger display
        - Aging analysis
        """
        from django.db.models import Sum, Q

        # Sales outstanding
        sales_invoiced = self.invoices.filter(
            status='Finalized'
        ).aggregate(total=Sum('grand_total'))['total'] or Decimal('0')

        sales_received = self.receipts.aggregate(
            total=Sum('amount')
        )['total'] or Decimal('0')

        sales_credits = self.credit_notes.aggregate(total=Sum('amount'))['total'] or Decimal('0')

        sales_outstanding = sales_invoiced - sales_received - sales_credits

        # Purchase outstanding
        purchase_invoiced = self.purchase_invoices.filter(
            status='Finalized'
        ).aggregate(total=Sum('grand_total'))['total'] or Decimal('0')

        purchase_paid = self.payments_out.aggregate(
            total=Sum('amount')
        )['total'] or Decimal('0')

        purchase_debits = self.debit_notes.aggregate(total=Sum('amount'))['total'] or Decimal('0')

        purchase_outstanding = purchase_invoiced - purchase_paid - purchase_debits

        return self.opening_balance + sales_outstanding - purchase_outstanding


def quantize_money(value):
    """Round money values to two decimals for stored totals."""
    return Decimal(value).quantize(Decimal('0.01'))


class PriceList(models.Model):
    """
    Tenant-scoped item price overrides.

    A price list captures a time-bound catalog of item prices that can be used
    by quotations and other sales documents without mutating stock masters.
    """

    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='price_lists',
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    effective_from = models.DateField(default=timezone.localdate)
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Price List'
        verbose_name_plural = 'Price Lists'
        ordering = ['-is_active', 'name']

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError({'effective_to': 'Effective end date cannot be before the start date.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def get_item_price(self, item, item_variant=None):
        """
        Resolve the configured rate for a stock item or variant.

        Variant-specific overrides win first. If no exact variant override
        exists, fall back to an item-level override.
        """
        queryset = self.items.all()
        if item_variant is not None:
            exact = queryset.filter(item=item, item_variant=item_variant).first()
            if exact is not None:
                return exact
        return queryset.filter(item=item, item_variant__isnull=True).first()


class PriceListItem(models.Model):
    """
    One priced item inside a price list.

    The item is linked to a stock master record and stores the override rate
    that quotation flows can snapshot into customer-facing documents.
    """

    price_list = models.ForeignKey(
        PriceList,
        on_delete=models.CASCADE,
        related_name='items',
    )
    item = models.ForeignKey(
        'stock.Item',
        on_delete=models.CASCADE,
        related_name='price_list_items',
    )
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='price_list_items',
    )
    rate = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Price List Item'
        verbose_name_plural = 'Price List Items'
        unique_together = ['price_list', 'item', 'item_variant']

    def __str__(self):
        return f'{self.price_list.name} - {self.item.sku}'

    def clean(self):
        super().clean()
        if self.item.organization_id != self.price_list.organization_id:
            raise ValidationError({'item': 'Item does not belong to this price list organization.'})
        if self.item_variant_id:
            if self.item_variant.item_id != self.item_id:
                raise ValidationError({'item_variant': 'Variant must belong to the selected item.'})
            if self.item_variant.organization_id != self.price_list.organization_id:
                raise ValidationError({'item_variant': 'Variant does not belong to this price list organization.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class Quotation(models.Model):
    """
    Draft sales quote used before order or invoice conversion.

    Quotations snapshot price-list driven rates and keep the eventual sales
    document immutable after conversion.
    """

    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Sent', 'Sent'),
        ('Accepted', 'Accepted'),
        ('Rejected', 'Rejected'),
        ('Converted', 'Converted'),
        ('Cancelled', 'Cancelled'),
    ]

    quotation_number = models.CharField(max_length=30, unique=True)
    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='quotations',
    )
    party = models.ForeignKey(
        'sale.Party',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quotations',
    )
    business_location = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='quotations',
    )
    price_list = models.ForeignKey(
        PriceList,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quotations',
    )
    quotation_date = models.DateTimeField(default=timezone.now)
    valid_until = models.DateField(null=True, blank=True)
    sub_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    discount_type = models.CharField(
        max_length=10,
        choices=[('Percentage', 'Percentage'), ('Fixed', 'Fixed')],
        default='Fixed'
    )
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'), blank=True)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    notes = models.TextField(blank=True)
    terms = models.TextField(blank=True)
    converted_order = models.OneToOneField(
        'Order',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='source_quotation',
    )
    converted_invoice = models.OneToOneField(
        'Invoice',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='source_quotation',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='quotations_created',
    )

    class Meta:
        verbose_name = 'Quotation'
        verbose_name_plural = 'Quotations'
        ordering = ['-quotation_date', '-id']

    def __str__(self):
        return f'{self.quotation_number} - {self.grand_total}'

    def save(self, *args, **kwargs):
        if not self.quotation_number:
            self.quotation_number = self.generate_quotation_number()
        self.full_clean()
        super().save(*args, **kwargs)

    def generate_quotation_number(self):
        prefix = f"QT{timezone.now().strftime('%Y%m%d')}"
        last = Quotation.objects.filter(quotation_number__startswith=prefix).order_by('-quotation_number').first()
        if last:
            try:
                num = int(last.quotation_number[-4:])
                return f"{prefix}{num + 1:04d}"
            except ValueError:
                pass
        return f"{prefix}0001"

    def recalculate_totals(self):
        subtotal = quantize_money(sum((item.line_total for item in self.items.all()), Decimal('0')))
        self.sub_total = subtotal
        if self.discount_type == 'Percentage':
            self.discount_amount = quantize_money(subtotal * self.discount_percent / 100)
        self.grand_total = quantize_money(subtotal - self.discount_amount)
        self.save(update_fields=['sub_total', 'discount_amount', 'grand_total', 'updated_at'])


    def clean(self):
        super().clean()
        if self.valid_until and self.valid_until < self.quotation_date.date():
            raise ValidationError({'valid_until': 'Valid until cannot be earlier than the quotation date.'})


class QuotationItem(models.Model):
    """
    One quoted line item.

    Rates are copied from the selected price list or from the stock master at
    creation time so future price changes do not rewrite the quote.
    """

    quotation = models.ForeignKey(
        Quotation,
        on_delete=models.CASCADE,
        related_name='items',
    )
    item = models.ForeignKey(
        'stock.Item',
        on_delete=models.PROTECT,
        related_name='quotation_items',
    )
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quotation_items',
    )
    price_list_item = models.ForeignKey(
        PriceListItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quotation_items',
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        validators=[MinValueValidator(Decimal('0.0001'))],
    )
    unit = models.ForeignKey(
        'stock.Unit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='quotation_items',
    )
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Quotation Item'
        verbose_name_plural = 'Quotation Items'
        unique_together = ['quotation', 'item', 'item_variant']

    def __str__(self):
        return f'{self.quotation.quotation_number} - {self.item.sku}'

    def clean(self):
        super().clean()
        if self.quotation_id and self.quotation.status in {'Converted', 'Cancelled'}:
            raise ValidationError({'quotation': 'Converted or cancelled quotations cannot be edited.'})
        if self.item.organization_id != self.quotation.organization_id:
            raise ValidationError({'item': 'Item does not belong to the quotation organization.'})
        if self.item_variant_id:
            if self.item_variant.item_id != self.item_id:
                raise ValidationError({'item_variant': 'Variant must belong to the selected item.'})
            if self.item_variant.organization_id != self.quotation.organization_id:
                raise ValidationError({'item_variant': 'Variant does not belong to the quotation organization.'})

    def save(self, *args, **kwargs):
        self.line_total = quantize_money((self.quantity * self.rate) - self.discount)
        self.full_clean()
        super().save(*args, **kwargs)
        self.quotation.recalculate_totals()

    def delete(self, *args, **kwargs):
        quotation = self.quotation
        super().delete(*args, **kwargs)
        quotation.recalculate_totals()


class Order(models.Model):
    """
    Order - POS Cart / Hold Functionality
    ======================================

    Purpose:
    - Temporary cart for POS billing
    - Supports hold/recall for interrupted transactions
    - Can be converted to finalized invoice

    Interaction:
    - FK to OrderItem (line items via order.order_items)
    - FK to Party (optional customer)
    - FK to BusinessLocation (which store/branch)
    - FK to User (created_by)

    Workflow:
    1. Create Order with items
    2. Status: 'Hold' → Can be recalled later
    3. Status: 'Billing' → Being processed
    4. Convert to Invoice → Status: 'Invoiced'
    5. Cancel → Status: 'Cancelled'

    Hold/Recall Logic:
    - Hold: Stores current cart state, frees up for next customer
    - Recall: Retrieves held order, continues billing
    - Held orders list: GET /sale/orders/?status=Hold

    Endpoint Interaction:
    - POST /sale/orders/ - Create new order
    - GET /sale/orders/ - List all orders (filter by status)
    - POST /sale/orders/{id}/hold/ - Hold current order
    - POST /sale/orders/{id}/recall/ - Recall held order
    - POST /sale/orders/{id}/convert/ - Convert to finalized invoice
    - POST /sale/orders/{id}/cancel/ - Cancel order
    """
    STATUS_CHOICES = [
        ('Hold', 'Hold'),
        ('Billing', 'Billing'),
        ('Invoiced', 'Invoiced'),
        ('Cancelled', 'Cancelled'),
    ]

    order_number = models.CharField(
        max_length=20,
        unique=True,
        help_text="Auto-generated order number"
    )
    party = models.ForeignKey(
        Party,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='orders'
    )
    business_location = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='orders'
    )
    sub_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0')
    )
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0')
    )
    discount_type = models.CharField(
        max_length=10,
        choices=[('Percentage', 'Percentage'), ('Fixed', 'Fixed')],
        default='Fixed'
    )
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0'),
        blank=True
    )
    grand_total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0')
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='Billing'
    )
    hold_notes = models.CharField(
        max_length=200,
        blank=True,
        help_text="Notes when order is put on hold"
    )
    customer_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Walk-in customer name (if no party selected)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='orders_created'
    )

    class Meta:
        verbose_name = 'Order'
        verbose_name_plural = 'Orders'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.order_number} - {self.status}"

    def save(self, *args, **kwargs):
        if not self.order_number:
            self.order_number = self.generate_order_number()
        super().save(*args, **kwargs)

    def generate_order_number(self):
        today = timezone.now().date()
        prefix = f"ORD{today.strftime('%Y%m%d')}"
        last_order = Order.objects.filter(
            order_number__startswith=prefix
        ).order_by('-order_number').first()

        if last_order:
            try:
                last_num = int(last_order.order_number[-4:])
                return f"{prefix}{last_num + 1:04d}"
            except ValueError:
                pass
        return f"{prefix}0001"

    def calculate_totals(self):
        """Recalculate order totals from items."""
        total = sum(item.total for item in self.order_items.all())
        self.sub_total = total

        if self.discount_type == 'Percentage':
            self.discount_amount = total * self.discount_percent / 100

        self.grand_total = total - self.discount_amount
        self.save(update_fields=['sub_total', 'discount_amount', 'grand_total'])


class OrderItem(models.Model):
    """
    OrderItem - Line Items in POS Order
    =====================================

    Purpose:
    - Individual items in an order/cart
    - Similar structure to InvoiceItem but for cart state

    Interaction:
    - FK to Order (parent order)
    - FK to Item/ItemVariant (from stock app)
    - FK to Unit (for quantity display)

    Link to Stock:
    - Uses Item/ItemVariant from stock app
    - On order conversion to invoice, creates InvoiceItem
    - Rate pulled from item's current unit_price

    Endpoint Interaction:
    - POST /sale/orders/{id}/items/ - Add item to order
    - PUT /sale/order-items/{id}/ - Update quantity/rate
    - DELETE /sale/order-items/{id}/ - Remove item
    """
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='order_items'
    )
    item = models.ForeignKey(
        'stock.Item',
        on_delete=models.CASCADE,
        related_name='order_items'
    )
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_items'
    )
    hsn_code = models.CharField(
        max_length=10,
        blank=True,
        help_text="HSN code from item's tax_code"
    )
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('1')
    )
    unit = models.ForeignKey(
        'stock.Unit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_items'
    )
    rate = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Selling price per unit"
    )
    discount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0'),
        help_text="Line-level discount"
    )
    total = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0')
    )

    class Meta:
        verbose_name = 'Order Item'
        verbose_name_plural = 'Order Items'

    def save(self, *args, **kwargs):
        self.total = (self.quantity * self.rate) - self.discount
        super().save(*args, **kwargs)
        self.order.calculate_totals()


class DeliveryChallan(models.Model):
    """
    Delivery challan used to dispatch goods before final sale invoicing.

    Multiple challans can later be consolidated into one invoice while keeping
    traceability to each source document and its lines.
    """
    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Dispatched', 'Dispatched'),
        ('Invoiced', 'Invoiced'),
        ('Cancelled', 'Cancelled'),
    ]

    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='delivery_challans'
    )
    challan_number = models.CharField(max_length=20, unique=True)
    party = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name='delivery_challans'
    )
    business_location = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='delivery_challans'
    )
    challan_date = models.DateField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='delivery_challans_created'
    )

    class Meta:
        verbose_name = 'Delivery Challan'
        verbose_name_plural = 'Delivery Challans'
        ordering = ['-challan_date', '-id']

    def __str__(self):
        return f"{self.challan_number} - {self.party.name}"

    def save(self, *args, **kwargs):
        if not self.challan_number:
            prefix = f"CH{timezone.now().strftime('%Y%m%d')}"
            last = DeliveryChallan.objects.filter(challan_number__startswith=prefix).order_by('-challan_number').first()
            if last:
                try:
                    num = int(last.challan_number[-4:])
                    self.challan_number = f"{prefix}{num + 1:04d}"
                except ValueError:
                    self.challan_number = f"{prefix}0001"
            else:
                self.challan_number = f"{prefix}0001"
        super().save(*args, **kwargs)


class DeliveryChallanItem(models.Model):
    """
    Individual line item inside a delivery challan.

    The item structure mirrors order and invoice line items so challans can be
    copied into a final sale invoice without losing pricing or traceability.
    """
    delivery_challan = models.ForeignKey(
        DeliveryChallan,
        on_delete=models.CASCADE,
        related_name='items'
    )
    item = models.ForeignKey(
        'stock.Item',
        on_delete=models.PROTECT,
        related_name='delivery_challan_items'
    )
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='delivery_challan_items'
    )
    hsn_code = models.CharField(max_length=10, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit = models.ForeignKey(
        'stock.Unit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='delivery_challan_items'
    )
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    class Meta:
        verbose_name = 'Delivery Challan Item'
        verbose_name_plural = 'Delivery Challan Items'

    def save(self, *args, **kwargs):
        self.total = (self.quantity * self.rate) - self.discount
        super().save(*args, **kwargs)


class Invoice(models.Model):
    """
    Invoice - Sales Tax Invoice / Bill of Supply
    ==============================================

    Purpose:
    - Main billing document for sales
    - Supports multiple invoice types for GST compliance
    - Auto-calculates GST based on state comparison

    INTERACTION WITH OTHER MODELS:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                         Invoice Creation Flow                      │
    │                                                                     │
    │   Order (optional) ──► Invoice ──► InvoiceItem (line items)       │
    │         │                   │                │                     │
    │         │                   │                ▼                     │
    │         │                   │         stock.Item/ItemVariant       │
    │         │                   │                │                     │
    │         ▼                   ▼                ▼                     │
    │   Party (Customer)    BusinessLocation    Unit, TaxCode           │
    │                    (multi-GSTIN support)                           │
    │                                                                     │
    │   After Finalize:                                                 │
    │   - Creates Receipt (if payment captured)                         │
    │   - Triggers StockMovement (deduct inventory)                      │
    │   - Updates Party.outstanding                                      │
    └─────────────────────────────────────────────────────────────────────┘

    INVOICE NUMBERING LOGIC:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  Format: {GSTIN}/{FY}/{NNNNN}                                       │
    │  Example: 27AAAAA0000A1Z5/2025-26/00001                            │
    │                                                                     │
    │  How it works:                                                      │
    │  1. User selects BusinessLocation on invoice form                  │
    │  2. On save/finalize, get_next_invoice_number() called             │
    │  3. Method: location.invoice_sequence += 1                         │
    │  4. Uses location's GSTIN and current FY                           │
    │  5. Sequence is unique per business_location, not global           │
    │                                                                     │
    │  Endpoint: GET /sale/invoices/{id}/ - Shows invoice_number        │
    └─────────────────────────────────────────────────────────────────────┘

    GST CALCULATION LOGIC:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  Step 1: Determine place of supply                                   │
    │  - billing_state = party.state (customer's state)                  │
    │  - supplier_state = business_location.state                        │
    │                                                                     │
    │  Step 2: Determine tax type                                        │
    │  - If billing_state == supplier_state → Intra-state                │
    │    → CGST + SGST (each = half of total tax rate)                   │
    │                                                                     │
    │  - If billing_state != supplier_state → Inter-state                │
    │    → IGST (full tax rate)                                          │
    │                                                                     │
    │  Step 3: Calculate per item                                        │
    │  - Get tax_code from stock.Item                                    │
    │  - Get tax components (CGST/SGST/IGST rates)                       │
    │  - Apply to taxable_amount (quantity * rate - discount)            │
    │                                                                     │
    │  Step 4: Aggregate at invoice level                                │
    │  - tax_summary: JSON grouped by tax rate                           │
    │  - Example: {"5": {"cgst": 100, "sgst": 100, "igst": 0},          │
    │             "18": {"cgst": 900, "sgst": 900, "igst": 0}}          │
    └─────────────────────────────────────────────────────────────────────┘

    INVOICE TYPES:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  Type                │ Use Case              │ GST Requirement    │
    │──────────────────────┼───────────────────────┼─────────────────────│
    │  Tax Invoice         │ B2B (GST customer)    │ GSTIN mandatory     │
    │  Bill of Supply      │ B2C non-GST items     │ No GST              │
    │  Export              │ Export to SEZ/Foreign │ IGST + Export docs  │
    │  SEZ                 │ Sale to SEZ unit      │ IGST + SEZ decl     │
    │  Cash                │ B2C cash sale         │ No GSTIN needed     │
    └─────────────────────────────────────────────────────────────────────┘

    FIELDS EXPLAINED:
    - invoice_type: Tax Invoice, Bill of Supply, Export, SEZ, Cash
    - billing_state: Customer's state (determines IGST vs CGST+SGST)
    - status: Draft/Finalized/Cancelled
    - tax_summary: JSON aggregation of GST by rate
    - e_way_bill: For transactions > ₹50,000 (optional)
    - e_invoice_details: IRN, QR code for e-invoicing (optional)

    ENDPOINT INTERACTION:
    - POST /sale/invoices/ - Create draft invoice
    - GET /sale/invoices/ - List with filters (date, party, status)
    - GET /sale/invoices/{id}/ - Detail with tax breakdown
    - POST /sale/invoices/{id}/finalize/ - Finalize & deduct stock
    - POST /sale/invoices/{id}/cancel/ - Cancel (creates credit note)
    - GET /sale/invoices/{id}/print/ - Generate printable format
    """
    INVOICE_TYPE_CHOICES = [
        ('Tax Invoice', 'Tax Invoice'),
        ('Bill of Supply', 'Bill of Supply'),
        ('Export', 'Export'),
        ('SEZ', 'SEZ'),
        ('Cash', 'Cash'),
    ]

    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Finalized', 'Finalized'),
        ('Cancelled', 'Cancelled'),
    ]

    invoice_number = models.CharField(
        max_length=30,
        unique=True,
        help_text="Auto-generated: GSTIN/FY/NNNNN"
    )
    invoice_type = models.CharField(
        max_length=20,
        choices=INVOICE_TYPE_CHOICES,
        default='Tax Invoice'
    )
    party = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name='invoices',
        null=True,
        blank=True
    )
    billing_state = models.ForeignKey(
        'configuration.State',
        on_delete=models.PROTECT,
        related_name='invoices_billed',
        help_text="Place of supply (customer state)"
    )
    business_location = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='invoices'
    )
    source_challans = models.ManyToManyField(
        DeliveryChallan,
        blank=True,
        related_name='invoices'
    )
    invoice_date = models.DateTimeField(default=timezone.now)
    due_date = models.DateField(
        null=True,
        blank=True,
        help_text="Payment due date for credit sales"
    )

    # Items (through model)
    sub_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    discount_type = models.CharField(
        max_length=10,
        choices=[('Percentage', 'Percentage'), ('Fixed', 'Fixed')],
        default='Fixed'
    )
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    tcs_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    tcs_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    gross_profit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    round_off = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    tax_summary = models.JSONField(
        default=dict,
        help_text="GST breakdown by rate: {'5': {'cgst': x, 'sgst': y}, '18': {...}}"
    )

    notes = models.TextField(blank=True)
    terms = models.TextField(
        blank=True,
        default="Goods once sold cannot be taken back. Interest @18% p.a. on delayed payments."
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='Draft'
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cancelled_invoices'
    )

    e_way_bill = models.CharField(max_length=20, blank=True)
    e_invoice_details = models.JSONField(default=dict, blank=True)

    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoices'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='invoices_created'
    )
    salesperson = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoices_sold'
    )

    class Meta:
        verbose_name = 'Invoice'
        verbose_name_plural = 'Invoices'
        ordering = ['-invoice_date', '-id']

    def __str__(self):
        return f"{self.invoice_number} - ₹{self.grand_total}"

    def save(self, *args, **kwargs):
        # Set billing state from party if not set
        if not self.billing_state and self.party and self.party.state:
            self.billing_state = self.party.state
        super().save(*args, **kwargs)

    def calculate_totals(self):
        """
        Recalculate invoice totals from the stored line items.

        The invoice keeps line-item tax snapshots, so the parent totals can be
        rebuilt from the immutable child records at any time.
        """
        items = list(self.items.all())
        sub_total = quantize_money(sum((item.taxable_amount for item in items), Decimal('0')))
        taxable_amount = quantize_money(sub_total - self.discount_amount)

        cgst_amount = quantize_money(sum((item.cgst_amount for item in items), Decimal('0')))
        sgst_amount = quantize_money(sum((item.sgst_amount for item in items), Decimal('0')))
        igst_amount = quantize_money(sum((item.igst_amount for item in items), Decimal('0')))
        cess_amount = quantize_money(sum((item.cess_amount for item in items), Decimal('0')))
        tcs_amount = quantize_money((taxable_amount * self.tcs_rate) / 100)
        cost_basis = quantize_money(sum((item.cost_basis for item in items), Decimal('0')))

        summary = {}
        for item in items:
            if item.igst_amount and not item.cgst_amount and not item.sgst_amount:
                rate_key = str(item.igst_rate)
            else:
                rate_key = str(item.cgst_rate + item.sgst_rate)
            bucket = summary.setdefault(
                rate_key,
                {'taxable': Decimal('0'), 'cgst': Decimal('0'), 'sgst': Decimal('0'), 'igst': Decimal('0')}
            )
            bucket['taxable'] += item.taxable_amount
            bucket['cgst'] += item.cgst_amount
            bucket['sgst'] += item.sgst_amount
            bucket['igst'] += item.igst_amount

        self.sub_total = sub_total
        self.taxable_amount = taxable_amount
        self.cgst_amount = cgst_amount
        self.sgst_amount = sgst_amount
        self.igst_amount = igst_amount
        self.tcs_amount = tcs_amount
        self.round_off = quantize_money(self.round_off or Decimal('0'))
        self.gross_profit_amount = quantize_money(taxable_amount - cost_basis)
        self.grand_total = quantize_money(taxable_amount + cgst_amount + sgst_amount + igst_amount + cess_amount + tcs_amount + self.round_off)
        self.tax_summary = {
            rate: {key: str(quantize_money(value)) for key, value in values.items()}
            for rate, values in summary.items()
        }
        self.save(update_fields=[
            'sub_total',
            'discount_amount',
            'taxable_amount',
            'tcs_amount',
            'gross_profit_amount',
            'cgst_amount',
            'sgst_amount',
            'igst_amount',
            'round_off',
            'grand_total',
            'tax_summary',
            'updated_at',
        ])

    def finalize(self):
        """
        Finalize invoice - deduct stock, update party outstanding.

        FLOW:
        1. Generate invoice number (must happen first)
        2. Set status = 'Finalized'
        3. For each InvoiceItem:
           - Create StockMovement (Sale type, reduce stock)
           - If item has variants: reduce variant stock
           - If no variants: reduce item stock
        4. Update Order status to 'Invoiced' (if linked)
        5. Create Receipt if payment captured (optional)
        """
        with transaction.atomic():
            if self.status != 'Draft':
                raise ValidationError(f"Cannot finalize invoice with status '{self.status}'")

            self.calculate_totals()
            if not self.invoice_number:
                self.invoice_number = self.business_location.get_next_invoice_number()

            self.status = 'Finalized'
            self.invoice_date = timezone.now()
            self.save(update_fields=['status', 'invoice_date', 'invoice_number'])

            from stock.services import post_stock_movement

            organization = self.business_location.organization
            for item in self.items.all():
                post_stock_movement(
                    organization=organization,
                    movement_type='Sale',
                    item=item.item,
                    item_variant=item.item_variant,
                    batch=item.batch,
                    warehouse=self.business_location,
                    quantity=item.quantity,
                    rate=item.rate,
                    cgst_rate=item.cgst_rate,
                    sgst_rate=item.sgst_rate,
                    igst_rate=item.igst_rate,
                    total_amount=item.total,
                    reference_number=self.invoice_number,
                    status='Approved',
                    source_document_type='sale.InvoiceItem',
                    source_document_id=item.pk,
                    source_line_reference=item.pk,
                    notes='Invoice finalization',
                )

            # Update order status
            if self.order:
                self.order.status = 'Invoiced'
                self.order.save(update_fields=['status'])

            from accounting.services import post_sale_invoice
            post_sale_invoice(self)

    def cancel(self, user):
        """
        Cancel invoice - reverse stock, create credit note.

        FLOW:
        1. Set status = 'Cancelled'
        2. Record cancelled_by and cancelled_at
        3. For each InvoiceItem:
           - Create StockMovement (Return type, add stock back)
        4. Update Party outstanding
        """
        with transaction.atomic():
            if self.status != 'Finalized':
                raise ValidationError("Can only cancel finalized invoices")

            self.status = 'Cancelled'
            self.cancelled_by = user
            self.cancelled_at = timezone.now()
            self.save(update_fields=['status', 'cancelled_by', 'cancelled_at'])

            from stock.models import StockMovement
            from stock.services import reverse_stock_movement, post_stock_movement

            for item in self.items.all():
                movement = StockMovement.objects.filter(
                    source_document_type='sale.InvoiceItem',
                    source_document_id=str(item.pk),
                    movement_type='Sale',
                    status='Approved',
                ).order_by('-created_at').first()
                if movement is not None:
                    reverse_stock_movement(
                        movement,
                        reference_number=f'CN-{self.invoice_number}',
                        notes='Invoice cancellation',
                    )
                else:
                    post_stock_movement(
                        organization=self.business_location.organization,
                        movement_type='Return',
                        item=item.item,
                        item_variant=item.item_variant,
                        warehouse=self.business_location,
                        quantity=item.quantity,
                        rate=item.rate,
                        reference_number=f'CN-{self.invoice_number}',
                        status='Approved',
                        source_document_type='sale.InvoiceItem',
                        source_document_id=item.pk,
                        source_line_reference=item.pk,
                        notes='Invoice cancellation',
                    )

            from accounting.services import post_invoice_cancellation
            post_invoice_cancellation(self, user=user)


class InvoiceItem(models.Model):
    """
    InvoiceItem - Line Items in Sales Invoice
    ===========================================

    Purpose:
    - Individual line items in an invoice
    - Contains item details, quantity, rates, and tax breakdown

    INTERACTION:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  InvoiceItem Flow:                                                  │
    │                                                                     │
    │  OrderItem (cart) ──► InvoiceItem (invoice)                        │
    │         │                        │                                  │
    │         ▼                        ▼                                  │
    │    stock.Item ◄─────────────────► stock.Item                        │
    │    stock.ItemVariant ◄─────────► stock.ItemVariant                 │
    │    stock.Unit ◄─────────────────► stock.Unit                       │
    │    stock.TaxCode (HSN) ◄───────► stock.TaxCode (for tax rates)     │
    │                              │                                       │
    │                              ▼                                      │
    │                         stock.Batch (for batch tracking)           │
    └─────────────────────────────────────────────────────────────────────┘

    GST CALCULATION PER ITEM:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  1. taxable_amount = (quantity × rate) - discount                 │
    │                                                                     │
    │  2. Get tax rates from item.tax_code.components                    │
    │     (TaxComponent with CGST/SGST/IGST rates)                        │
    │                                                                     │
    │  3. If Intra-state (billing_state == business_location.state):    │
    │     cgst_rate = tax_rate / 2                                        │
    │     sgst_rate = tax_rate / 2                                        │
    │     cgst_amount = taxable_amount × cgst_rate / 100                 │
    │     sgst_amount = taxable_amount × sgst_rate / 100                 │
    │     igst_rate = 0, igst_amount = 0                                 │
    │                                                                     │
    │  4. If Inter-state:                                                 │
    │     igst_rate = tax_rate                                           │
    │     igst_amount = taxable_amount × igst_rate / 100                 │
    │     cgst_rate = 0, sgst_rate = 0, cgst_amount = 0, sgst_amount = 0 │
    │                                                                     │
    │  5. total = taxable_amount + cgst_amount + sgst_amount + igst_amount│
    └─────────────────────────────────────────────────────────────────────┘

    BATCH TRACKING:
    - Optional link to stock.Batch
    - On finalization, reduces batch quantity_remaining
    - Used for expiry tracking and FIFO/LIFO

    ENDPOINT INTERACTION:
    - Created automatically when converting Order to Invoice
    - POST /sale/invoices/{id}/items/ - Add item to invoice (draft)
    - GET /sale/invoices/{id}/ - Includes items in response
    """
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name='items'
    )
    item = models.ForeignKey(
        'stock.Item',
        on_delete=models.PROTECT,
        related_name='invoice_items'
    )
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_items'
    )
    batch = models.ForeignKey(
        'stock.Batch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_items'
    )
    source_challan = models.ForeignKey(
        DeliveryChallan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_items'
    )

    hsn_code = models.CharField(max_length=10, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit = models.ForeignKey(
        'stock.Unit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_items'
    )
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'), blank=True)

    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    cost_price_snapshot = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    cost_basis = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    gross_profit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    cgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    sgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    igst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    cess_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    cess_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    class Meta:
        verbose_name = 'Invoice Item'
        verbose_name_plural = 'Invoice Items'

    def save(self, *args, **kwargs):
        self.calculate_totals()
        super().save(*args, **kwargs)

    def calculate_totals(self):
        """Calculate tax amounts based on state comparison."""
        if self.item_variant_id:
            self.cost_price_snapshot = self.item_variant.cost_price
        else:
            self.cost_price_snapshot = self.item.cost_price
        self.cost_basis = (self.quantity * self.cost_price_snapshot)
        self.taxable_amount = (self.quantity * self.rate) - self.discount
        self.cgst_rate = Decimal('0')
        self.cgst_amount = Decimal('0')
        self.sgst_rate = Decimal('0')
        self.sgst_amount = Decimal('0')
        self.igst_rate = Decimal('0')
        self.igst_amount = Decimal('0')
        # Check if intra-state or inter-state
        if self.invoice.billing_state_id == self.invoice.business_location.state_id:
            # Intra-state: CGST + SGST
            if self.item.tax_code:
                self.cgst_rate = self.item.cgst_rate
                self.sgst_rate = self.item.sgst_rate

                self.cgst_amount = self.taxable_amount * self.cgst_rate / 100
                self.sgst_amount = self.taxable_amount * self.sgst_rate / 100
        else:
            # Inter-state: IGST
            if self.item.tax_code:
                self.igst_rate = self.item.igst_rate
                self.igst_amount = self.taxable_amount * self.igst_rate / 100

        self.cess_amount = self.taxable_amount * self.cess_rate / 100
        self.total = self.taxable_amount + self.cgst_amount + self.sgst_amount + self.igst_amount + self.cess_amount
        self.gross_profit = self.taxable_amount - self.cost_basis


    def save(self, *args, **kwargs):
        self.calculate_totals()
        super().save(*args, **kwargs)
        if self.invoice_id:
            self.invoice.calculate_totals()


class CreditNote(models.Model):
    """
    CreditNote - Sales Return / Debit to Customer
    ==============================================

    Purpose:
    - Document for sales returns
    - Reduces party outstanding
    - Can optionally restore inventory

    INTERACTION:
    - FK to Invoice (original invoice being returned)
    - FK to Party (customer)
    - CreditNoteItem (line items being returned)
    - When is_stock_returned=True, creates StockMovement (add stock)

    Workflow:
    1. Select Invoice to return
    2. Select items/quantity to return
    3. Auto-calculate return amount
    4. If "Restore Stock" checked → adds inventory back

    ENDPOINT:
    - POST /sale/credit-notes/ - Create credit note
    - GET /sale/credit-notes/ - List returns
    """
    credit_note_number = models.CharField(max_length=20, unique=True)
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.PROTECT,
        related_name='credit_notes'
    )
    party = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name='credit_notes'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    reason = models.CharField(max_length=200)
    is_stock_returned = models.BooleanField(
        default=False,
        help_text="Check to restore inventory"
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='credit_notes_created'
    )

    class Meta:
        verbose_name = 'Credit Note'
        verbose_name_plural = 'Credit Notes'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        created = self.pk is None
        if not self.credit_note_number:
            self.credit_note_number = self.generate_credit_note_number()
        super().save(*args, **kwargs)
        if created and self.amount:
            from accounting.services import post_credit_note
            post_credit_note(self)

    def generate_credit_note_number(self):
        prefix = f"CN{timezone.now().strftime('%Y%m%d')}"
        last = CreditNote.objects.filter(
            credit_note_number__startswith=prefix
        ).order_by('-credit_note_number').first()
        if last:
            try:
                num = int(last.credit_note_number[-4:])
                return f"{prefix}{num + 1:04d}"
            except ValueError:
                pass
        return f"{prefix}0001"

    def calculate_totals(self):
        total = sum(item.refund_amount for item in self.items.all())
        self.amount = total


class CreditNoteItem(models.Model):
    """
    CreditNoteItem - Line Items in Credit Note
    ============================================

    Purpose: Individual items being returned in a credit note.

    Fields:
        credit_note (FK): Parent credit note
        invoice_item (FK): Original invoice item being returned
        quantity_returned (Decimal): Quantity being returned (supports partial)
        rate (Decimal): Rate captured at credit note creation
        discount (Decimal): Line-level discount
        taxable_amount (Decimal): (quantity × rate) - discount
        cgst_rate, sgst_rate, igst_rate (Decimal): Tax rates snapshot
        cgst_amount, sgst_amount, igst_amount (Decimal): Tax amounts
        refund_amount (Decimal): Total refund for this line
    """
    credit_note = models.ForeignKey(
        CreditNote,
        on_delete=models.CASCADE,
        related_name='items'
    )
    invoice_item = models.ForeignKey(
        InvoiceItem,
        on_delete=models.PROTECT,
        related_name='credit_note_items'
    )
    quantity_returned = models.DecimalField(max_digits=12, decimal_places=4)
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    cgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    sgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    igst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    cess_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    cess_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    class Meta:
        verbose_name = 'Credit Note Item'
        verbose_name_plural = 'Credit Note Items'

    def save(self, *args, **kwargs):
        self.calculate_totals()
        super().save(*args, **kwargs)

    def calculate_totals(self):
        self.taxable_amount = (self.quantity_returned * self.rate) - self.discount
        self.cgst_amount = self.taxable_amount * self.cgst_rate / 100
        self.sgst_amount = self.taxable_amount * self.sgst_rate / 100
        self.igst_amount = self.taxable_amount * self.igst_rate / 100
        self.cess_amount = self.taxable_amount * self.cess_rate / 100
        self.refund_amount = self.taxable_amount + self.cgst_amount + self.sgst_amount + self.igst_amount + self.cess_amount


class ReceiptAllocation(models.Model):
    """
    ReceiptAllocation - Link Receipt to Invoice (for advance payments)
    ========================================================

    Purpose: Allows payments to be allocated across multiple invoices.

    Fields:
        receipt (FK): The payment receipt
        invoice (FK): Invoice being paid
        amount_allocated (Decimal): Amount applied to this invoice
    """
    receipt = models.ForeignKey(
        'Receipt',
        on_delete=models.CASCADE,
        related_name='allocations'
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.PROTECT,
        related_name='receipt_allocations'
    )
    amount_allocated = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = 'Receipt Allocation'
        verbose_name_plural = 'Receipt Allocations'
        unique_together = ['receipt', 'invoice']


class PaymentOutAllocation(models.Model):
    """
    PaymentOutAllocation - Link PaymentOut to PurchaseInvoice (for advance payments)
    =======================================================================

    Purpose: Allows payments to be allocated across multiple purchase invoices.

    Fields:
        payment (FK): The payment out
        purchase_invoice (FK): Purchase invoice being paid
        amount_allocated (Decimal): Amount applied to this invoice
    """
    payment = models.ForeignKey(
        'PaymentOut',
        on_delete=models.CASCADE,
        related_name='allocations'
    )
    purchase_invoice = models.ForeignKey(
        'PurchaseInvoice',
        on_delete=models.PROTECT,
        related_name='payment_allocations'
    )
    amount_allocated = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = 'Payment Out Allocation'
        verbose_name_plural = 'Payment Out Allocations'
        unique_together = ['payment', 'purchase_invoice']


class Receipt(models.Model):
    """
    Receipt - Payment Received from Customer
    ==========================================

    Purpose:
    - Records payments against invoices
    - Supports multiple payment modes
    - Updates party outstanding

    INTERACTION:
    - FK to Party (customer)
    - FK to BusinessLocation (which location received payment)
    - FK to User (received_by)
    - ReceiptAllocation (link payments to invoices)

    PAYMENT MODES:
    - Cash: Default, tracked in cash drawer
    - Card: Debit/Credit card
    - UPI: Google Pay, PhonePe, etc.
    - Bank Transfer: NEFT/RTGS
    - Credit: Adjust against credit limit

    Workflow:
    1. Create receipt linked to invoice
    2. Amount ≤ invoice grand_total
    3. Partial payments allowed (multiple receipts per invoice)
    4. Updates party.outstanding

    ENDPOINT:
    - POST /sale/receipts/ - Record payment
    - GET /sale/receipts/ - List payments
    - GET /sale/receipts/{id}/ - Payment detail
    """
    receipt_number = models.CharField(max_length=20, unique=True)
    party = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name='receipts'
    )
    business_location = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='receipts'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_mode = models.CharField(
        max_length=20,
        choices=[
            ('Cash', 'Cash'),
            ('Card', 'Card'),
            ('UPI', 'UPI'),
            ('Bank Transfer', 'Bank Transfer'),
            ('Credit', 'Credit'),
        ],
        default='Cash'
    )
    reference_number = models.CharField(max_length=50, blank=True)
    transaction_date = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    received_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='receipts_received'
    )

    class Meta:
        verbose_name = 'Receipt'
        verbose_name_plural = 'Receipts'
        ordering = ['-transaction_date']

    def save(self, *args, **kwargs):
        created = self.pk is None
        if not self.receipt_number:
            self.receipt_number = self.generate_receipt_number()
        super().save(*args, **kwargs)
        if created and self.amount:
            from accounting.services import post_receipt
            post_receipt(self)

    def generate_receipt_number(self):
        prefix = f"RCPT{timezone.now().strftime('%Y%m%d')}"
        last = Receipt.objects.filter(
            receipt_number__startswith=prefix
        ).order_by('-receipt_number').first()
        if last:
            try:
                num = int(last.receipt_number[-4:])
                return f"{prefix}{num + 1:04d}"
            except ValueError:
                pass
        return f"{prefix}0001"


# ===================== PURCHASE MODELS =====================


class PurchaseOrder(models.Model):
    """
    PurchaseOrder - PO to Suppliers
    =================================

    Purpose:
    - Document to request purchase from supplier
    - Track PO vs GRN vs Invoice
    - Partial receiving support

    INTERACTION:
    - FK to Party (supplier)
    - FK to BusinessLocation (ordering location)
    - Access via order_items (reverse FK)
    - FK to GRN (link when goods received)

    STATUS FLOW:
    - Draft → Sent → Partial Received → Received → Cancelled

    PARTIAL RECEIVING:
    - Create GRN linked to PO
    - PO status: 'Partial Received' if not all items received
    - PO status: 'Received' when full quantity received

    ENDPOINT:
    - POST /sale/purchase-orders/ - Create PO
    - GET /sale/purchase-orders/ - List POs
    - POST /sale/purchase-orders/{id}/send/ - Mark as sent to supplier
    - GET /sale/purchase-orders/{id}/ - PO detail with received status
    """
    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Sent', 'Sent'),
        ('Partial', 'Partial Received'),
        ('Received', 'Received'),
        ('Cancelled', 'Cancelled'),
    ]

    po_number = models.CharField(max_length=20, unique=True)
    supplier = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name='purchase_orders'
    )
    business_location = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='purchase_orders'
    )
    order_date = models.DateField(default=timezone.now)
    expected_date = models.DateField(null=True, blank=True)
    sub_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    terms = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='purchase_orders_created'
    )

    class Meta:
        verbose_name = 'Purchase Order'
        verbose_name_plural = 'Purchase Orders'
        ordering = ['-order_date']

    def __str__(self):
        return f"{self.po_number} - {self.supplier.name}"

    def save(self, *args, **kwargs):
        if not self.po_number:
            self.po_number = self.generate_po_number()
        super().save(*args, **kwargs)

    def recalculate_receiving_status(self):
        """Derive PO status from received quantities on the line items."""
        if self.status == 'Cancelled':
            return self.status

        items = list(self.order_items.all())
        if not items:
            return self.status

        total_ordered = sum((item.quantity_ordered for item in items), Decimal('0'))
        total_received = sum((item.quantity_received for item in items), Decimal('0'))

        if total_received <= 0:
            return 'Draft' if self.status == 'Draft' else 'Sent'
        if total_ordered > 0 and total_received >= total_ordered:
            return 'Received'
        return 'Partial'

    def generate_po_number(self):
        prefix = f"PO{timezone.now().strftime('%Y%m')}"
        last = PurchaseOrder.objects.filter(
            po_number__startswith=prefix
        ).order_by('-po_number').first()
        if last:
            try:
                num = int(last.po_number[-4:])
                return f"{prefix}{num + 1:04d}"
            except ValueError:
                pass
        return f"{prefix}0001"


class PurchaseOrderItem(models.Model):
    """
    PurchaseOrderItem - Items in PO
    ===============================

    Interaction:
    - FK to PurchaseOrder
    - FK to stock.Item / stock.ItemVariant
    - quantity_ordered: Total ordered
    - quantity_received: Total received (updated from GRN)
    """
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name='order_items'
    )
    item = models.ForeignKey(
        'stock.Item',
        on_delete=models.PROTECT,
        related_name='purchase_order_items'
    )
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='purchase_order_items'
    )
    quantity_ordered = models.DecimalField(max_digits=12, decimal_places=4)
    quantity_received = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal('0'))
    unit = models.ForeignKey(
        'stock.Unit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = 'Purchase Order Item'
        verbose_name_plural = 'Purchase Order Items'

    def save(self, *args, **kwargs):
        self.total = (self.quantity_ordered * self.rate) - self.discount
        super().save(*args, **kwargs)


class GoodReceiptNote(models.Model):
    """
    GoodReceiptNote (GRN) - Goods Received from Supplier
    =====================================================

    Purpose:
    - Record goods received from supplier
    - Link to PO for tracking
    - Create Purchase Invoice from GRN

    INTERACTION:
    - FK to PurchaseOrder (optional link)
    - FK to Party (supplier)
    - FK to BusinessLocation
    - Access via grn_items (reverse FK)

    PO LINKING:
    - If PO provided, auto-fill items and expected quantities
    - Track partial receiving against PO
    - Update PO.status based on receiving status

    GRN to Purchase Invoice:
    - Create PurchaseInvoice from GRN
    - Invoice date = GRN date or supplier's invoice date
    - Supplier's invoice number captured for matching

    ENDPOINT:
    - POST /sale/grns/ - Create GRN
    - GET /sale/grns/ - List GRNs
    - POST /sale/grns/{id}/create-invoice/ - Convert to purchase invoice
    """
    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Posted', 'Posted'),
        ('Cancelled', 'Cancelled'),
    ]

    grn_number = models.CharField(max_length=20, unique=True)
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='grns'
    )
    supplier = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name='grns'
    )
    business_location = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='grns'
    )
    received_date = models.DateField(default=timezone.now)
    supplier_invoice_number = models.CharField(max_length=50, blank=True)
    supplier_invoice_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='grns_created'
    )

    class Meta:
        verbose_name = 'Good Receipt Note'
        verbose_name_plural = 'Good Receipt Notes'
        ordering = ['-received_date']

    def __str__(self):
        return f"{self.grn_number} - {self.supplier.name}"

    def save(self, *args, **kwargs):
        if not self.grn_number:
            self.grn_number = self.generate_grn_number()
        super().save(*args, **kwargs)

    def post_receipt(self, user=None):
        from .services import post_good_receipt_note

        return post_good_receipt_note(self, user=user)

    def generate_grn_number(self):
        prefix = f"GRN{timezone.now().strftime('%Y%m')}"
        last = GoodReceiptNote.objects.filter(
            grn_number__startswith=prefix
        ).order_by('-grn_number').first()
        if last:
            try:
                num = int(last.grn_number[-4:])
                return f"{prefix}{num + 1:04d}"
            except ValueError:
                pass
        return f"{prefix}0001"


class GRNItem(models.Model):
    """
    GRNItem - Items Received in GRN
    ===============================

    Interaction:
    - FK to GoodReceiptNote
    - FK to stock.Item / stock.ItemVariant
    - On save, creates Batch (if not exists) for inventory tracking

    BATCH CREATION:
    - Automatically creates batch entry for received items
    - batch_number format: GRN-{grn_number}-{item.sku}
    - quantity_received → quantity_remaining initially
    - cost_per_unit from PO or manual entry
    """
    grn = models.ForeignKey(
        GoodReceiptNote,
        on_delete=models.CASCADE,
        related_name='grn_items'
    )
    item = models.ForeignKey(
        'stock.Item',
        on_delete=models.PROTECT,
        related_name='grn_items'
    )
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='grn_items'
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit = models.ForeignKey(
        'stock.Unit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    batch = models.ForeignKey(
        'stock.Batch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='grn_items'
    )
    total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = 'GRN Item'
        verbose_name_plural = 'GRN Items'

    def save(self, *args, **kwargs):
        self.total = self.quantity * self.rate
        super().save(*args, **kwargs)


class PurchaseInvoice(models.Model):
    """
    PurchaseInvoice - Supplier Bill / Purchase Bill
    ================================================

    Purpose:
    - Bill received from supplier
    - Input Tax Credit (ITC) tracking
    - Can be linked to GRN or direct

    INTERACTION:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  Purchase Flow:                                                    │
    │                                                                     │
    │  PurchaseOrder ──► GoodReceiptNote ──► PurchaseInvoice            │
    │         │                    │                    │                │
    │         │                    │                    ▼                │
    │         │                    │           BusinessLocation         │
    │         │                    │                    │                │
    │         ▼                    ▼                    ▼                │
    │     Supplier ◄─────────────► Party ◄────────────► (ITC tracking)  │
    └─────────────────────────────────────────────────────────────────────┘

    GST CALCULATION (Reverse):
    - Purchase is opposite of sales
    - CGST/SGST paid = Input Tax Credit
    - IGST paid = Input Tax Credit
    - Works same as sales calculation (state comparison)
    - But recorded as "input" tax for ITC

    GRN LINKING:
    - If linked to GRN, auto-populate items
    - supplier_invoice_number: Supplier's bill number
    - supplier_invoice_date: Supplier's bill date

    ENDPOINT:
    - POST /sale/purchase-invoices/ - Create purchase bill
    - GET /sale/purchase-invoices/ - List purchase invoices
    - POST /sale/purchase-invoices/{id}/finalize/ - Finalize & add stock
    """
    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Finalized', 'Finalized'),
        ('Cancelled', 'Cancelled'),
    ]

    invoice_number = models.CharField(max_length=30, unique=True)
    supplier = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name='purchase_invoices'
    )
    business_location = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='purchase_invoices'
    )
    grn = models.ForeignKey(
        GoodReceiptNote,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='purchase_invoices'
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='purchase_invoices'
    )

    supplier_invoice_number = models.CharField(max_length=50)
    supplier_invoice_date = models.DateField()
    invoice_date = models.DateField(default=timezone.now)
    due_date = models.DateField(null=True, blank=True)

    sub_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    round_off = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='Draft'
    )
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='purchase_invoices_created'
    )

    class Meta:
        verbose_name = 'Purchase Invoice'
        verbose_name_plural = 'Purchase Invoices'
        ordering = ['-invoice_date']

    def __str__(self):
        return f"{self.invoice_number} - {self.supplier.name}"

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = self.business_location.get_next_purchase_invoice_number()
        super().save(*args, **kwargs)

    def calculate_totals(self):
        """Rebuild purchase invoice totals from the stored line items."""
        items = list(self.items.all())
        sub_total = quantize_money(sum((item.taxable_amount for item in items), Decimal('0')))
        taxable_amount = quantize_money(sub_total - self.discount_amount)
        cgst_amount = quantize_money(sum((item.cgst_amount for item in items), Decimal('0')))
        sgst_amount = quantize_money(sum((item.sgst_amount for item in items), Decimal('0')))
        igst_amount = quantize_money(sum((item.igst_amount for item in items), Decimal('0')))
        round_off = quantize_money(self.round_off or Decimal('0'))
        grand_total = quantize_money(taxable_amount + cgst_amount + sgst_amount + igst_amount + round_off)

        self.sub_total = sub_total
        self.taxable_amount = taxable_amount
        self.cgst_amount = cgst_amount
        self.sgst_amount = sgst_amount
        self.igst_amount = igst_amount
        self.round_off = round_off
        self.grand_total = grand_total
        self.save(update_fields=[
            'sub_total',
            'discount_amount',
            'taxable_amount',
            'cgst_amount',
            'sgst_amount',
            'igst_amount',
            'round_off',
            'grand_total',
        ])

    def finalize(self):
        """Finalize purchase invoice - add stock (Purchase type movement)."""
        with transaction.atomic():
            if self.status != 'Draft':
                raise ValidationError(f"Cannot finalize purchase invoice with status '{self.status}'")

            self.calculate_totals()
            self.status = 'Finalized'
            self.save(update_fields=['status'])

            if not self.grn_id:
                from stock.services import post_stock_movement

                organization = self.business_location.organization
                for item in self.items.all():
                    post_stock_movement(
                        organization=organization,
                        movement_type='Purchase',
                        item=item.item,
                        item_variant=item.item_variant,
                        warehouse=self.business_location,
                        quantity=item.quantity,
                        rate=item.rate,
                        cgst_rate=item.cgst_rate,
                        sgst_rate=item.sgst_rate,
                        igst_rate=item.igst_rate,
                        total_amount=item.total,
                        reference_number=self.invoice_number,
                        status='Approved',
                        source_document_type='sale.PurchaseInvoiceItem',
                        source_document_id=item.pk,
                        source_line_reference=item.pk,
                        notes='Purchase invoice finalization',
                    )
            else:
                from .services import post_good_receipt_note

                post_good_receipt_note(self.grn)

            from accounting.services import post_purchase_invoice
            post_purchase_invoice(self)

    def cancel(self, user=None):
        with transaction.atomic():
            if self.status != 'Finalized':
                raise ValidationError(f"Cannot cancel purchase invoice with status '{self.status}'")
            self.status = 'Cancelled'
            self.save(update_fields=['status'])
            from accounting.services import post_purchase_invoice_cancellation
            post_purchase_invoice_cancellation(self, user=user)


class PurchaseInvoiceItem(models.Model):
    """
    PurchaseInvoiceItem - Line Items in Purchase Invoice
    ======================================================

    Similar to InvoiceItem but for purchases.
    Calculates ITC (Input Tax Credit) instead of output tax.
    """
    purchase_invoice = models.ForeignKey(
        PurchaseInvoice,
        on_delete=models.CASCADE,
        related_name='items'
    )
    item = models.ForeignKey(
        'stock.Item',
        on_delete=models.PROTECT,
        related_name='purchase_invoice_items'
    )
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='purchase_invoice_items'
    )
    hsn_code = models.CharField(max_length=10, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit = models.ForeignKey(
        'stock.Unit',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'), blank=True)
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    cgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    sgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    igst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    cess_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    cess_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    class Meta:
        verbose_name = 'Purchase Invoice Item'
        verbose_name_plural = 'Purchase Invoice Items'

    def save(self, *args, **kwargs):
        self.calculate_totals()
        super().save(*args, **kwargs)

    def calculate_totals(self):
        """Calculate tax amounts based on supplier state comparison."""
        self.taxable_amount = (self.quantity * self.rate) - self.discount

        if self.purchase_invoice.supplier.state_id == self.purchase_invoice.business_location.state_id:
            self.cgst_rate = self.item.cgst_rate
            self.sgst_rate = self.item.sgst_rate
            self.igst_rate = Decimal('0')

            self.cgst_amount = self.taxable_amount * self.cgst_rate / 100
            self.sgst_amount = self.taxable_amount * self.sgst_rate / 100
            self.igst_amount = Decimal('0')
        else:
            self.igst_rate = self.item.igst_rate
            self.cgst_rate = Decimal('0')
            self.sgst_rate = Decimal('0')

            self.igst_amount = self.taxable_amount * self.igst_rate / 100
            self.cgst_amount = Decimal('0')
            self.sgst_amount = Decimal('0')

        self.cess_amount = self.taxable_amount * self.cess_rate / 100
        self.total = self.taxable_amount + self.cgst_amount + self.sgst_amount + self.igst_amount + self.cess_amount


class DebitNote(models.Model):
    """
    DebitNote - Purchase Return / Credit from Supplier
    ====================================================

    Purpose:
    - Document for purchase returns
    - Reduces ITC claimed
    - Can optionally reverse inventory
    """
    debit_note_number = models.CharField(max_length=20, unique=True)
    purchase_invoice = models.ForeignKey(
        PurchaseInvoice,
        on_delete=models.PROTECT,
        related_name='debit_notes'
    )
    supplier = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name='debit_notes'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=200)
    is_stock_returned = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True
    )

    def save(self, *args, **kwargs):
        created = self.pk is None
        if not self.debit_note_number:
            prefix = f"DN{timezone.now().strftime('%Y%m%d')}"
            last = DebitNote.objects.filter(
                debit_note_number__startswith=prefix
            ).order_by('-debit_note_number').first()
            if last:
                try:
                    num = int(last.debit_note_number[-4:])
                    self.debit_note_number = f"{prefix}{num + 1:04d}"
                except ValueError:
                    self.debit_note_number = f"{prefix}0001"
            else:
                self.debit_note_number = f"{prefix}0001"
        super().save(*args, **kwargs)
        if created and self.amount:
            from accounting.services import post_debit_note
            post_debit_note(self)

    def calculate_totals(self):
        total = sum(item.refund_amount for item in self.items.all())
        self.amount = total


class DebitNoteItem(models.Model):
    """
    DebitNoteItem - Line Items in Debit Note
    ============================================

    Purpose: Individual items being returned in a debit note.

    Fields:
        debit_note (FK): Parent debit note
        purchase_invoice_item (FK): Original purchase invoice item being returned
        quantity_returned (Decimal): Quantity being returned (supports partial)
        rate (Decimal): Rate captured at debit note creation
        discount (Decimal): Line-level discount
        taxable_amount (Decimal): (quantity × rate) - discount
        cgst_rate, sgst_rate, igst_rate (Decimal): Tax rates snapshot
        cgst_amount, sgst_amount, igst_amount (Decimal): Tax amounts
        refund_amount (Decimal): Total refund for this line
    """
    debit_note = models.ForeignKey(
        DebitNote,
        on_delete=models.CASCADE,
        related_name='items'
    )
    purchase_invoice_item = models.ForeignKey(
        'PurchaseInvoiceItem',
        on_delete=models.PROTECT,
        related_name='debit_note_items'
    )
    quantity_returned = models.DecimalField(max_digits=12, decimal_places=4)
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    taxable_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    cgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    sgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    igst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    cess_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    cess_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    class Meta:
        verbose_name = 'Debit Note Item'
        verbose_name_plural = 'Debit Note Items'

    def save(self, *args, **kwargs):
        self.calculate_totals()
        super().save(*args, **kwargs)

    def calculate_totals(self):
        self.taxable_amount = (self.quantity_returned * self.rate) - self.discount
        self.cgst_amount = self.taxable_amount * self.cgst_rate / 100
        self.sgst_amount = self.taxable_amount * self.sgst_rate / 100
        self.igst_amount = self.taxable_amount * self.igst_rate / 100
        self.cess_amount = self.taxable_amount * self.cess_rate / 100
        self.refund_amount = self.taxable_amount + self.cgst_amount + self.sgst_amount + self.igst_amount + self.cess_amount


class PaymentOut(models.Model):
    """
    PaymentOut - Payment Made to Supplier
    =====================================

    Purpose:
    - Records payment against purchase invoice
    - Uses PaymentOutAllocation for allocation tracking
    """
    payment_number = models.CharField(max_length=20, unique=True)
    supplier = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name='payments_out'
    )
    business_location = models.ForeignKey(
        'configuration.Warehouse',
        on_delete=models.PROTECT,
        related_name='payments_out'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_mode = models.CharField(
        max_length=20,
        choices=[
            ('Cash', 'Cash'),
            ('Card', 'Card'),
            ('UPI', 'UPI'),
            ('Bank Transfer', 'Bank Transfer'),
        ],
        default='Bank Transfer'
    )
    reference_number = models.CharField(max_length=50, blank=True)
    transaction_date = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True
    )

    def save(self, *args, **kwargs):
        created = self.pk is None
        if not self.payment_number:
            prefix = f"PAY{timezone.now().strftime('%Y%m%d')}"
            last = PaymentOut.objects.filter(
                payment_number__startswith=prefix
            ).order_by('-payment_number').first()
            if last:
                try:
                    num = int(last.payment_number[-4:])
                    self.payment_number = f"{prefix}{num + 1:04d}"
                except ValueError:
                    self.payment_number = f"{prefix}0001"
            else:
                self.payment_number = f"{prefix}0001"
        super().save(*args, **kwargs)
        if created and self.amount:
            from accounting.services import post_payment_out
            post_payment_out(self)
