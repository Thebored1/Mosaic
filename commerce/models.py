"""
Commerce domain models.

This module defines the customer-facing commerce layer that sits on top of the
existing inventory and tenant system. The goal is to support multiple sales
channels without duplicating the ERP/POS models:

1. CommerceListing exposes stock items and variants to a specific channel.
2. ShopperAddress stores reusable buyer addresses for checkout.
3. Cart and CartItem capture the pre-checkout shopping session.
4. CommerceOrder and CommerceOrderLine freeze the cart into an immutable sale.

Design notes:
    - Listings always belong to exactly one organization.
    - Carts and orders belong to a single authenticated UserAccount.
    - Order lines snapshot the item, variant, quantity, SKU, and price at the
      moment of checkout so downstream stock, payment, and fulfillment flows can
      safely evolve without mutating the buyer's historical document.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator


def validate_positive_quantity(value):
    """
    Reject zero or negative commerce quantities.

    Commerce quantity fields are intentionally strict because cart and order
    calculations rely on non-zero units to maintain valid totals and prevent
    silent no-op records from being stored.
    """
    if value <= 0:
        raise ValidationError('Quantity must be greater than zero.')


def quantize_money(value):
    """
    Round monetary values to two decimal places for DecimalField storage.

    The commerce layer stores all monetary totals in two-decimal fields, so this
    helper normalizes intermediate calculations before validation and save().
    """
    return Decimal(value).quantize(Decimal('0.01'))


def get_active_commerce_settings(organization):
    """
    Return the commerce settings row for an organization, creating it if needed.

    The commerce layer keeps operational toggles such as stock reservation and
    oversell protection in a dedicated settings row so the web frontend can be
    configured per tenant without introducing a second config system.
    """
    if organization is None:
        return None
    settings, _ = CommerceSettings.objects.get_or_create(organization=organization)
    return settings


def get_listing_reservation_quantity(listing):
    """
    Return the active reserved quantity for a listing.

    Reservations hold stock that was already checked out but not yet shipped.
    The available quantity exposed to buyers subtracts these held units so the
    storefront does not promise inventory that is already committed elsewhere.
    """
    active_statuses = ['reserved']
    queryset = InventoryReservation.objects.filter(status__in=active_statuses)
    if listing.item_variant_id:
        queryset = queryset.filter(item_variant_id=listing.item_variant_id)
    else:
        queryset = queryset.filter(item_id=listing.item_id, item_variant__isnull=True)
    aggregate = queryset.aggregate(total=Sum('quantity'))
    return aggregate['total'] or Decimal('0')


def get_listing_effective_price(listing, account):
    """
    Resolve the price a buyer should see for a listing.

    B2B buyer-specific pricing is stored as a seller/buyer organization
    override. If the authenticated account is not tied to a buyer organization,
    the listing's public price is used.
    """
    if account is None or getattr(account, 'organization_id', None) is None:
        return listing.price

    override = CommercePriceOverride.objects.filter(
        seller_organization=listing.organization,
        buyer_organization=account.organization,
        listing=listing,
        is_active=True,
    ).first()
    if override is not None:
        return override.price
    return listing.price


def adjust_inventory(listing, quantity_delta):
    """
    Apply a quantity delta to the linked stock source for a listing.

    Positive deltas restock the source, while negative deltas reduce the
    available stock. The helper keeps inventory updates in one place so checkout
    shipping and manual returns can both reuse the same consistency rules.
    """
    source = listing.item_variant if listing.item_variant_id else listing.item
    if source is None:
        return

    current_stock = source.current_stock or Decimal('0')
    new_stock = quantize_money(current_stock + Decimal(quantity_delta))
    if new_stock < 0:
        raise ValidationError('Inventory cannot go below zero.')

    source.current_stock = new_stock
    source.save(update_fields=['current_stock', 'updated_at'] if hasattr(source, 'updated_at') else ['current_stock'])


class CommerceListing(models.Model):
    """
    Channel-aware product listing published by an organization.

    A CommerceListing is the public-facing representation of stock data. It
    references a single `stock.Item` and optionally a single `stock.ItemVariant`
    while adding channel flags and listing metadata such as title, display price,
    and quantity constraints.

    Typical usage:
        - B2C storefronts browse active listings only.
        - B2B portals can reuse the same listing with different channel filters.
        - Marketplace sellers can publish the same stock item under separate
          organization-owned listings.

    Relationship to inventory:
        - The listing does not own inventory.
        - Available quantity is read from the linked item or variant.
        - Validation prevents cross-organization leakage when the listing is
          created or updated.
    """

    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='commerce_listings',
    )
    item = models.ForeignKey(
        'stock.Item',
        on_delete=models.CASCADE,
        related_name='commerce_listings',
    )
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='commerce_listings',
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    compare_at_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    min_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('1'),
        validators=[validate_positive_quantity],
    )
    max_quantity = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    is_b2c_enabled = models.BooleanField(default=True)
    is_b2b_enabled = models.BooleanField(default=True)
    is_marketplace_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Commerce Listing'
        verbose_name_plural = 'Commerce Listings'
        unique_together = ('organization', 'item', 'item_variant')
        ordering = ['title', 'id']

    def __str__(self):
        """Return the buyer-facing listing title."""
        return self.title

    @property
    def available_quantity(self):
        """
        Expose live available stock from the stock app.

        The commerce layer does not maintain its own stock ledger. It reads the
        current inventory values from `stock.Item` or `stock.ItemVariant` so the
        catalog always reflects the latest inventory state.
        """
        reserved_quantity = get_listing_reservation_quantity(self)
        if self.item_variant_id:
            return quantize_money((self.item_variant.current_stock or Decimal('0')) - reserved_quantity)
        return quantize_money((self.item.current_stock or Decimal('0')) - reserved_quantity)

    def clean(self):
        """
        Ensure the listing stays inside one organization's inventory.

        Validation is intentionally defensive because commerce listings are the
        public edge of the system. A seller must not be able to expose another
        organization’s stock, and a variant must always belong to the selected
        item.
        """
        super().clean()
        if self.item.organization_id and self.item.organization_id != self.organization_id:
            raise ValidationError({'item': 'Item does not belong to this organization.'})

        if self.item_variant_id:
            if self.item_variant.item_id != self.item_id:
                raise ValidationError({'item_variant': 'Variant must belong to the selected item.'})
            if self.item_variant.organization_id and self.item_variant.organization_id != self.organization_id:
                raise ValidationError({'item_variant': 'Variant does not belong to this organization.'})

        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            raise ValidationError({'max_quantity': 'Maximum quantity cannot be lower than minimum quantity.'})

    def save(self, *args, **kwargs):
        """Run model validation before persisting the listing."""
        self.full_clean()
        super().save(*args, **kwargs)


class ShopperAddress(models.Model):
    """
    Reusable shipping and billing address owned by a user account.

    Buyer accounts can keep several addresses for checkout and order history.
    The address model is deliberately separate from the order snapshot so that:

        - users can update their saved address book without rewriting past orders
        - checkout can reuse a selected address as a stable snapshot
        - billing and shipping can be managed independently
    """

    user_account = models.ForeignKey(
        'account.UserAccount',
        on_delete=models.CASCADE,
        related_name='shopper_addresses',
    )
    label = models.CharField(max_length=100)
    recipient_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20)
    line1 = models.CharField(max_length=255)
    line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state = models.ForeignKey(
        'configuration.State',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='shopper_addresses',
    )
    postal_code = models.CharField(max_length=20)
    country = models.CharField(max_length=100, default='India')
    is_default_shipping = models.BooleanField(default=False)
    is_default_billing = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Shopper Address'
        verbose_name_plural = 'Shopper Addresses'
        ordering = ['-is_default_shipping', '-is_default_billing', '-updated_at']

    def __str__(self):
        """Display the address label with the owning username."""
        return f'{self.label} - {self.user_account.user.username}'

    def save(self, *args, **kwargs):
        """
        Persist the address and enforce single default shipping/billing values.

        If one address is marked default for shipping or billing, the remaining
        addresses for the same account are cleared so the account never ends up
        with conflicting defaults.
        """
        super().save(*args, **kwargs)
        if self.is_default_shipping:
            self.user_account.shopper_addresses.exclude(pk=self.pk).update(is_default_shipping=False)
        if self.is_default_billing:
            self.user_account.shopper_addresses.exclude(pk=self.pk).update(is_default_billing=False)


class Cart(models.Model):
    """
    Active buyer cart used before checkout.

    A cart is a transient working document. It stays open while the buyer adds
    or updates items, then transitions to `checked_out` when an order is created.
    The cart is scoped to one user account and one channel so that the same
    login can maintain separate B2C, B2B, or marketplace shopping states.
    """

    CHANNEL_CHOICES = [
        ('b2c', 'B2C'),
        ('b2b', 'B2B'),
        ('marketplace', 'Marketplace'),
    ]
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('checked_out', 'Checked Out'),
        ('abandoned', 'Abandoned'),
    ]

    user_account = models.ForeignKey(
        'account.UserAccount',
        on_delete=models.CASCADE,
        related_name='commerce_carts',
    )
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default='b2c')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    notes = models.TextField(blank=True)
    sub_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    checked_out_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        """Return a compact cart identifier for admin/debug use."""
        return f'Cart {self.pk} ({self.user_account.user.username})'

    def recalculate_totals(self):
        """
        Keep cart totals in sync with its line items.

        Cart totals are derived values, not user input. Recomputing them after
        every line item change prevents stale subtotals from being persisted.
        """
        subtotal = quantize_money(sum((item.line_total for item in self.items.all()), Decimal('0')))
        self.sub_total = subtotal
        self.grand_total = subtotal
        self.save(update_fields=['sub_total', 'grand_total', 'updated_at'])

    @classmethod
    def current_for_account(cls, account, channel='b2c'):
        """
        Return the active cart for the user account, creating it if missing.

        This is the entry point used by the API layer to provide a single active
        cart per account/channel pair. It keeps the browsing experience simple:
        if the buyer has no open cart, one is created automatically.
        """
        cart = cls.objects.filter(user_account=account, status='open', channel=channel).first()
        if cart:
            return cart
        return cls.objects.create(user_account=account, channel=channel)


class CartItem(models.Model):
    """
    Snapshot one listing inside a buyer cart.

    Cart items are transient and can be edited freely. Each line points to a
    CommerceListing rather than raw stock models so the cart keeps channel
    pricing and listing constraints intact.
    """

    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name='items')
    listing = models.ForeignKey(CommerceListing, on_delete=models.CASCADE, related_name='cart_items')
    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('1'),
        validators=[validate_positive_quantity],
    )
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('cart', 'listing')
        ordering = ['created_at', 'id']

    def __str__(self):
        """Return the item title and quantity for quick inspection."""
        return f'{self.listing.title} x {self.quantity}'

    def clean(self):
        """
        Validate channel, quantity bounds, and stock before save.

        Validation happens at the cart line level so the API can reject invalid
        quantity changes before the buyer reaches checkout.
        """
        super().clean()
        listing = self.listing
        if not listing.is_active:
            raise ValidationError({'listing': 'Listing is not active.'})
        if self.quantity < listing.min_quantity:
            raise ValidationError({'quantity': 'Quantity is below the listing minimum.'})
        if listing.max_quantity is not None and self.quantity > listing.max_quantity:
            raise ValidationError({'quantity': 'Quantity exceeds the listing maximum.'})

        available = listing.available_quantity
        if available is not None and self.quantity > available:
            raise ValidationError({'quantity': 'Requested quantity exceeds available stock.'})

    def save(self, *args, **kwargs):
        """
        Stamp the current listing price and persist a validated line item.

        The price is copied from the listing at the time the cart item is saved.
        That keeps the cart consistent with the chosen listing and avoids the
        need for a second pricing layer in the checkout flow.
        """
        if not self.unit_price:
            self.unit_price = self.listing.price
        self.line_total = quantize_money(self.unit_price * self.quantity)
        self.full_clean()
        super().save(*args, **kwargs)
        self.cart.recalculate_totals()

    def delete(self, *args, **kwargs):
        """Remove the item and refresh the parent cart totals."""
        cart = self.cart
        super().delete(*args, **kwargs)
        cart.recalculate_totals()


class CommerceOrder(models.Model):
    """
    Storefront order created from a checked-out cart.

    This is the buyer-facing sales document for the commerce layer. It is an
    immutable snapshot of the cart at checkout time and is intentionally kept
    separate from the ERP/POS invoice models so storefront flows can evolve
    without destabilizing back-office accounting.
    """

    STATUS_CHOICES = [
        ('placed', 'Placed'),
        ('cancelled', 'Cancelled'),
    ]
    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('refunded', 'Refunded'),
    ]
    FULFILLMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ]

    order_number = models.CharField(max_length=30, unique=True)
    user_account = models.ForeignKey(
        'account.UserAccount',
        on_delete=models.CASCADE,
        related_name='commerce_orders',
    )
    channel = models.CharField(max_length=20, choices=Cart.CHANNEL_CHOICES, default='b2c')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='placed')
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='pending')
    fulfillment_status = models.CharField(max_length=20, choices=FULFILLMENT_STATUS_CHOICES, default='pending')
    billing_address = models.ForeignKey(
        ShopperAddress,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='billing_orders',
    )
    shipping_address = models.ForeignKey(
        ShopperAddress,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='shipping_orders',
    )
    billing_snapshot = models.JSONField(default=dict)
    shipping_snapshot = models.JSONField(default=dict)
    notes = models.TextField(blank=True)
    sub_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    placed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        """Return the public order number."""
        return self.order_number

    @classmethod
    def next_order_number(cls):
        """
        Generate a human-readable commerce order number.

        The prefix is date-based so operators can quickly identify when the
        order was placed. The suffix is a monotonically increasing sequence for
        the day.
        """
        prefix = f'COM{timezone.now().strftime("%Y%m%d")}'
        last = cls.objects.filter(order_number__startswith=prefix).order_by('-order_number').first()
        if last:
            try:
                return f'{prefix}{int(last.order_number[-4:]) + 1:04d}'
            except ValueError:
                pass
        return f'{prefix}0001'

    def recalculate_totals(self):
        """
        Recalculate order totals from its line items.

        Orders derive their totals from the frozen order lines. This keeps the
        placed order internally consistent even if the source cart is later
        discarded.
        """
        subtotal = quantize_money(sum((line.line_total for line in self.lines.all()), Decimal('0')))
        self.sub_total = subtotal
        self.grand_total = subtotal
        self.save(update_fields=['sub_total', 'grand_total', 'updated_at'])

    def build_marketplace_settlements(self):
        """
        Create per-seller settlements for marketplace orders.

        Each seller organization receives a settlement record containing the
        order lines that belong to its catalog. The method is idempotent: if
        settlements already exist, they are returned unchanged.
        """
        if self.channel != 'marketplace':
            return []
        if self.marketplace_settlements.exists():
            return list(self.marketplace_settlements.prefetch_related('lines'))

        settlements = []
        seller_ids = list(self.lines.values_list('organization_id', flat=True).distinct())
        for seller_org_id in seller_ids:
            seller_org = self.lines.filter(organization_id=seller_org_id).select_related('organization').first().organization
            seller_settings = get_active_commerce_settings(seller_org)
            commission_rate = seller_settings.marketplace_commission_percent if seller_settings is not None else Decimal('0')
            settlement = MarketplaceSettlement.objects.create(
                order=self,
                seller_organization=seller_org,
                commission_rate=commission_rate,
                status='pending',
            )
            seller_lines = self.lines.filter(organization_id=seller_org_id).select_related('listing')
            for order_line in seller_lines:
                MarketplaceSettlementLine.objects.create(
                    settlement=settlement,
                    order_line=order_line,
                    quantity=order_line.quantity,
                    line_total=order_line.line_total,
                    commission_amount=quantize_money(order_line.line_total * commission_rate / Decimal('100')),
                )
            settlement.recalculate_totals()
            settlements.append(settlement)
        return settlements

    def can_cancel(self):
        """
        Return whether the order may still be cancelled.

        The order can only be cancelled before fulfillment begins. Once the
        shipment has been marked as shipped, cancellation is no longer allowed
        because inventory has already been consumed.
        """
        shipment = getattr(self, 'shipment', None)
        if shipment and shipment.status == 'shipped':
            return False
        if self.fulfillment_status in {'shipped', 'delivered'}:
            return False
        return self.status != 'cancelled'

    def cancel_order(self, notes=''):
        """
        Cancel the order before shipment and release any reserved stock.

        The cancellation path is intentionally strict: shipped orders cannot be
        cancelled. If a shipment exists but has not been shipped yet, its
        reservations are released first.
        """
        if not self.can_cancel():
            raise ValidationError('Orders cannot be cancelled after shipment.')

        shipment = getattr(self, 'shipment', None)
        if shipment is not None and shipment.status != 'cancelled':
            shipment.cancel(notes=notes)
        else:
            for reservation in self.reservations.select_related('listing').all():
                if reservation.status == 'reserved':
                    reservation.release(notes=notes)

        for settlement in self.marketplace_settlements.all():
            if settlement.status not in {'paid', 'reversed'}:
                settlement.cancel(notes=notes)

        self.status = 'cancelled'
        self.fulfillment_status = 'cancelled'
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'fulfillment_status', 'notes', 'updated_at'])

    @staticmethod
    def address_snapshot(address):
        """
        Freeze the buyer address used at checkout time.

        Checkout stores a copy of the address fields on the order so future
        edits to the shopper's address book do not rewrite historical orders.
        """
        if address is None:
            return {}
        return {
            'id': address.id,
            'label': address.label,
            'recipient_name': address.recipient_name,
            'phone': address.phone,
            'line1': address.line1,
            'line2': address.line2,
            'city': address.city,
            'state': address.state.name if address.state_id else '',
            'postal_code': address.postal_code,
            'country': address.country,
        }

    @classmethod
    @transaction.atomic
    def create_from_cart(cls, cart, billing_address=None, shipping_address=None, notes=''):
        """
        Create a storefront order and close the source cart.

        The method performs the checkout handoff atomically:
            - validates that the cart is open and not empty
            - creates the order shell
            - copies each cart item into an immutable order line
            - recalculates totals from the frozen lines
            - marks the cart as checked out
        """
        if cart.status != 'open':
            raise ValidationError('Only open carts can be checked out.')
        if not cart.items.exists():
            raise ValidationError('Cannot check out an empty cart.')

        seller_organizations = {
            item.listing.organization_id for item in cart.items.select_related('listing', 'listing__organization')
        }
        if cart.channel != 'marketplace' and len(seller_organizations) != 1:
            raise ValidationError('Checkout currently supports one seller organization per order.')
        if not seller_organizations:
            raise ValidationError('Checkout requires at least one seller organization.')

        order = cls.objects.create(
            order_number=cls.next_order_number(),
            user_account=cart.user_account,
            channel=cart.channel,
            billing_address=billing_address,
            shipping_address=shipping_address,
            billing_snapshot=cls.address_snapshot(billing_address),
            shipping_snapshot=cls.address_snapshot(shipping_address),
            notes=notes,
        )

        for cart_item in cart.items.select_related('listing', 'listing__item', 'listing__item_variant', 'listing__organization'):
            listing = cart_item.listing
            price = quantize_money(get_listing_effective_price(listing, cart.user_account))
            source = listing.item_variant if listing.item_variant_id else listing.item
            seller_settings = get_active_commerce_settings(listing.organization)
            should_reserve_stock = True if seller_settings is None else seller_settings.reserve_stock_on_checkout
            prevent_oversell = True if seller_settings is None else seller_settings.prevent_oversell
            if should_reserve_stock:
                if source is not None and prevent_oversell:
                    source.__class__.objects.select_for_update().get(pk=source.pk)
                    InventoryReservation.objects.select_for_update().filter(
                        listing=listing,
                        status='reserved',
                    ).exists()

                available_quantity = listing.available_quantity
                if available_quantity is not None and cart_item.quantity > available_quantity:
                    raise ValidationError({
                        'quantity': f'Requested quantity exceeds available stock for {listing.title}.'
                    })

            order_line = CommerceOrderLine.objects.create(
                order=order,
                listing=listing,
                organization=listing.organization,
                item=listing.item,
                item_variant=listing.item_variant,
                title=listing.title,
                sku=listing.item_variant.sku if listing.item_variant_id else listing.item.sku,
                quantity=cart_item.quantity,
                unit_price=price,
                line_total=quantize_money(price * cart_item.quantity),
            )

            if should_reserve_stock:
                InventoryReservation.objects.create(
                    organization=listing.organization,
                    order=order,
                    order_line=order_line,
                    listing=listing,
                    item=listing.item,
                    item_variant=listing.item_variant,
                    quantity=cart_item.quantity,
                    status='reserved',
                )

        order.recalculate_totals()
        if cart.channel == 'marketplace':
            order.build_marketplace_settlements()
        cart.status = 'checked_out'
        cart.checked_out_at = timezone.now()
        cart.save(update_fields=['status', 'checked_out_at', 'updated_at'])
        return order


class CommerceOrderLine(models.Model):
    """
    Immutable order line copied from a checked-out cart item.

    Order lines are denormalized on purpose. They preserve the display title,
    SKU, quantity, and price as they existed at checkout time, which is the
    minimum data needed for accounting, fulfillment, and customer support.
    """

    order = models.ForeignKey(CommerceOrder, on_delete=models.CASCADE, related_name='lines')
    listing = models.ForeignKey(
        CommerceListing,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_lines',
    )
    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.PROTECT,
        related_name='commerce_order_lines',
    )
    item = models.ForeignKey('stock.Item', on_delete=models.PROTECT, related_name='commerce_order_lines')
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='commerce_order_lines',
    )
    title = models.CharField(max_length=200)
    sku = models.CharField(max_length=50)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ['id']

    def __str__(self):
        """Return the order number and line title for admin display."""
        return f'{self.order.order_number} - {self.title}'


class MarketplaceSettlement(models.Model):
    """
    Per-seller settlement snapshot for a marketplace order.

    Marketplace checkout can include items from multiple seller organizations.
    This model splits the order revenue by seller so each organization can see
    its gross sales, commission, and net payable amount.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('ready', 'Ready'),
        ('paid', 'Paid'),
        ('cancelled', 'Cancelled'),
        ('reversed', 'Reversed'),
    ]

    order = models.ForeignKey(
        CommerceOrder,
        on_delete=models.CASCADE,
        related_name='marketplace_settlements',
    )
    seller_organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='marketplace_settlements',
    )
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'))
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    adjustment_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('order', 'seller_organization')
        ordering = ['-created_at', 'id']

    def __str__(self):
        """Return the order number and seller organization name."""
        return f'{self.order.order_number} - {self.seller_organization.name}'

    def recalculate_totals(self):
        """
        Recalculate settlement totals from its lines and configured commission.

        The settlement is an accounting snapshot. Gross sales are derived from
        the attached order lines, while commission comes from the seller's
        marketplace configuration.
        """
        gross_amount = quantize_money(sum((line.line_total for line in self.lines.all()), Decimal('0')))
        commission_amount = quantize_money(gross_amount * self.commission_rate / Decimal('100'))
        net_amount = quantize_money(gross_amount - commission_amount + self.adjustment_amount - self.tax_amount)
        self.gross_amount = gross_amount
        self.commission_amount = commission_amount
        self.net_amount = net_amount
        self.save(update_fields=['gross_amount', 'commission_amount', 'net_amount', 'updated_at'])

    def mark_ready(self, notes=''):
        """Mark the settlement as ready for payout."""
        self.status = 'ready'
        self.ready_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'ready_at', 'notes', 'updated_at'])

    def mark_paid(self, notes=''):
        """Mark the settlement as paid to the seller."""
        self.status = 'paid'
        self.paid_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'paid_at', 'notes', 'updated_at'])

    def cancel(self, notes=''):
        """Cancel the settlement before payout processing."""
        self.status = 'cancelled'
        self.cancelled_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'cancelled_at', 'notes', 'updated_at'])


class MarketplaceSettlementLine(models.Model):
    """
    Line-level contribution to a marketplace settlement.

    Each order line is attached to exactly one settlement so seller accounting
    can show how much of the order belongs to a given seller organization.
    """

    settlement = models.ForeignKey(
        MarketplaceSettlement,
        on_delete=models.CASCADE,
        related_name='lines',
    )
    order_line = models.OneToOneField(
        CommerceOrderLine,
        on_delete=models.CASCADE,
        related_name='marketplace_settlement_line',
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    class Meta:
        ordering = ['id']

    def __str__(self):
        """Return the settlement and order line title."""
        return f'{self.settlement.order.order_number} - {self.order_line.title}'


class MarketplacePayout(models.Model):
    """
    Manual or gateway-backed payout for a marketplace settlement.

    Gateway integration can later populate the provider fields. Until then the
    seller can mark the payout as processed manually after review.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
        ('reversed', 'Reversed'),
    ]

    METHOD_CHOICES = [
        ('manual', 'Manual'),
        ('gateway', 'Gateway'),
        ('bank', 'Bank Transfer'),
    ]

    settlement = models.OneToOneField(
        MarketplaceSettlement,
        on_delete=models.CASCADE,
        related_name='payout',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='manual')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    provider_name = models.CharField(max_length=120, blank=True)
    provider_reference = models.CharField(max_length=120, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', 'id']

    def __str__(self):
        """Return the settlement order number and payout status."""
        return f'{self.settlement.order.order_number} - {self.status}'

    def clean(self):
        """Keep the payout aligned with the settlement amount."""
        super().clean()
        if self.amount != self.settlement.net_amount:
            raise ValidationError({'amount': 'Payout amount must match the settlement net amount.'})

    def save(self, *args, **kwargs):
        """Validate the payout before persisting it."""
        self.full_clean()
        super().save(*args, **kwargs)

    def process(self, notes=''):
        """Mark the payout as processed and the settlement as paid."""
        self.status = 'processed'
        self.processed_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'processed_at', 'notes', 'updated_at'])
        self.settlement.mark_paid(notes=notes)

    def fail(self, notes=''):
        """Mark the payout as failed without paying the seller."""
        self.status = 'failed'
        self.failed_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'failed_at', 'notes', 'updated_at'])


class CommerceSettings(models.Model):
    """
    Operational commerce settings for an organization.

    These settings let the seller control how the storefront behaves without
    changing code:

        - whether checkout reserves stock
        - whether oversell protection uses row locking
        - whether fulfillment stays manual
        - whether returns and refunds are handled manually

    The row is one-to-one with an organization so the API can expose a single
    configuration document per tenant.
    """

    organization = models.OneToOneField(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='commerce_settings',
    )
    reserve_stock_on_checkout = models.BooleanField(default=True)
    prevent_oversell = models.BooleanField(default=True)
    manual_fulfillment = models.BooleanField(default=True)
    manual_returns_only = models.BooleanField(default=True)
    allow_b2b_price_overrides = models.BooleanField(default=True)
    marketplace_commission_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal('0'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Commerce Settings'
        verbose_name_plural = 'Commerce Settings'

    def __str__(self):
        """Return the owning organization name."""
        return f'Commerce settings - {self.organization.name}'


class CommercePriceOverride(models.Model):
    """
    Buyer-specific B2B price override for a seller listing.

    Sellers can define negotiated pricing for a specific buyer organization.
    The override is only applied when the authenticated account belongs to the
    buyer organization and the seller has explicitly created the rule.
    """

    seller_organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='commerce_price_overrides',
    )
    buyer_organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='commerce_buyer_price_overrides',
    )
    listing = models.ForeignKey(
        CommerceListing,
        on_delete=models.CASCADE,
        related_name='price_overrides',
    )
    price = models.DecimalField(max_digits=12, decimal_places=2)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('seller_organization', 'buyer_organization', 'listing')
        ordering = ['listing__title', 'buyer_organization__name']

    def clean(self):
        """
        Ensure the override stays within the seller's catalog boundary.

        Price overrides are a seller-owned negotiation tool. The listing must
        belong to the seller organization and the buyer organization must be a
        real tenant, not an ecommerce-only account.
        """
        super().clean()
        if self.listing_id and self.listing.organization_id != self.seller_organization_id:
            raise ValidationError({'listing': 'Listing must belong to the seller organization.'})

    def save(self, *args, **kwargs):
        """Validate the override before persisting it."""
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        """Return a compact buyer-specific price label."""
        return f'{self.listing.title} - {self.buyer_organization.name}'


class InventoryReservation(models.Model):
    """
    Reserve stock for a checked-out commerce order line.

    Reservations are created during checkout and released, consumed, or
    expired later depending on fulfillment outcome. They are the commerce
    layer's concurrency guard for preventing the same units from being sold
    twice before shipment.
    """

    STATUS_CHOICES = [
        ('reserved', 'Reserved'),
        ('released', 'Released'),
        ('consumed', 'Consumed'),
        ('expired', 'Expired'),
    ]

    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='commerce_reservations',
    )
    order = models.ForeignKey(
        CommerceOrder,
        on_delete=models.CASCADE,
        related_name='reservations',
    )
    order_line = models.OneToOneField(
        CommerceOrderLine,
        on_delete=models.CASCADE,
        related_name='reservation',
    )
    listing = models.ForeignKey(
        CommerceListing,
        on_delete=models.CASCADE,
        related_name='reservations',
    )
    item = models.ForeignKey('stock.Item', on_delete=models.PROTECT, related_name='commerce_reservations')
    item_variant = models.ForeignKey(
        'stock.ItemVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='commerce_reservations',
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=4, validators=[validate_positive_quantity])
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='reserved')
    reserved_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-reserved_at', 'id']

    def __str__(self):
        """Return the order number and reserved quantity."""
        return f'{self.order.order_number} - {self.quantity} reserved'

    @property
    def source(self):
        """
        Return the inventory source that this reservation holds.

        Reservations point to the variant when the listing is variant-specific;
        otherwise they point to the parent item.
        """
        return self.item_variant if self.item_variant_id else self.item

    def clean(self):
        """Validate reservation quantity and tenant ownership."""
        super().clean()
        if self.order_line_id and self.order_line.order_id != self.order_id:
            raise ValidationError({'order_line': 'Order line must belong to the selected order.'})
        if self.listing_id and self.listing.organization_id != self.organization_id:
            raise ValidationError({'listing': 'Listing does not belong to the reservation organization.'})
        if self.item_variant_id and self.item_variant.item_id != self.item_id:
            raise ValidationError({'item_variant': 'Variant must belong to the selected item.'})

    def release(self, notes=''):
        """
        Release a reservation back into available stock.

        Release is used when a shopper cancels before shipment or when an order
        expires without being fulfilled.
        """
        self.status = 'released'
        self.released_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'released_at', 'notes'])

    def consume(self, notes=''):
        """
        Mark the reservation as consumed and deduct physical stock.

        Consumption happens when the seller ships the order. The reservation
        disappears from available stock and the underlying item or variant is
        decremented so the warehouse reflects actual on-hand quantity.
        """
        if self.status != 'reserved':
            raise ValidationError('Only reserved stock can be consumed.')

        adjust_inventory(self.listing, Decimal('-1') * self.quantity)
        self.status = 'consumed'
        self.consumed_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'consumed_at', 'notes'])


class CommerceShipment(models.Model):
    """
    Manual fulfillment record for a storefront order.

    The shipment keeps the lifecycle intentionally simple for now. Sellers can
    mark items as packed, shipped, and delivered manually, and the model
    consumes inventory reservations when the shipment becomes shipped.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('packed', 'Packed'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ]
    METHOD_CHOICES = [
        ('manual', 'Manual'),
        ('integrated', 'Integrated'),
    ]

    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='commerce_shipments',
    )
    order = models.OneToOneField(
        CommerceOrder,
        on_delete=models.CASCADE,
        related_name='shipment',
    )
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='manual')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    carrier_name = models.CharField(max_length=120, blank=True)
    tracking_number = models.CharField(max_length=120, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', 'id']

    def __str__(self):
        """Return the order number and fulfillment status."""
        return f'{self.order.order_number} - {self.status}'

    def clean(self):
        """Keep shipment records aligned with the order tenant and state."""
        super().clean()
        if self.order_id:
            if self.order.lines.exclude(organization=self.organization).exists():
                raise ValidationError({'order': 'Shipment organization must match all order lines.'})
        if self.status == 'shipped' and not self.shipped_at:
            self.shipped_at = timezone.now()
        if self.status == 'delivered' and not self.delivered_at:
            self.delivered_at = timezone.now()

    def save(self, *args, **kwargs):
        """Persist the shipment and mirror the status onto the order."""
        self.full_clean()
        super().save(*args, **kwargs)
        status_map = {
            'pending': 'pending',
            'packed': 'processing',
            'shipped': 'shipped',
            'delivered': 'delivered',
            'cancelled': 'cancelled',
        }
        self.order.fulfillment_status = status_map.get(self.status, 'pending')
        self.order.save(update_fields=['fulfillment_status', 'updated_at'])

    def mark_packed(self, notes=''):
        """Mark the order as packed and ready for handoff."""
        self.status = 'packed'
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'notes', 'updated_at'])

    def mark_shipped(self, notes=''):
        """
        Mark the order as shipped and consume inventory reservations.

        Manual fulfillment starts here. The reservations are consumed so the
        actual item or variant stock is reduced only when the package leaves
        the seller's control.
        """
        if self.order.status == 'cancelled':
            raise ValidationError('Cancelled orders cannot be shipped.')

        reservations_by_line = {
            reservation.order_line_id: reservation
            for reservation in self.order.reservations.select_related('listing').all()
        }
        for order_line in self.order.lines.select_related('listing').all():
            reservation = reservations_by_line.get(order_line.id)
            if reservation is not None and reservation.status == 'reserved':
                reservation.consume(notes=notes)
            elif reservation is None:
                adjust_inventory(order_line.listing, Decimal('-1') * order_line.quantity)
        self.status = 'shipped'
        if notes:
            self.notes = notes
        self.shipped_at = timezone.now()
        self.save(update_fields=['status', 'notes', 'shipped_at', 'updated_at'])

    def mark_delivered(self, notes=''):
        """Mark the order as delivered after seller confirmation."""
        self.status = 'delivered'
        if notes:
            self.notes = notes
        self.delivered_at = timezone.now()
        self.save(update_fields=['status', 'notes', 'delivered_at', 'updated_at'])

    def cancel(self, notes=''):
        """Cancel the shipment before it leaves the seller."""
        if self.status == 'shipped':
            raise ValidationError('Cannot cancel a shipment after it has been shipped.')
        for reservation in self.order.reservations.select_related('listing').all():
            if reservation.status == 'reserved':
                reservation.release(notes=notes)
        self.status = 'cancelled'
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'notes', 'updated_at'])


class CommerceReturnRequest(models.Model):
    """
    Manual return request for items sent back by the buyer.

    Returns are only processed after the items have physically been sent back
    to the seller. The request therefore tracks the request lifecycle separately
    from the refund and inventory restock steps.
    """

    STATUS_CHOICES = [
        ('requested', 'Requested'),
        ('approved', 'Approved'),
        ('received', 'Received'),
        ('processed', 'Processed'),
        ('rejected', 'Rejected'),
        ('closed', 'Closed'),
    ]

    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='commerce_return_requests',
    )
    order = models.ForeignKey(
        CommerceOrder,
        on_delete=models.CASCADE,
        related_name='return_requests',
    )
    shipment = models.ForeignKey(
        CommerceShipment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='return_requests',
    )
    order_line = models.ForeignKey(
        CommerceOrderLine,
        on_delete=models.CASCADE,
        related_name='return_requests',
    )
    quantity = models.DecimalField(max_digits=12, decimal_places=4, validators=[validate_positive_quantity])
    reason = models.CharField(max_length=255)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='requested')
    requested_at = models.DateTimeField(auto_now_add=True)
    received_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-requested_at', 'id']

    def __str__(self):
        """Return the order number and return status."""
        return f'{self.order.order_number} - {self.status}'

    def clean(self):
        """Validate the return against the shipped order line."""
        super().clean()
        if self.order_line_id and self.order_line.order_id != self.order_id:
            raise ValidationError({'order_line': 'Return line must belong to the selected order.'})
        if self.shipment_id and self.shipment.order_id != self.order_id:
            raise ValidationError({'shipment': 'Shipment must belong to the selected order.'})
        if self.order_line_id and self.order_line.organization_id != self.organization_id:
            raise ValidationError({'order_line': 'Return organization must match the order line organization.'})
        if self.order.fulfillment_status not in {'shipped', 'delivered'}:
            raise ValidationError({'order': 'Returns are only allowed after shipment.'})
        if self.quantity > self.order_line.quantity:
            raise ValidationError({'quantity': 'Return quantity cannot exceed the original ordered quantity.'})

    def save(self, *args, **kwargs):
        """Validate the request before persisting it."""
        self.full_clean()
        super().save(*args, **kwargs)

    def mark_received(self, notes=''):
        """Mark the return as physically received by the seller."""
        self.status = 'received'
        self.received_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'received_at', 'notes'])

    def process(self, notes=''):
        """
        Process a received return and restock the returned items.

        The seller only processes a return after the package comes back. When
        that happens, the inventory is incremented and the return is marked as
        processed. Refunds can then be handled manually or through a future
        payment integration.
        """
        if self.status != 'received':
            raise ValidationError('Returns can only be processed after the items are received.')

        adjust_inventory(self.order_line.listing, self.quantity)
        self.status = 'processed'
        self.processed_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'processed_at', 'notes'])


class CommerceRefund(models.Model):
    """
    Manual refund record linked to a commerce return request.

    Refunds are intentionally modeled as a separate step from return receipt.
    The seller can receive and inspect items first, then decide when to issue
    the refund using the finance process they prefer.
    """

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('processed', 'Processed'),
        ('rejected', 'Rejected'),
    ]

    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='commerce_refunds',
    )
    order = models.ForeignKey(
        CommerceOrder,
        on_delete=models.CASCADE,
        related_name='refunds',
    )
    return_request = models.OneToOneField(
        CommerceReturnRequest,
        on_delete=models.CASCADE,
        related_name='refund',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', 'id']

    def __str__(self):
        """Return the order number and refund status."""
        return f'{self.order.order_number} - {self.status}'

    def clean(self):
        """Validate the refund against the parent return request."""
        super().clean()
        if self.order_id and self.return_request.order_id != self.order_id:
            raise ValidationError({'return_request': 'Return request must belong to the selected order.'})
        if self.return_request.organization_id != self.organization_id:
            raise ValidationError({'return_request': 'Refund organization must match the return request organization.'})
        if self.return_request.status not in {'received', 'processed', 'closed'}:
            raise ValidationError({'return_request': 'Refunds are only allowed after the return is received.'})

    def save(self, *args, **kwargs):
        """Validate the refund before persisting it."""
        self.full_clean()
        super().save(*args, **kwargs)

    def mark_processed(self, notes=''):
        """Mark the refund as processed by the seller or finance team."""
        self.status = 'processed'
        self.processed_at = timezone.now()
        if notes:
            self.notes = notes
        self.save(update_fields=['status', 'processed_at', 'notes', 'updated_at'])


class Wishlist(models.Model):
    """
    Saved product list for an authenticated buyer account.

    Wishlists help the frontend power saved items and future purchase planning
    without forcing the shopper to keep everything inside a cart.
    """

    CHANNEL_CHOICES = Cart.CHANNEL_CHOICES

    user_account = models.ForeignKey(
        'account.UserAccount',
        on_delete=models.CASCADE,
        related_name='wishlists',
    )
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default='b2c')
    name = models.CharField(max_length=120, default='Wishlist')
    is_default = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_default', '-updated_at']

    def __str__(self):
        """Return the owning username and wishlist name."""
        return f'{self.user_account.user.username} - {self.name}'

    @classmethod
    def current_for_account(cls, account, channel='b2c'):
        """Return the active wishlist for the account and create it if needed."""
        wishlist = cls.objects.filter(user_account=account, channel=channel, is_default=True).first()
        if wishlist:
            return wishlist
        return cls.objects.create(user_account=account, channel=channel)


class WishlistItem(models.Model):
    """
    One saved listing inside a wishlist.

    The wishlist remains a lightweight planning surface. It stores only the
    listing and timestamps, leaving pricing and availability to the catalog.
    """

    wishlist = models.ForeignKey(Wishlist, on_delete=models.CASCADE, related_name='items')
    listing = models.ForeignKey(CommerceListing, on_delete=models.CASCADE, related_name='wishlist_items')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('wishlist', 'listing')
        ordering = ['created_at', 'id']

    def __str__(self):
        """Return the wishlist and listing title."""
        return f'{self.wishlist.name} - {self.listing.title}'


class ProductReview(models.Model):
    """
    Shopper review for a published commerce listing.

    Reviews are buyer-facing social proof. They are kept separate from the
    catalog itself so moderation can be added without disturbing stock or
    pricing data.
    """

    listing = models.ForeignKey(CommerceListing, on_delete=models.CASCADE, related_name='reviews')
    user_account = models.ForeignKey('account.UserAccount', on_delete=models.CASCADE, related_name='product_reviews')
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    title = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True)
    is_approved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('listing', 'user_account')
        ordering = ['-created_at']

    def __str__(self):
        """Return the listing title and rating."""
        return f'{self.listing.title} - {self.rating} stars'

    def clean(self):
        """Require a purchase before allowing a review."""
        super().clean()
        if not CommerceOrderLine.objects.filter(
            order__user_account=self.user_account,
            listing=self.listing,
            order__status='placed',
        ).exists():
            raise ValidationError({'listing': 'A review requires a purchased order for this listing.'})

    def save(self, *args, **kwargs):
        """Validate the review before saving it."""
        self.full_clean()
        super().save(*args, **kwargs)


class CommerceContentPage(models.Model):
    """
    Public storefront page managed by a seller organization.

    Content pages cover home-page sections, about pages, policies, FAQs, and
    other public-facing content required by a storefront experience.
    """

    PAGE_TYPE_CHOICES = [
        ('home', 'Home'),
        ('about', 'About'),
        ('policy', 'Policy'),
        ('faq', 'FAQ'),
        ('category', 'Category'),
        ('custom', 'Custom'),
    ]

    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        related_name='commerce_pages',
    )
    slug = models.SlugField(max_length=120)
    title = models.CharField(max_length=200)
    page_type = models.CharField(max_length=20, choices=PAGE_TYPE_CHOICES, default='custom')
    body = models.TextField(blank=True)
    is_published = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('organization', 'slug')
        ordering = ['title']

    def __str__(self):
        """Return the page title."""
        return self.title


class CommerceNotification(models.Model):
    """
    Buyer notification entry for order and commerce events.

    Notifications let the frontend show a simple inbox for order updates,
    return progress, wishlist activity, and other commerce interactions.
    """

    user_account = models.ForeignKey('account.UserAccount', on_delete=models.CASCADE, related_name='commerce_notifications')
    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='commerce_notifications',
    )
    notification_type = models.CharField(max_length=50)
    title = models.CharField(max_length=200)
    message = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        """Return the notification title."""
        return self.title


class CommerceAuditEvent(models.Model):
    """
    Audit trail entry for commerce activity.

    This table records the operational history of storefront actions so staff
    can inspect who changed pricing, fulfillment, orders, or buyer-facing
    content without depending on application logs.
    """

    actor_user = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='commerce_audit_events',
    )
    actor_account = models.ForeignKey(
        'account.UserAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='commerce_audit_events',
    )
    organization = models.ForeignKey(
        'account.Organization',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='commerce_audit_events',
    )
    action = models.CharField(max_length=80)
    entity_type = models.CharField(max_length=120)
    entity_id = models.CharField(max_length=50, blank=True)
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        """Return a compact audit label."""
        return f'{self.action} - {self.entity_type}'
