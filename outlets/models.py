import random
import string

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


def generate_order_code():
    """6-char code the student shows at pickup and the owner can search by."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(alphabet, k=6))
        if not Order.objects.filter(order_code=code).exists():
            return code


class Location(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    photo = models.URLField(blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Restaurant(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    photo = models.URLField(blank=True)
    is_open_today = models.BooleanField(default=True)
    location = models.ForeignKey(
        Location, on_delete=models.PROTECT, related_name="restaurants"
    )
    contact_number = models.CharField(max_length=20, blank=True)
    # Shown to a student once their order is accepted, so they can pay the
    # restaurant directly (no payment gateway — see Order status machine).
    upi_id = models.CharField(max_length=100, blank=True)
    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="restaurant",
        null=True,
        blank=True,
    )

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class MenuItem(models.Model):
    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.CASCADE, related_name="menu_items"
    )
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=50, blank=True)
    price = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    price_half = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    price_full = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    # For items sold in more than two sizes (e.g. pizza Regular/Medium/Large/Giant).
    # Ordered mapping of size label -> price, e.g. {"Regular": 99, "Medium": 179}.
    # Takes priority over price/price_half/price_full when present.
    price_tiers = models.JSONField(null=True, blank=True)
    is_permanently_active = models.BooleanField(default=True)
    is_available_today = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.restaurant.name})"


class Order(models.Model):
    # Payment happens right after the student places the order — before the
    # restaurant ever sees it. An order only becomes visible/actionable to
    # the owner once payment_status is CLAIMED (see MyOrdersView), so from
    # the owner's side there's no "waiting for payment" state to sit
    # through: by the time an order reaches them, it's already paid, and
    # Accept both confirms and starts prep in one tap.
    #
    # Real owners (not just app design) require this — they won't start
    # cooking without seeing payment first, so payment can't happen after
    # acceptance the way it used to.
    #
    # Rejecting an already-paid order is the one case with no gateway to
    # auto-refund through. That's rare (item availability is already
    # checked at order-creation time, so it's mostly "we're too busy right
    # now"), and when it happens the owner gets the student's phone number
    # (student_phone_number, below) to refund via their UPI app's "Pay via
    # Mobile Number" option, rather than having to ask the student for it.
    STATUS_PLACED = "placed"
    STATUS_PREPARING = "preparing"
    STATUS_REJECTED = "rejected"
    STATUS_READY = "ready"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = [
        (STATUS_PLACED, "Placed"),
        (STATUS_PREPARING, "Preparing"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_READY, "Ready for pickup"),
        (STATUS_COMPLETED, "Completed"),
    ]

    PAYMENT_PENDING = "pending"
    PAYMENT_CLAIMED = "claimed"
    PAYMENT_STATUS_CHOICES = [
        (PAYMENT_PENDING, "Pending"),
        (PAYMENT_CLAIMED, "Claimed by student"),
    ]

    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.PROTECT, related_name="orders"
    )
    order_code = models.CharField(
        max_length=6, unique=True, default=generate_order_code, editable=False
    )
    student_name = models.CharField(max_length=100)
    # Collected at checkout solely so a rejected-after-payment order gives
    # the owner a number to refund via their UPI app's "Pay via Mobile
    # Number" option, instead of having to ask the student for it after
    # the fact. Not a VPA — UPI's deep-link spec only supports payee VPA
    # (pa=), not a phone number, so this can't be turned into a tap-to-pay
    # link or QR the way restaurant_upi_id can; it's shown as plain text
    # for the owner to enter manually.
    student_phone_number = models.CharField(max_length=20, blank=True)

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PLACED
    )
    payment_status = models.CharField(
        max_length=20, choices=PAYMENT_STATUS_CHOICES, default=PAYMENT_PENDING
    )
    # Student taps "I've paid" -> payment_claimed_at. Purely informational;
    # nothing in the owner flow waits on it.
    payment_claimed_at = models.DateTimeField(null=True, blank=True)

    total_amount = models.DecimalField(max_digits=8, decimal_places=2)
    estimated_ready_minutes = models.PositiveIntegerField(default=12)
    estimated_ready_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def mark_preparing(self):
        self.status = self.STATUS_PREPARING
        self.estimated_ready_at = timezone.now() + timezone.timedelta(
            minutes=self.estimated_ready_minutes
        )
        self.save(update_fields=["status", "estimated_ready_at", "updated_at"])

    def __str__(self):
        return f"Order {self.order_code} ({self.restaurant.name})"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    # Nullable + SET_NULL so deleting a menu item later doesn't erase order
    # history; name/size/price are snapshotted at order time regardless,
    # since the menu item's own price can change after the order is placed.
    menu_item = models.ForeignKey(
        MenuItem, on_delete=models.SET_NULL, null=True, blank=True
    )
    name = models.CharField(max_length=100)
    size_label = models.CharField(max_length=30, blank=True)
    unit_price = models.DecimalField(max_digits=7, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.quantity}x {self.name} ({self.order.order_code})"
