import random
import string
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

IST = ZoneInfo("Asia/Kolkata")


def is_within_business_hours(now=None):
    """All outlets run the same real-world hours: 10am-6pm IST, Mon-Fri.
    Campus-wide, not per-restaurant — there's no version of this business
    where an outlet is legitimately open outside these hours, so it's
    enforced as a hard rule rather than something owners can override
    from the dashboard (see CreateOrderView and the restaurant serializers,
    which both call this instead of just checking Restaurant.is_open_today)."""
    now = (now or timezone.now()).astimezone(IST)
    return now.weekday() < 5 and 10 <= now.hour < 18


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
    # No longer where students pay (see Order — payment goes through
    # Razorpay into the platform's own account now). This is where the
    # platform sends the restaurant's earnings when forwarding them on;
    # kept as the same field/label the owner already knows rather than
    # adding a second one.
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

    @property
    def is_currently_open(self):
        """What students should see: the owner's manual toggle AND real
        business hours both have to say open. Outside 10am-6pm IST Mon-Fri
        this is always False, even if an owner left the toggle on."""
        return self.is_open_today and is_within_business_hours()

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
    # Money flow: the student pays Razorpay directly (not the restaurant's
    # UPI ID) into this platform's own Razorpay account. CreateOrderView
    # creates both this row and a matching Razorpay Order up front, in
    # PAYMENT_PENDING. The only thing that ever moves payment_status to
    # PAID is RazorpayWebhookView receiving and signature-verifying a
    # payment.captured event from Razorpay's servers — there is no
    # student-facing "I've paid" action anymore, deliberately: that was a
    # self-report a student could tap without actually paying, and nothing
    # stopped an owner from starting prep on the strength of it alone.
    # Verification now happens server-to-server, independent of the
    # student's device.
    #
    # An order only becomes visible/actionable to the owner once
    # payment_status is PAID (see MyOrdersView) — by the time it reaches
    # them it's already genuinely paid, so Accept is just "yes, we can make
    # this" and starts prep in one tap, same as before.
    #
    # Rejecting a paid order triggers an automatic refund through Razorpay
    # (see RejectOrderView) rather than the owner having to pay the student
    # back themselves — the platform holds the money, so the platform (via
    # the API, not a person) is what issues it back.
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
    PAYMENT_PAID = "paid"
    PAYMENT_REFUNDED = "refunded"
    # A checkout that was started (Razorpay order created) but never
    # completed within STALE_PENDING_MINUTES — the student closed the
    # modal, lost signal, whatever. Lazily applied (see expire_if_stale())
    # rather than needing a cron job: nothing else in this app runs on a
    # schedule, so "check and flip on next read" avoids adding that
    # infrastructure just for this. Distinct from PENDING so the frontend
    # can stop showing an infinite "confirming payment" spinner and the
    # retry-payment button can refuse to reopen a stale checkout.
    PAYMENT_EXPIRED = "expired"
    PAYMENT_STATUS_CHOICES = [
        (PAYMENT_PENDING, "Pending"),
        (PAYMENT_PAID, "Paid"),
        (PAYMENT_REFUNDED, "Refunded"),
        (PAYMENT_EXPIRED, "Expired"),
    ]

    # How long a checkout can sit unpaid before it's considered abandoned.
    # Long enough that a student fumbling with their banking app isn't cut
    # off mid-payment, short enough that "expired" actually means
    # something by the time anyone sees it.
    STALE_PENDING_MINUTES = 60

    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.PROTECT, related_name="orders"
    )
    order_code = models.CharField(
        max_length=6, unique=True, default=generate_order_code, editable=False
    )
    student_name = models.CharField(max_length=100)
    # Collected at checkout for the restaurant to reach the student directly
    # if needed (e.g. an issue with the order) — refunds no longer go
    # through this at all now that Razorpay refunds the original payment
    # method automatically on reject.
    student_phone_number = models.CharField(max_length=20, blank=True)

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PLACED
    )
    payment_status = models.CharField(
        max_length=20, choices=PAYMENT_STATUS_CHOICES, default=PAYMENT_PENDING
    )
    # Set by CreateOrderView when the Razorpay Order is created — needed to
    # open Checkout and to match an incoming webhook back to this row.
    razorpay_order_id = models.CharField(max_length=64, blank=True)
    # Set by RazorpayWebhookView once payment.captured is verified.
    razorpay_payment_id = models.CharField(max_length=64, blank=True)
    # Set by RejectOrderView after issuing a refund via the Razorpay API.
    razorpay_refund_id = models.CharField(max_length=64, blank=True)
    # Webhook-confirmed payment time — this is real, unlike the old
    # self-reported "I've paid" timestamp it replaces.
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)

    total_amount = models.DecimalField(max_digits=8, decimal_places=2)
    estimated_ready_minutes = models.PositiveIntegerField(default=12)
    estimated_ready_at = models.DateTimeField(null=True, blank=True)
    # Null means "as soon as possible" — the default and by far the common
    # case. When set, the student picked a future pickup slot at checkout
    # (see CreateOrderView), and mark_preparing() below targets that time
    # instead of "now + estimated_ready_minutes".
    scheduled_for = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def expire_if_stale(self):
        """Called on read (order lookup, retry-payment) rather than on a
        schedule. Only ever touches PENDING orders — PAID/REFUNDED/already-
        EXPIRED are left alone, and a real payment.captured webhook is
        still honored for an EXPIRED order (see RazorpayWebhookView): this
        only stops the app from showing an infinite spinner or letting a
        student reopen a stale checkout, it never discards a payment that
        actually went through."""
        if self.payment_status != self.PAYMENT_PENDING:
            return
        age = timezone.now() - self.created_at
        if age > timezone.timedelta(minutes=self.STALE_PENDING_MINUTES):
            self.payment_status = self.PAYMENT_EXPIRED
            self.save(update_fields=["payment_status", "updated_at"])

    def mark_preparing(self):
        self.status = self.STATUS_PREPARING
        now = timezone.now()
        asap_ready = now + timezone.timedelta(minutes=self.estimated_ready_minutes)
        # A scheduled order accepted well ahead of its slot should still
        # show that slot as the ready time, not "12 minutes from now" —
        # but if it's accepted late (scheduled time already close or
        # passed), fall back to the normal prep-time estimate so it
        # doesn't show a ready time in the past.
        if self.scheduled_for and self.scheduled_for > asap_ready:
            self.estimated_ready_at = self.scheduled_for
        else:
            self.estimated_ready_at = asap_ready
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


class PushSubscription(models.Model):
    """A browser's Web Push subscription for one order's status page.
    Students don't have accounts, so this attaches to the specific order
    they're tracking (created when they land on order-status.html and
    grant notification permission) rather than to a user — see
    send_order_push() in views.py for where these actually get used."""

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Push subscription for {self.order.order_code}"
