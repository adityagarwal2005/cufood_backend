import random
import string
from decimal import Decimal
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


class StudentProfile(models.Model):
    """Marks a User as a registered student account, as opposed to a
    restaurant owner (which is just a plain User with an owned Restaurant —
    see Restaurant.owner). Django's User model already covers
    username/email/password, so this only needs to exist to distinguish
    "this account can place orders" from "this account owns a restaurant",
    and to hold future student-only fields (e.g. a WhatsApp-verified phone
    number, once that's built)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="student_profile"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.username


class EmailOTP(models.Model):
    """A one-time login code sent to a student's email. Short-lived and
    single-use — see StudentRequestOtpView (sends) and StudentLoginView
    (verifies) in views.py. Not tied to a User row directly since it also
    has to work for the "email + OTP" login mode, where the lookup is by
    email first."""

    OTP_TTL_MINUTES = 10
    # Caps wrong-code guesses against one OTP, so a 6-digit code can't just
    # be brute-forced within its 10-minute window.
    MAX_ATTEMPTS = 5

    email = models.EmailField()
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    consumed = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)

    @property
    def is_expired(self):
        return timezone.now() - self.created_at > timezone.timedelta(minutes=self.OTP_TTL_MINUTES)


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
    # Every order is now placed by a logged-in student account (see
    # CreateOrderView) — this is that account. student_name is kept in
    # sync with the account's username at order time rather than removed,
    # so everything downstream that already reads order.student_name
    # (owner dashboard, order-status page) didn't need to change when
    # accounts were added. SET_NULL rather than CASCADE so deleting an
    # account doesn't erase its order history.
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )
    student_name = models.CharField(max_length=100)
    # No longer collected at checkout (payment already ties the order to a
    # real phone via Razorpay/UPI, and an account's username is what the
    # restaurant verifies against now) — kept on the model, still blank,
    # only for the handful of pre-accounts orders that have one.
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

    # total_amount is the FULL amount actually charged to the student
    # (menu item subtotal + platform_fee) — that's what gets sent to
    # Razorpay and refunded in full on reject, since the student paid all
    # of it. platform_fee is broken out separately so the restaurant's
    # actual payout (total_amount - platform_fee) can be shown correctly
    # wherever an owner sees "how much do I owe/get for this order" —
    # they never see the fee itself, since it's not theirs.
    # Stored per-order (not looked up from a live constant) so a change to
    # PLATFORM_FEE later doesn't retroactively alter historical orders.
    PLATFORM_FEE = Decimal("1.50")
    total_amount = models.DecimalField(max_digits=8, decimal_places=2)
    platform_fee = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
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


class RestaurantPushSubscription(models.Model):
    """A restaurant owner's Web Push subscription — unlike PushSubscription
    above, this attaches to the restaurant/account itself (owners do log
    in, unlike students), so one subscription covers every future order,
    not just one. Created from the dashboard's "Enable order alerts"
    button. See send_owner_push() in views.py, fired from
    RazorpayWebhookView the moment a new order's payment is confirmed —
    that's the same instant it first becomes visible/actionable to the
    owner (see MyOrdersView), so "new order" and "you can now act on this"
    are the same event."""

    restaurant = models.ForeignKey(
        Restaurant, on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Push subscription for {self.restaurant.name}"
