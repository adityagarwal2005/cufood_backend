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
    STATUS_PAYMENT_PENDING = "payment_pending"
    STATUS_PLACED = "placed"
    STATUS_ACCEPTED = "accepted"
    STATUS_REJECTED = "rejected"
    STATUS_READY = "ready"
    STATUS_COMPLETED = "completed"
    STATUS_CHOICES = [
        (STATUS_PAYMENT_PENDING, "Payment pending"),
        (STATUS_PLACED, "Placed"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_READY, "Ready for pickup"),
        (STATUS_COMPLETED, "Completed"),
    ]

    PAYMENT_PENDING = "pending"
    PAYMENT_PAID = "paid"
    PAYMENT_REFUNDED = "refunded"
    PAYMENT_FAILED = "failed"
    PAYMENT_STATUS_CHOICES = [
        (PAYMENT_PENDING, "Pending"),
        (PAYMENT_PAID, "Paid"),
        (PAYMENT_REFUNDED, "Refunded"),
        (PAYMENT_FAILED, "Failed"),
    ]

    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.PROTECT, related_name="orders"
    )
    order_code = models.CharField(
        max_length=6, unique=True, default=generate_order_code, editable=False
    )
    student_name = models.CharField(max_length=100)
    student_uid = models.CharField(max_length=50)

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PAYMENT_PENDING
    )
    payment_status = models.CharField(
        max_length=20, choices=PAYMENT_STATUS_CHOICES, default=PAYMENT_PENDING
    )
    razorpay_order_id = models.CharField(max_length=64, blank=True)
    razorpay_payment_id = models.CharField(max_length=64, blank=True)
    razorpay_refund_id = models.CharField(max_length=64, blank=True)

    total_amount = models.DecimalField(max_digits=8, decimal_places=2)
    estimated_ready_minutes = models.PositiveIntegerField(default=15)
    estimated_ready_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def mark_accepted(self):
        self.status = self.STATUS_ACCEPTED
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
