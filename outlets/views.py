import json
import logging
import secrets
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from time import monotonic
from urllib.parse import quote

import razorpay
import requests
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import escape
from pywebpush import WebPushException, webpush
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .throttles import LoginIdentifierThrottle
from .models import (
    IST,
    EmailOTP,
    Location,
    MenuItem,
    Order,
    OrderItem,
    PushSubscription,
    Restaurant,
    RestaurantPushSubscription,
    StudentProfile,
    is_within_business_hours,
)

logger = logging.getLogger(__name__)


def _send_webpush_to(subscriptions, title, body, url, context_label):
    """Shared send loop for both push flows below — the only difference
    between a student's per-order subscription and an owner's per-restaurant
    one is what they're stored against, not how sending/cleanup works.
    Best-effort: failures here should never break the status transition
    that triggered them, so every exception is swallowed after logging.
    A 410 Gone means the browser/OS revoked that subscription (uninstalled,
    permission revoked, etc.) — deleting it rather than retrying it forever."""
    if not settings.VAPID_PRIVATE_KEY:
        return
    deadline = monotonic() + PUSH_TOTAL_BUDGET_SECONDS
    for sub in subscriptions:
        # One slow endpoint shouldn't spend the whole budget and leave the
        # caller waiting on the rest. Notifications are a bonus — the
        # status page still updates by polling without them.
        if monotonic() > deadline:
            logger.warning("Push budget exhausted for %s; skipping remaining", context_label)
            return
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_CLAIM_EMAIL},
                timeout=PUSH_TIMEOUT_SECONDS,
            )
        except WebPushException as err:
            status_code = getattr(err.response, "status_code", None)
            if status_code == 410:
                sub.delete()
            else:
                logger.warning("Push failed for %s: %s", context_label, err)
        except Exception:
            logger.exception("Unexpected error sending push for %s", context_label)


# Outbound calls to third parties (Resend, browser push services) run
# inline on request paths — including the Razorpay webhook and the callback
# a student's browser is sitting on. `requests` defaults to NO timeout, so
# a peer that accepts the connection and then goes quiet blocks the worker
# until Cloud Run's request timeout (minutes, not seconds). With
# gunicorn --workers 2, two such calls take a whole instance out of
# service. Everything below is therefore explicitly bounded.
#
# These can't simply be moved to a background thread: this service runs
# with Cloud Run's default CPU throttling, so work started during a request
# is not guaranteed CPU once the response is sent. Bounding the calls is
# the fix that actually holds here; a real queue (Cloud Tasks) is the
# longer-term answer.
EMAIL_TIMEOUT_SECONDS = 5
PUSH_TIMEOUT_SECONDS = 5
# Ceiling across ALL of one recipient's subscriptions, since the loop below
# is sequential and a device can hold several.
PUSH_TOTAL_BUDGET_SECONDS = 12


def send_resend_email(payload):
    """POST one email to Resend, with a timeout.

    Deliberately not send_resend_email(): that SDK calls
    requests.request(...) with no timeout and offers no way to supply one,
    which is exactly the unbounded block described above. The API itself is
    a single JSON POST, so calling it directly costs nothing and gives us
    the timeout.

    Raises on failure. Callers decide whether that is fatal (send_otp_email,
    where a silently unsent code is worse than an error) or best-effort
    (the order emails, which must never fail a payment transition)."""
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {settings.RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=EMAIL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def send_order_push(order, title, body):
    """A student's status page works fine over polling alone (see
    order-status.js) — this is a bonus that fires a real system
    notification even if they've closed the tab/app."""
    _send_webpush_to(
        order.push_subscriptions.all(), title, body,
        f"/order-status.html?code={order.order_code}", f"order {order.order_code}",
    )


def send_owner_push(restaurant, title, body):
    """Fired the instant a new order's payment is confirmed (see
    RazorpayWebhookView) — that's the same moment it first becomes
    visible/actionable on the dashboard (see MyOrdersView), so a real
    system notification here means an owner doesn't have to keep the
    dashboard tab open to know a new order just came in."""
    _send_webpush_to(
        restaurant.push_subscriptions.all(), title, body,
        "/dashboard.html", f"restaurant {restaurant.slug}",
    )


def get_razorpay_client():
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def rupees_to_paise(amount):
    """Decimal rupees -> integer paise, the unit Razorpay's API expects.
    Decimal arithmetic throughout avoids float rounding surprises on money."""
    return int((amount * 100).to_integral_value())
from .serializers import (
    LocationSerializer,
    MenuItemCreateSerializer,
    OrderSerializer,
    OwnerMenuItemSerializer,
    OwnerOrderSerializer,
    OwnerRestaurantSerializer,
    RestaurantDetailSerializer,
    RestaurantListSerializer,
)


def resolve_item_price(menu_item, size_label):
    """Returns (unit_price, error_message). error_message is None on success.
    Mirrors the frontend's price display contract: price_tiers takes
    priority over price_half/price_full, which takes priority over price."""
    size_label = (size_label or "").strip()

    if menu_item.price_tiers:
        if size_label not in menu_item.price_tiers:
            valid = ", ".join(menu_item.price_tiers.keys())
            return None, f"'{menu_item.name}' needs a size: {valid}."
        return menu_item.price_tiers[size_label], None

    if menu_item.price_half is not None or menu_item.price_full is not None:
        if size_label == "Half" and menu_item.price_half is not None:
            return menu_item.price_half, None
        if size_label == "Full" and menu_item.price_full is not None:
            return menu_item.price_full, None
        return None, f"'{menu_item.name}' needs a size: Half, Full."

    if menu_item.price is None:
        return None, f"'{menu_item.name}' doesn't have a price set yet."
    return menu_item.price, None


def get_owned_restaurant(user):
    """Return the restaurant owned by this user, or None."""
    return Restaurant.objects.filter(owner=user).first()


def get_order_for_owner(user, order_code):
    """Return (order, error_response). error_response is None on success.
    Scopes the lookup to the caller's own restaurant so one owner can
    never see or act on another restaurant's orders."""
    restaurant = get_owned_restaurant(user)
    if restaurant is None:
        return None, Response(
            {"detail": "No restaurant linked to this account"},
            status=status.HTTP_404_NOT_FOUND,
        )
    order = Order.objects.filter(order_code=order_code.upper(), restaurant=restaurant).first()
    if order is None:
        return None, Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)
    return order, None


def get_valid_location_or_error(request):
    """Return (location_slug, None) or (None, error_response) for the
    required ?location= query param."""
    location_slug = request.query_params.get("location")
    if not location_slug:
        return None, Response(
            {"detail": "The 'location' query parameter is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not Location.objects.filter(slug=location_slug).exists():
        return None, Response(
            {"detail": f"Unknown location '{location_slug}'."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return location_slug, None


class LocationListView(ListAPIView):
    queryset = Location.objects.all().order_by("name")
    serializer_class = LocationSerializer


class RestaurantListView(ListAPIView):
    serializer_class = RestaurantListSerializer

    def get_queryset(self):
        location_slug, error = get_valid_location_or_error(self.request)
        if error is not None:
            return Restaurant.objects.none()
        return Restaurant.objects.filter(location__slug=location_slug).order_by("name")

    def list(self, request, *args, **kwargs):
        _, error = get_valid_location_or_error(request)
        if error is not None:
            return error
        return super().list(request, *args, **kwargs)


class RestaurantDetailView(RetrieveAPIView):
    # select_related("location") folds what would otherwise be a second
    # query (for RestaurantDetailSerializer.location) into the same query
    # via a SQL JOIN — one less round-trip on the page a student hits most.
    queryset = Restaurant.objects.select_related("location")
    serializer_class = RestaurantDetailSerializer
    lookup_field = "slug"


class SearchView(APIView):
    def get(self, request):
        location_slug, error = get_valid_location_or_error(request)
        if error is not None:
            return error

        query = request.query_params.get("q", "").strip()
        if not query:
            return Response([])

        matching_items = MenuItem.objects.filter(
            is_permanently_active=True,
            is_available_today=True,
            restaurant__location__slug=location_slug,
        ).filter(Q(name__icontains=query) | Q(category__icontains=query))

        counts = {}
        for item in matching_items.select_related("restaurant"):
            restaurant = item.restaurant
            key = restaurant.id
            if key not in counts:
                counts[key] = {
                    "restaurant_name": restaurant.name,
                    "restaurant_slug": restaurant.slug,
                    "matching_item_count": 0,
                }
            counts[key]["matching_item_count"] += 1

        return Response(list(counts.values()))


class LoginView(APIView):
    """Returns a bearer token rather than relying on a session cookie —
    the frontend (Vercel) and this backend (Render) are different domains,
    and a session+CSRF-cookie scheme can't work across that: JS on the
    frontend can never read a cookie the backend set (cookies are scoped
    to the domain that set them, regardless of SameSite), so every
    state-changing request would fail CSRF validation. A token sent as a
    normal Authorization header has no such dependency."""

    throttle_classes = [ScopedRateThrottle, LoginIdentifierThrottle]
    throttle_scope = "login"

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")
        user = authenticate(request, username=username, password=password)
        if user is None:
            return Response(
                {"detail": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED
            )
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"detail": "Logged in", "token": token.key})


class LogoutView(APIView):
    """Shared by both owners and students — logging out is just discarding
    the bearer token either way, nothing account-type-specific about it."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.user.auth_token.delete()
        return Response({"detail": "Logged out"})


def consume_otp(email, submitted_code):
    """Verify a submitted OTP for `email`. Returns (ok, error_detail).

    Two things here are deliberate. The attempts counter is incremented
    with an F() expression so it happens inside the database — a Python
    read-modify-write lets concurrent wrong guesses all read the same
    value and write back the same +1, which quietly defeats MAX_ATTEMPTS
    (the only per-code guard there is; the request throttle is per-IP).

    And the code itself is compared with compare_digest rather than !=,
    so the comparison doesn't return early on the first wrong byte."""
    otp_row = (
        EmailOTP.objects.filter(email=email, consumed=False)
        .order_by("-created_at")
        .first()
    )
    if otp_row is None or otp_row.is_expired or otp_row.attempts >= EmailOTP.MAX_ATTEMPTS:
        return False, "That code has expired. Request a new one."

    if not secrets.compare_digest(str(otp_row.code), str(submitted_code)):
        EmailOTP.objects.filter(pk=otp_row.pk).update(attempts=F("attempts") + 1)
        return False, "Incorrect code."

    # Conditional on consumed=False so a code replayed twice in parallel
    # is only ever accepted once.
    claimed = EmailOTP.objects.filter(pk=otp_row.pk, consumed=False).update(consumed=True)
    if not claimed:
        return False, "That code has already been used. Request a new one."
    return True, None


GENERIC_OTP_SENT = "If that account exists, a login code has been sent to its email."


def generate_otp_code():
    """6-digit code for email verification and OTP login.

    secrets rather than random for the same reason as generate_order_code:
    this is an authentication credential. random's Mersenne Twister state
    can be reconstructed from enough observed output, and an attacker can
    produce observations on demand by requesting codes for an address they
    control — which is exactly what would let them predict the code emailed
    to somebody else."""
    return f"{secrets.randbelow(1_000_000):06d}"


def find_student_by_identifier(identifier, include_unverified=False):
    """A student logs in with either their username or their email — try
    both. student_profile__isnull=False keeps this from ever matching a
    restaurant-owner account that happens to share a username/email.
    Unverified accounts (registered but the email code was never entered)
    are excluded by default — they're not real students yet, so login and
    "resend a login code" shouldn't be able to find them. The one caller
    that needs the opposite is finishing registration itself, where the
    account being looked up *is* the unverified one being verified."""
    if not identifier:
        return None
    qs = User.objects.filter(
        Q(username__iexact=identifier) | Q(email__iexact=identifier),
        student_profile__isnull=False,
    )
    if not include_unverified:
        qs = qs.filter(is_active=True)
    return qs.first()


SITE_URL = "https://www.cufood.in"


ORDER_FOOTER_NOTE = "You're getting this because you placed an order on CUFood."
ACCOUNT_FOOTER_NOTE = "You're getting this because someone asked to sign in to CUFood with this address."


def render_branded_email(body_html, footer_note=ORDER_FOOTER_NOTE):
    """Wraps an email body in the CUFood header/footer. Table-based and
    inline-styled on purpose — email clients (Gmail especially) strip
    <style> blocks and ignore most modern CSS, so anything that isn't
    inline on the element won't survive. The logo is a pre-composited
    PNG with the dark background baked in rather than a transparent one
    on a coloured cell: the wordmark's "CU" is white, so on a client that
    drops background colours it would otherwise render invisible."""
    return (
        '<div style="margin:0;padding:24px 12px;background:#f4f4f4;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center"'
        ' style="max-width:520px;width:100%;margin:0 auto;border-collapse:collapse;'
        'background:#ffffff;border-radius:16px;overflow:hidden;'
        'font-family:Helvetica,Arial,sans-serif;">'
        '<tr><td align="center" bgcolor="#060605" style="background:#060605;padding:20px 0;">'
        f'<img src="{SITE_URL}/logo-email.png" width="200" alt="CUFood"'
        ' style="display:block;border:0;outline:none;text-decoration:none;width:200px;height:auto;">'
        '</td></tr>'
        f'<tr><td style="padding:28px 28px 24px;color:#0a0a0a;font-size:15px;line-height:1.6;">{body_html}</td></tr>'
        '<tr><td style="padding:16px 28px 24px;border-top:1px solid #e6e6e6;'
        'color:#6b6b6b;font-size:12px;line-height:1.5;">'
        'CUFood &middot; CU Campus<br>'
        f'{footer_note}'
        '</td></tr>'
        '</table></div>'
    )


def email_button(href, label):
    return (
        f'<a href="{href}" style="display:inline-block;background:#d9531e;color:#ffffff;'
        'text-decoration:none;font-weight:bold;font-size:14px;padding:12px 22px;'
        f'border-radius:999px;">{label}</a>'
    )


def order_items_table(order):
    rows = "".join(
        '<tr>'
        f'<td style="padding:6px 0;color:#0a0a0a;font-size:14px;">{item.quantity}&times; {escape(item.name)}'
        f'{f" ({escape(item.size_label)})" if item.size_label else ""}</td>'
        f'<td align="right" style="padding:6px 0;color:#6b6b6b;font-size:14px;">'
        f'&#8377;{item.unit_price * item.quantity}</td>'
        '</tr>'
        for item in order.items.all()
    )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"'
        ' style="border-collapse:collapse;margin:8px 0 4px;">'
        f'{rows}'
        '<tr><td style="padding:10px 0 0;border-top:1px solid #e6e6e6;font-size:14px;'
        '"><strong>Total</strong></td>'
        '<td align="right" style="padding:10px 0 0;border-top:1px solid #e6e6e6;font-size:14px;">'
        f'<strong>&#8377;{order.total_amount}</strong></td></tr>'
        '</table>'
    )


def send_order_confirmation_email(order):
    """Best-effort — never blocks the payment-confirmation flow that
    triggers it (see RazorpayWebhookView). A missing/failed send here
    just means the student doesn't get a receipt in their inbox; the
    order itself is already correctly marked paid either way."""
    if not settings.RESEND_API_KEY or not order.student or not order.student.email:
        return
    instructions_html = (
        f'<p style="margin:14px 0 0;color:#6b6b6b;font-size:14px;">'
        f'<strong style="color:#0a0a0a;">Your note:</strong> {escape(order.special_instructions)}</p>'
        if order.special_instructions else ""
    )
    body = (
        '<p style="margin:0 0 6px;font-size:20px;font-weight:bold;">Order placed &#127881;</p>'
        f'<p style="margin:0 0 20px;color:#6b6b6b;">Thanks for ordering from '
        f'<strong style="color:#0a0a0a;">{escape(order.restaurant.name)}</strong>. '
        'We\'ll let you know as soon as it\'s being prepared.</p>'
        '<p style="margin:0 0 4px;color:#6b6b6b;font-size:13px;">Pickup code</p>'
        f'<p style="margin:0 0 18px;font-size:26px;font-weight:bold;letter-spacing:4px;">'
        f'{escape(order.order_code)}</p>'
        f'{order_items_table(order)}'
        f'{instructions_html}'
        f'<p style="margin:22px 0 0;">'
        f'{email_button(f"{SITE_URL}/order-status.html?code={order.order_code}", "Track your order")}</p>'
    )
    try:
        send_resend_email({
            "from": settings.OTP_FROM_EMAIL,
            "to": [order.student.email],
            "subject": f"Order placed at {order.restaurant.name} (#{order.order_code})",
            "html": render_branded_email(body),
        })
    except Exception:
        logger.exception("Failed to send order confirmation email for %s", order.order_code)


def send_order_rejected_email(order, refunded):
    """Sent when an outlet declines an order (see RejectOrderView). The
    refund wording is driven by whether the Razorpay refund actually went
    through, not by the rejection alone — promising money back in an
    email when the refund call failed would be a lie the student acts on."""
    if not settings.RESEND_API_KEY or not order.student or not order.student.email:
        return
    if refunded:
        money_html = (
            '<p style="margin:0 0 18px;color:#6b6b6b;">'
            f'We\'ve already sent your <strong style="color:#0a0a0a;">&#8377;{order.total_amount}</strong> '
            'back to the way you paid. It usually lands within a few minutes, though your bank can '
            'occasionally take a little longer.</p>'
        )
    else:
        money_html = (
            '<p style="margin:0 0 18px;color:#6b6b6b;">'
            'No payment was taken for this order, so there\'s nothing to refund.</p>'
        )
    body = (
        '<p style="margin:0 0 6px;font-size:20px;font-weight:bold;">Your order was declined</p>'
        f'<p style="margin:0 0 16px;color:#6b6b6b;">Sorry &mdash; '
        f'<strong style="color:#0a0a0a;">{escape(order.restaurant.name)}</strong> '
        f'couldn\'t take order <strong style="color:#0a0a0a;">#{escape(order.order_code)}</strong>. '
        'This usually means they\'ve run out of something or are too busy right now.</p>'
        f'{money_html}'
        f'{order_items_table(order)}'
        f'<p style="margin:22px 0 0;">'
        f'{email_button(f"{SITE_URL}/location-select.html", "Order something else")}</p>'
    )
    try:
        send_resend_email({
            "from": settings.OTP_FROM_EMAIL,
            "to": [order.student.email],
            "subject": f"Order #{order.order_code} was declined — refund on its way"
            if refunded
            else f"Order #{order.order_code} was declined",
            "html": render_branded_email(body),
        })
    except Exception:
        logger.exception("Failed to send rejection email for %s", order.order_code)


def send_otp_email(email, code):
    """The login/verification code. Goes through the same branded shell as
    the order emails — a bare wall of text from an unfamiliar sender is
    exactly what a phishing attempt looks like, and this is the first
    email a new student ever gets from us."""
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY is not configured.")
    body = (
        '<p style="margin:0 0 6px;font-size:20px;font-weight:bold;">Your login code</p>'
        '<p style="margin:0 0 20px;color:#6b6b6b;">Enter this in CUFood to finish signing in.</p>'
        '<p style="margin:0 0 6px;font-size:34px;font-weight:bold;letter-spacing:8px;'
        f'color:#0a0a0a;">{escape(code)}</p>'
        f'<p style="margin:0 0 18px;color:#6b6b6b;font-size:13px;">'
        f'Expires in {EmailOTP.OTP_TTL_MINUTES} minutes.</p>'
        '<p style="margin:0;color:#6b6b6b;font-size:13px;">'
        "Didn't ask for this? You can ignore this email &mdash; nobody can sign "
        'in without the code above.</p>'
    )
    send_resend_email({
        "from": settings.OTP_FROM_EMAIL,
        "to": [email],
        "subject": f"Your CUFood login code is {code}",
        "html": render_branded_email(body, footer_note=ACCOUNT_FOOTER_NOTE),
    })


def student_auth_response(user):
    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "username": user.username, "email": user.email})


class StudentRegisterView(APIView):
    """Step 1 of account creation — username + email + password. Creates
    the account inactive and emails a verification code rather than
    logging the student in immediately: without this, anyone could type
    someone else's email address and the account would just work, with no
    proof they actually own that inbox. StudentVerifyRegistrationView
    (step 2) is what actually activates the account and returns a token.

    A previous unverified attempt at the same username/email is deleted
    first — otherwise someone who registered but never entered the code
    would permanently squat on that username/email, blocking the real
    owner (or themselves, retrying) from ever registering it."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        email = (request.data.get("email") or "").strip().lower()
        password = request.data.get("password") or ""

        if not re.fullmatch(r"[a-zA-Z0-9_.]{3,30}", username):
            return Response(
                {"detail": "Username must be 3-30 characters: letters, numbers, underscores, or dots."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            validate_email(email)
        except DjangoValidationError:
            return Response({"detail": "Enter a valid email address."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_password(password)
        except DjangoValidationError as exc:
            return Response({"detail": " ".join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

        User.objects.filter(
            Q(username__iexact=username) | Q(email__iexact=email),
            student_profile__isnull=False,
            is_active=False,
        ).delete()

        if User.objects.filter(username__iexact=username).exists():
            return Response({"detail": "That username is already taken."}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(email__iexact=email).exists():
            return Response({"detail": "An account already exists for that email."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.create_user(username=username, email=email, password=password, is_active=False)
        except IntegrityError:
            return Response({"detail": "That username or email is already taken."}, status=status.HTTP_400_BAD_REQUEST)
        StudentProfile.objects.create(user=user)

        code = generate_otp_code()
        EmailOTP.issue(email, code)
        try:
            send_otp_email(email, code)
        except Exception:
            logger.exception("Failed to send registration OTP email to %s", email)
            user.delete()
            return Response(
                {"detail": "Could not send a verification code right now. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"detail": "A verification code has been sent to your email."})


class StudentVerifyRegistrationView(APIView):
    """Step 2 — the code from StudentRegisterView. Activates the account
    and logs the student in, same as a normal login would."""

    throttle_classes = [ScopedRateThrottle, LoginIdentifierThrottle]
    throttle_scope = "login"

    def post(self, request):
        identifier = (request.data.get("identifier") or "").strip()
        otp = (request.data.get("otp") or "").strip()

        user = find_student_by_identifier(identifier, include_unverified=True)
        if user is None or user.is_active:
            return Response({"detail": "Invalid or already-verified account."}, status=status.HTTP_400_BAD_REQUEST)

        ok, otp_error = consume_otp(user.email, otp)
        if not ok:
            return Response({"detail": otp_error}, status=status.HTTP_400_BAD_REQUEST)

        user.is_active = True
        user.save(update_fields=["is_active"])
        return student_auth_response(user)


class StudentResendRegistrationOtpView(APIView):
    """Resend the verification code for an account still stuck at step 1
    (e.g. the first email got lost, or the code expired)."""

    throttle_classes = [ScopedRateThrottle, LoginIdentifierThrottle]
    throttle_scope = "login"

    def post(self, request):
        identifier = (request.data.get("identifier") or "").strip()
        user = find_student_by_identifier(identifier, include_unverified=True)
        if user is None or user.is_active:
            return Response({"detail": "Invalid or already-verified account."}, status=status.HTTP_400_BAD_REQUEST)

        code = generate_otp_code()
        EmailOTP.issue(user.email, code)
        try:
            send_otp_email(user.email, code)
        except Exception:
            logger.exception("Failed to resend registration OTP email to %s", user.email)
            return Response(
                {"detail": "Could not send the code right now. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"detail": "A new verification code has been sent to your email."})


class StudentRequestOtpView(APIView):
    """Sends a 6-digit login code to the account's email — used for both
    the "email + OTP" and "username + OTP" login modes (identifier can be
    either; the code always goes to the email on file)."""

    throttle_classes = [ScopedRateThrottle, LoginIdentifierThrottle]
    throttle_scope = "login"

    def post(self, request):
        identifier = (request.data.get("identifier") or "").strip()
        user = find_student_by_identifier(identifier)
        # Deliberately the same answer whether or not the account exists.
        # Returning 404 here turned this endpoint into an account
        # enumerator: anyone could test usernames/emails and learn which
        # ones are registered — which sits oddly next to the care taken
        # below not to echo the address back. The client just moves on to
        # the code-entry step either way; a nonexistent account simply
        # never receives a code to enter.
        if user is None:
            return Response({"detail": GENERIC_OTP_SENT})

        code = generate_otp_code()
        EmailOTP.issue(user.email, code)
        try:
            send_otp_email(user.email, code)
        except Exception:
            logger.exception("Failed to send OTP email to %s", user.email)
            return Response(
                {"detail": "Could not send the code right now. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        # Doesn't echo back the email — the student already knows which
        # inbox they're checking, and this avoids confirming account
        # details for whatever partial identifier they typed.
        return Response({"detail": GENERIC_OTP_SENT})


class StudentLoginView(APIView):
    """Handles all four login modes from one endpoint: identifier is either
    a username or an email, and exactly one of password/otp is provided."""

    throttle_classes = [ScopedRateThrottle, LoginIdentifierThrottle]
    throttle_scope = "login"

    def post(self, request):
        identifier = (request.data.get("identifier") or "").strip()
        password = request.data.get("password")
        otp = (request.data.get("otp") or "").strip()

        user = find_student_by_identifier(identifier)
        if user is None:
            return Response({"detail": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        if password:
            authenticated = authenticate(request, username=user.username, password=password)
            if authenticated is None:
                return Response({"detail": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)
            return student_auth_response(user)

        if otp:
            ok, otp_error = consume_otp(user.email, otp)
            if not ok:
                return Response({"detail": otp_error}, status=status.HTTP_401_UNAUTHORIZED)
            return student_auth_response(user)

        return Response({"detail": "A password or code is required."}, status=status.HTTP_400_BAD_REQUEST)


class StudentMeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not hasattr(request.user, "student_profile"):
            return Response({"detail": "Not a student account."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"username": request.user.username, "email": request.user.email})


class StudentOrdersView(APIView):
    """A logged-in student's own order history — replaces the old
    localStorage-tracked list of order codes now that orders are tied to
    a real account instead of a browser."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not hasattr(request.user, "student_profile"):
            return Response({"detail": "Not a student account."}, status=status.HTTP_404_NOT_FOUND)
        # select_related/prefetch_related are load-bearing, not a
        # micro-optimisation: both serializers read order.restaurant and
        # order.items per row, so without them this is 1 + 2N queries —
        # ~201 for a full page. active-orders.js polls this every 30s.
        orders = (
            Order.objects.filter(student=request.user)
            .select_related("restaurant")
            .prefetch_related("items")
            .order_by("-created_at")[:100]
        )
        return Response(OrderSerializer(orders, many=True).data)


class MyRestaurantView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(OwnerRestaurantSerializer(restaurant).data)


class ToggleRestaurantOpenView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        restaurant.is_open_today = not restaurant.is_open_today
        restaurant.save(update_fields=["is_open_today"])
        return Response(OwnerRestaurantSerializer(restaurant).data)


class UpdateUpiIdView(APIView):
    """Lets an owner set the UPI ID their order earnings get forwarded to
    (see Restaurant.upi_id) — students pay through Razorpay now, not this
    directly. No format validation beyond "looks like a VPA" — restaurants
    know their own UPI ID better than a regex would."""

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        upi_id = (request.data.get("upi_id") or "").strip()
        if upi_id and "@" not in upi_id:
            return Response(
                {"detail": "That doesn't look like a UPI ID (should look like name@bank)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        restaurant.upi_id = upi_id
        restaurant.save(update_fields=["upi_id"])
        return Response(OwnerRestaurantSerializer(restaurant).data)


class ToggleMenuItemTodayView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, item_id):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        item = get_object_or_404(MenuItem, id=item_id, restaurant=restaurant)
        item.is_available_today = not item.is_available_today
        item.save(update_fields=["is_available_today"])
        return Response(OwnerMenuItemSerializer(item).data)


class MenuItemCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = MenuItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(restaurant=restaurant)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MenuItemDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, item_id):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        item = get_object_or_404(MenuItem, id=item_id, restaurant=restaurant)
        serializer = MenuItemCreateSerializer(item, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(OwnerMenuItemSerializer(item).data)

    def delete(self, request, item_id):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        item = get_object_or_404(MenuItem, id=item_id, restaurant=restaurant)
        item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


MAX_ITEM_QUANTITY = 20
# How far ahead a student can schedule a pickup — a same-day "beat the
# lunch rush" window, not an indefinite-future booking system.
MIN_SCHEDULE_LEAD_MINUTES = 10
MAX_SCHEDULE_LEAD_HOURS = 4


def parse_scheduled_for(raw_value):
    """Returns (scheduled_for, error_message). error_message is None on
    success; scheduled_for is None for both a null/absent input (ASAP)
    and no error, so callers must check error_message, not truthiness."""
    if not raw_value:
        return None, None
    scheduled_for = parse_datetime(raw_value)
    if scheduled_for is None:
        return None, "Invalid scheduled time."
    if timezone.is_naive(scheduled_for):
        scheduled_for = timezone.make_aware(scheduled_for, timezone.utc)
    now = timezone.now()
    if scheduled_for < now + timezone.timedelta(minutes=MIN_SCHEDULE_LEAD_MINUTES):
        return None, f"Scheduled pickup must be at least {MIN_SCHEDULE_LEAD_MINUTES} minutes from now."
    if scheduled_for > now + timezone.timedelta(hours=MAX_SCHEDULE_LEAD_HOURS):
        return None, f"Scheduled pickup can't be more than {MAX_SCHEDULE_LEAD_HOURS} hours from now."
    return scheduled_for, None


def create_order_with_unique_code(**fields):
    """Create an Order, retrying if its generated code collides.

    order_code is unique and defaults to a random value, so two orders
    created at the same moment can pick the same code and the second
    insert fails. Without this that IntegrityError reaches the student as
    a 500 at the worst possible moment — right before payment. Each retry
    re-runs the default and gets a fresh code."""
    for attempt in range(5):
        try:
            with transaction.atomic():
                return Order.objects.create(**fields)
        except IntegrityError:
            # Only swallow the collision we know how to fix; anything
            # else (a real constraint problem) should surface.
            if attempt == 4:
                raise
            logger.warning("order_code collision, retrying (attempt %s)", attempt + 1)
    raise IntegrityError("Could not allocate a unique order_code")


class CreateOrderView(APIView):
    """Validates a student's cart server-side (never trust client-sent
    prices), creates the Order + OrderItems in 'placed'/payment 'pending',
    and opens a matching Razorpay Order so the frontend can launch Checkout
    immediately after. Payment itself is confirmed later, server-to-server,
    by RazorpayWebhookView — nothing here or on the student's device marks
    an order as paid.

    Requires a logged-in student account (not a restaurant owner) — the
    order is tied to that account (see Order.student) and student_name is
    taken from the account's username, not typed fresh every time. No
    phone number is collected anymore: it was unverified free text anyway,
    and payment already ties the order to a real phone via Razorpay/UPI."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "orders"

    def post(self, request):
        if not hasattr(request.user, "student_profile"):
            return Response({"detail": "Log in as a student to place an order."}, status=status.HTTP_403_FORBIDDEN)

        restaurant_slug = request.data.get("restaurant_slug")
        raw_items = request.data.get("items")
        special_instructions = (request.data.get("special_instructions") or "").strip()[:300]
        # A per-order display name, editable at checkout — cosmetic only,
        # never the account's actual identity (Order.student below is
        # always the real, logged-in account regardless of what's typed
        # here). Falls back to the account's username if left blank.
        student_name = (request.data.get("student_name") or "").strip()[:100] or request.user.username

        if not restaurant_slug:
            return Response({"detail": "restaurant_slug is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not raw_items or not isinstance(raw_items, list):
            return Response({"detail": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        scheduled_for, schedule_error = parse_scheduled_for(request.data.get("scheduled_for"))
        if schedule_error:
            return Response({"detail": schedule_error}, status=status.HTTP_400_BAD_REQUEST)

        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)

        # Every outlet on campus runs the same real-world hours — this is a
        # campus-wide rule, not a per-restaurant setting, so it's checked
        # independent of the restaurant's own is_open_today toggle. The one
        # exception is bypass_business_hours, a testing-only escape hatch
        # set directly in the database for a specific restaurant (see the
        # field's docstring on the model) — not reachable from the API.
        if not restaurant.bypass_business_hours:
            if not is_within_business_hours():
                return Response(
                    {"detail": "Ordering is only available 10 AM – 6 PM, Monday–Friday."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if scheduled_for and not is_within_business_hours(scheduled_for):
                return Response(
                    {"detail": "That pickup time is outside our 10 AM – 6 PM, Monday–Friday hours."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if not restaurant.is_open_today:
            return Response(
                {"detail": f"{restaurant.name} is closed today."}, status=status.HTTP_400_BAD_REQUEST
            )

        pending_items = []
        subtotal = 0
        for raw_item in raw_items:
            menu_item_id = raw_item.get("menu_item_id")
            quantity = raw_item.get("quantity")
            size_label = (raw_item.get("size_label") or "").strip()

            if not isinstance(quantity, int) or not (1 <= quantity <= MAX_ITEM_QUANTITY):
                return Response(
                    {"detail": "Invalid quantity in cart."}, status=status.HTTP_400_BAD_REQUEST
                )

            # Filtering by restaurant=restaurant here is what enforces
            # "single restaurant per order" at the data level, not just in
            # the frontend UI: an item from any other restaurant 404s.
            menu_item = MenuItem.objects.filter(id=menu_item_id, restaurant=restaurant).first()
            if menu_item is None:
                return Response(
                    {"detail": "One of the items in your cart isn't on this restaurant's menu."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not menu_item.is_permanently_active or not menu_item.is_available_today:
                return Response(
                    {"detail": f"'{menu_item.name}' is no longer available."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            unit_price, error = resolve_item_price(menu_item, size_label)
            if error:
                return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

            subtotal += unit_price * quantity
            pending_items.append(
                OrderItem(
                    menu_item=menu_item,
                    name=menu_item.name,
                    size_label=size_label,
                    unit_price=unit_price,
                    quantity=quantity,
                )
            )

        # total_amount is what's actually charged (subtotal + platform fee)
        # — the restaurant's own payout is total_amount - platform_fee,
        # computed wherever an owner needs to see it (see OwnerOrderSerializer).
        order = create_order_with_unique_code(
            restaurant=restaurant,
            student=request.user,
            student_name=student_name,
            special_instructions=special_instructions,
            total_amount=subtotal + Order.PLATFORM_FEE,
            platform_fee=Order.PLATFORM_FEE,
            scheduled_for=scheduled_for,
        )
        for item in pending_items:
            item.order = order
        OrderItem.objects.bulk_create(pending_items)

        try:
            razorpay_order = get_razorpay_client().order.create({
                "amount": rupees_to_paise(order.total_amount),
                "currency": "INR",
                "receipt": order.order_code,
                "notes": {"order_code": order.order_code, "restaurant_slug": restaurant.slug},
            })
        except Exception:
            # Don't leave an Order row around that can never be paid for —
            # the student sees a normal "try again" error, not a dead order
            # sitting invisibly in pending forever.
            logger.exception("Razorpay order.create failed for %s", order.order_code)
            order.delete()
            return Response(
                {"detail": "Could not start payment. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        order.razorpay_order_id = razorpay_order["id"]
        order.save(update_fields=["razorpay_order_id", "updated_at"])

        data = OrderSerializer(order).data
        data["razorpay_order_id"] = razorpay_order["id"]
        data["razorpay_key_id"] = settings.RAZORPAY_KEY_ID
        return Response(data, status=status.HTTP_201_CREATED)


class RetryPaymentView(APIView):
    """Public — if a student closes the Razorpay Checkout modal without
    paying (or it fails), the order they already have a pickup code for is
    still sitting there in payment 'pending'. Rather than making them
    abandon it and place a whole new order, this hands back the same
    Razorpay order details so Checkout can be reopened for it."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_status"

    def get(self, request, order_code):
        order = get_object_or_404(Order, order_code=order_code.upper())
        order.expire_if_stale()
        if order.payment_status == Order.PAYMENT_EXPIRED:
            return Response(
                {"detail": "This order has expired — please place a new order."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.payment_status != Order.PAYMENT_PENDING:
            return Response(
                {"detail": f"This order is already '{order.payment_status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not order.razorpay_order_id:
            return Response(
                {"detail": "No payment was ever started for this order."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({
            "razorpay_order_id": order.razorpay_order_id,
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
            "amount": order.total_amount,
            "restaurant_name": order.restaurant.name,
            "student_name": order.student_name,
        })


def mark_order_paid(order, razorpay_payment_id):
    """The single place an order becomes PAID. Two independent things can
    report a successful payment — Razorpay's server-to-server webhook and
    the redirect-mode callback the student's own browser is bounced
    through (see RazorpayCallbackView) — and in the app flow both usually
    fire for the same payment. Whichever arrives first wins; the second is
    a no-op, so the restaurant is never pushed the same order twice and
    the student never gets two confirmation emails.

    EXPIRED is accepted alongside PENDING on purpose: expire_if_stale()
    is a UI guard, never a claim that no payment could still land. Returns
    True only if this call is the one that made the transition."""
    if order.payment_status not in (Order.PAYMENT_PENDING, Order.PAYMENT_EXPIRED):
        return False

    # Guards against two callers racing on the same order (webhook and
    # callback landing together): the UPDATE only matches while the row is
    # still un-paid, so exactly one of them gets a non-zero rowcount and
    # goes on to send the notifications.
    claimed = Order.objects.filter(
        pk=order.pk,
        payment_status__in=(Order.PAYMENT_PENDING, Order.PAYMENT_EXPIRED),
    ).update(
        payment_status=Order.PAYMENT_PAID,
        razorpay_payment_id=razorpay_payment_id,
        payment_confirmed_at=timezone.now(),
        updated_at=timezone.now(),
    )
    if not claimed:
        return False

    order.refresh_from_db()
    item_summary = ", ".join(f"{item.quantity}x {item.name}" for item in order.items.all())
    send_owner_push(
        order.restaurant, "New order!",
        f"{item_summary} — ₹{order.total_amount}",
    )
    send_order_confirmation_email(order)
    return True


class RazorpayWebhookView(APIView):
    """Public, unauthenticated — but not unverified. Razorpay's servers
    call this directly the moment a payment actually succeeds, independent
    of the student's browser/device. The signature check below is what
    stops anyone else from being able to POST a fake 'payment succeeded'
    here; nothing is trusted until that passes."""

    permission_classes = [AllowAny]
    authentication_classes = []
    # Deliberately unthrottled. Every webhook arrives from Razorpay's own
    # handful of IPs, so any IP-keyed limit is a single platform-wide
    # bucket — above that many payments a minute Razorpay starts getting
    # 429s and payment confirmation stalls exactly when the platform is
    # busiest. The HMAC signature check below is what makes this endpoint
    # safe to expose; a rate limit adds nothing to that and costs
    # confirmations.

    def post(self, request):
        raw_body = request.body
        signature = request.headers.get("X-Razorpay-Signature", "")

        try:
            get_razorpay_client().utility.verify_webhook_signature(
                raw_body.decode("utf-8"), signature, settings.RAZORPAY_WEBHOOK_SECRET
            )
        except razorpay.errors.SignatureVerificationError:
            logger.warning("Razorpay webhook signature verification failed")
            return Response(status=status.HTTP_400_BAD_REQUEST)

        payload = json.loads(raw_body)
        event = payload.get("event")
        if event != "payment.captured":
            # We only act on capture events; anything else (authorized,
            # failed, refund events we triggered ourselves, etc.) is
            # acknowledged so Razorpay stops retrying it, but ignored.
            return Response(status=status.HTTP_200_OK)

        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        razorpay_order_id = payment_entity.get("order_id")
        razorpay_payment_id = payment_entity.get("id")
        if not razorpay_order_id or not razorpay_payment_id:
            return Response(status=status.HTTP_200_OK)

        order = Order.objects.filter(razorpay_order_id=razorpay_order_id).first()
        if order is None:
            logger.warning("Razorpay webhook for unknown order_id=%s", razorpay_order_id)
            return Response(status=status.HTTP_200_OK)

        # Razorpay can and does redeliver webhooks; mark_order_paid is
        # idempotent, so a redelivery is a no-op rather than a reprocess.
        mark_order_paid(order, razorpay_payment_id)

        return Response(status=status.HTTP_200_OK)


class RazorpayCallbackView(APIView):
    """Where Razorpay sends the student's browser back to after a payment
    made in redirect mode — which is what the Android app uses instead of
    Checkout's handler callback (see openRazorpayCheckout in checkout.js).

    Inside the app, paying by UPI hands control to PhonePe/GPay/Paytm.
    Coming back, the page that opened Checkout may have been torn down, so
    handler/ondismiss can't be relied on to run at all; that's what left
    students on 'Waiting for payment' after they'd actually paid. Redirect
    mode doesn't need the original JS context to survive — Razorpay POSTs
    the result here and we bounce the browser to the status page.

    Unauthenticated by necessity (this is a cross-origin form POST from
    Razorpay's domain, carrying no session or token). The signature check
    is what makes it trustworthy, exactly as in RazorpayWebhookView — a
    forged POST without a valid signature marks nothing as paid."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_status"

    def _redirect(self, order_code, failed=False):
        if not order_code:
            # No idea which order this was about — the orders list still
            # gets them to the right place, rather than a dead end.
            return HttpResponseRedirect(f"{SITE_URL}/my-orders.html")
        # payment=failed is a hint for the status page, not a state: it
        # says "Razorpay told us this attempt failed", so the page can say
        # so immediately instead of spinning until it infers it from age.
        # Nothing about the order's real payment_status depends on it.
        suffix = "&payment=failed" if failed else ""
        return HttpResponseRedirect(
            f"{SITE_URL}/order-status.html?code={quote(order_code)}{suffix}"
        )

    def post(self, request):
        razorpay_order_id = request.data.get("razorpay_order_id") or ""
        razorpay_payment_id = request.data.get("razorpay_payment_id") or ""
        razorpay_signature = request.data.get("razorpay_signature") or ""

        # A failed/cancelled payment posts an error object instead of the
        # three success fields. Nothing to verify or mark — just put the
        # student back on their order, where the retry button lives.
        if not (razorpay_order_id and razorpay_payment_id and razorpay_signature):
            order = Order.objects.filter(
                razorpay_order_id=self._order_id_from_error(request)
            ).first()
            return self._redirect(order.order_code if order else None, failed=True)

        order = Order.objects.filter(razorpay_order_id=razorpay_order_id).first()
        if order is None:
            logger.warning("Razorpay callback for unknown order_id=%s", razorpay_order_id)
            return self._redirect(None)

        try:
            get_razorpay_client().utility.verify_payment_signature({
                "razorpay_order_id": razorpay_order_id,
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_signature": razorpay_signature,
            })
        except razorpay.errors.SignatureVerificationError:
            # Deliberately not marked paid. If the payment was genuine the
            # webhook still confirms it independently, so a student is
            # never stranded by this branch alone.
            logger.warning("Razorpay callback signature failed for %s", order.order_code)
            return self._redirect(order.order_code)

        mark_order_paid(order, razorpay_payment_id)
        return self._redirect(order.order_code)

    @staticmethod
    def _order_id_from_error(request):
        """On failure Razorpay sends an error object instead of the three
        success fields. It arrives as a flattened form POST
        ("error[metadata][order_id]"), but tolerate a real nested dict too
        rather than depending on which encoding shows up."""
        flat = request.data.get("error[metadata][order_id]")
        if flat:
            return flat
        error = request.data.get("error")
        if isinstance(error, dict):
            metadata = error.get("metadata")
            if isinstance(metadata, dict):
                return metadata.get("order_id")
        return None


class OrderStatusView(APIView):
    """Public lookup for a student checking their own order — no login,
    just the 6-char pickup code. Throttled to make brute-force
    enumeration of other students' order codes impractical."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_status"

    def get(self, request, order_code):
        order = get_object_or_404(Order, order_code=order_code.upper())
        order.expire_if_stale()
        return Response(OrderSerializer(order).data)


class SubscribeOrderPushView(APIView):
    """Called from order-status.html once a student grants notification
    permission — stores their browser's Web Push subscription against this
    specific order so send_order_push() (see AcceptOrderView/RejectOrderView/
    MarkOrderReadyView) can reach them even after they close the tab/app.
    No login involved, same as the rest of the order-status flow — anyone
    with the order code can subscribe, which is fine since that's already
    the same amount of access the status page itself grants."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "order_status"

    def post(self, request, order_code):
        order = get_object_or_404(Order, order_code=order_code.upper())
        endpoint = request.data.get("endpoint")
        keys = request.data.get("keys") or {}
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        if not endpoint or not p256dh or not auth:
            return Response({"detail": "Invalid subscription."}, status=status.HTTP_400_BAD_REQUEST)

        PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={"order": order, "p256dh": p256dh, "auth": auth},
        )
        return Response(status=status.HTTP_201_CREATED)


class SubscribeOwnerPushView(APIView):
    """Called from dashboard.js once an owner grants notification
    permission — stores their browser's Web Push subscription against
    their restaurant so send_owner_push() (see RazorpayWebhookView) can
    reach them for every future order, not just while the dashboard tab
    is open. Authenticated, unlike SubscribeOrderPushView above, since
    owners actually have accounts to attach this to."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        endpoint = request.data.get("endpoint")
        keys = request.data.get("keys") or {}
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        if not endpoint or not p256dh or not auth:
            return Response({"detail": "Invalid subscription."}, status=status.HTTP_400_BAD_REQUEST)

        RestaurantPushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={"restaurant": restaurant, "p256dh": p256dh, "auth": auth},
        )
        return Response(status=status.HTTP_201_CREATED)


class MyOrdersView(APIView):
    """Owner's order queue. An unpaid 'placed' order is invisible here —
    the owner never sees or waits on anything unpaid; by the time an order
    shows up, it's already been paid for. Paid-and-placed orders need a
    decision; preparing/ready ones are being tracked; everything else is
    recent history."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        # Same 1 + 2N problem as StudentOrdersView — and this one is
        # polled continuously by every open dashboard.
        orders = (
            Order.objects.filter(restaurant=restaurant)
            .exclude(status=Order.STATUS_PLACED, payment_status=Order.PAYMENT_PENDING)
            .select_related("restaurant")
            .prefetch_related("items")
            .order_by("-created_at")[:100]
        )
        return Response(OwnerOrderSerializer(orders, many=True).data)


class DailySalesView(APIView):
    """Owner-facing sales breakdown for a single day (IST calendar day,
    default today). Only counts orders that were actually paid for —
    payment_status=paid naturally excludes unpaid/expired carts and
    successfully-refunded rejections, which is why we don't also filter
    on Order.status here."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )

        date_str = request.query_params.get("date")
        if date_str:
            try:
                target_date = date.fromisoformat(date_str)
            except ValueError:
                return Response({"detail": "Invalid date, expected YYYY-MM-DD"}, status=400)
        else:
            target_date = timezone.now().astimezone(IST).date()

        day_start = datetime.combine(target_date, time.min, tzinfo=IST)
        day_end = day_start + timedelta(days=1)

        orders = Order.objects.filter(
            restaurant=restaurant,
            payment_status=Order.PAYMENT_PAID,
            created_at__gte=day_start,
            created_at__lt=day_end,
        )

        # revenue must be computed before quantity is aliased below — once an
        # annotation named "quantity" exists, F("quantity") inside the same
        # annotate() resolves to that new annotation instead of the field,
        # which Django rejects (aggregate-of-aggregate).
        items_qs = (
            OrderItem.objects.filter(order__in=orders)
            .values("name")
            .annotate(revenue=Sum(F("unit_price") * F("quantity")))
            .annotate(quantity=Sum("quantity"))
            .order_by("-quantity")
        )
        items = list(items_qs)

        total_orders = orders.count()
        total_revenue = sum((o.total_amount - o.platform_fee for o in orders), Decimal("0.00"))
        most_ordered = items[0] if items else None

        return Response({
            "date": target_date.isoformat(),
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "most_ordered_item": most_ordered,
            "items": items,
        })


class AdminLoginView(APIView):
    """Login for the platform-wide admin analytics view — a Django
    superuser only, completely separate from student accounts and
    restaurant owner accounts (an owner token or student token both fail
    AdminStatsView's is_superuser check below, regardless of how they got
    it). Accepts username or email, same as student login, since there's
    no reason a superuser should have to remember which one they used."""

    throttle_classes = [ScopedRateThrottle, LoginIdentifierThrottle]
    throttle_scope = "login"

    def post(self, request):
        identifier = (request.data.get("identifier") or "").strip()
        password = request.data.get("password") or ""

        user = User.objects.filter(
            Q(username__iexact=identifier) | Q(email__iexact=identifier),
            is_superuser=True,
        ).first()
        if user is None:
            return Response({"detail": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        authenticated = authenticate(request, username=user.username, password=password)
        if authenticated is None:
            return Response({"detail": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "username": user.username})


class AdminStatsView(APIView):
    """Platform-wide analytics for one person: you. Total registered
    students, order/sales figures for today/yesterday/all-time, total
    refunds, and a per-restaurant + per-location breakdown for one
    selected day (defaults to today). "platform_revenue" here is the
    platform_fee actually collected — there's no restaurant commission
    deducted anywhere in the codebase yet, so this doesn't (and can't yet)
    include a commission figure; once that's built, it plugs into this
    same field."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_superuser:
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)

        date_str = request.query_params.get("date")
        if date_str:
            try:
                target_date = date.fromisoformat(date_str)
            except ValueError:
                return Response({"detail": "Invalid date, expected YYYY-MM-DD"}, status=400)
        else:
            target_date = timezone.now().astimezone(IST).date()

        def day_bounds(d):
            start = datetime.combine(d, time.min, tzinfo=IST)
            return start, start + timedelta(days=1)

        today_start, today_end = day_bounds(target_date)
        yesterday_start, yesterday_end = day_bounds(target_date - timedelta(days=1))

        paid_orders = Order.objects.filter(payment_status=Order.PAYMENT_PAID)
        refunded_orders = Order.objects.filter(payment_status=Order.PAYMENT_REFUNDED)

        def summarize(qs):
            agg = qs.aggregate(
                count=Count("id"), total_amount=Sum("total_amount"), platform_fee=Sum("platform_fee")
            )
            return {
                "orders": agg["count"] or 0,
                "total_sales": agg["total_amount"] or Decimal("0.00"),
                "platform_revenue": agg["platform_fee"] or Decimal("0.00"),
            }

        today_orders = paid_orders.filter(created_at__gte=today_start, created_at__lt=today_end)
        yesterday_orders = paid_orders.filter(created_at__gte=yesterday_start, created_at__lt=yesterday_end)

        refund_agg = refunded_orders.aggregate(count=Count("id"), amount=Sum("total_amount"))

        by_restaurant = [
            {
                "restaurant_name": row["restaurant__name"],
                "location_name": row["restaurant__location__name"],
                "orders": row["orders"],
                "total_sales": row["total_sales"],
                "platform_revenue": row["platform_revenue"],
            }
            for row in (
                today_orders.values("restaurant__name", "restaurant__location__name")
                .annotate(orders=Count("id"), total_sales=Sum("total_amount"), platform_revenue=Sum("platform_fee"))
                .order_by("-total_sales")
            )
        ]

        by_location = [
            {"location_name": row["restaurant__location__name"], "orders": row["orders"], "total_sales": row["total_sales"]}
            for row in (
                today_orders.values("restaurant__location__name")
                .annotate(orders=Count("id"), total_sales=Sum("total_amount"))
                .order_by("-total_sales")
            )
        ]

        return Response({
            "date": target_date.isoformat(),
            "total_registered_students": StudentProfile.objects.count(),
            "today": summarize(today_orders),
            "yesterday": summarize(yesterday_orders),
            "all_time": summarize(paid_orders),
            "refunds": {
                "count": refund_agg["count"] or 0,
                "total_amount": refund_agg["amount"] or Decimal("0.00"),
            },
            "by_restaurant": by_restaurant,
            "by_location": by_location,
        })


def claim_order_status(order, expected_status, new_status):
    """Atomically move an order from expected_status to new_status.

    Every owner action below is a check-then-act on a row two dashboard
    tabs (or one double-tapped button) can reach at the same time. Reading
    the row, deciding, then writing leaves a window where both callers see
    the same "still placed" state and both act on it — which for
    RejectOrderView meant both could reach the Razorpay refund call.

    The conditional UPDATE closes that window: it only matches while the
    row is still in expected_status, so exactly one caller gets a non-zero
    rowcount and proceeds. Returns True only for that caller."""
    claimed = Order.objects.filter(pk=order.pk, status=expected_status).update(
        status=new_status, updated_at=timezone.now()
    )
    if not claimed:
        return False
    order.status = new_status
    return True


def stale_transition_response(order_code, expected):
    """Shared 409 for a transition another request already made."""
    order = Order.objects.filter(order_code=order_code).first()
    actual = order.status if order else "unknown"
    return Response(
        {"detail": f"Order is '{actual}', not '{expected}' — it may have just been updated elsewhere."},
        status=status.HTTP_409_CONFLICT,
    )


class AcceptOrderView(APIView):
    """Accepting is the owner's one and only decision point — it says 'yes,
    we can make this' AND starts prep immediately, in one tap. That's safe
    to do in one step because payment is verified (via RazorpayWebhookView,
    not a self-report) before this order was even visible to the owner
    (see MyOrdersView) — there's nothing left to wait on, and nothing for
    the owner to double-check themselves."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, order_code):
        order, error = get_order_for_owner(request.user, order_code)
        if error is not None:
            return error
        if order.payment_status != Order.PAYMENT_PAID:
            return Response(
                {"detail": "This order hasn't been paid yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Claimed rather than checked-then-written so a double-tap can't
        # send the student two "Order accepted" pushes.
        if not claim_order_status(order, Order.STATUS_PLACED, Order.STATUS_PREPARING):
            return stale_transition_response(order_code, Order.STATUS_PLACED)
        order.set_ready_estimate()
        send_order_push(order, "Order accepted", f"{order.restaurant.name} is preparing your order.")
        return Response(OwnerOrderSerializer(order).data)


class RejectOrderView(APIView):
    """Since the order was already paid before the owner ever saw it (see
    MyOrdersView), rejecting a paid order triggers a refund through the
    Razorpay API right here — back to the same account/card the student
    paid with, automatically. Nobody has to remember to send money back
    manually; if the refund call itself fails, the order is deliberately
    left un-rejected so the owner can just try again rather than the order
    silently ending up 'rejected' with no refund actually issued."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, order_code):
        order, error = get_order_for_owner(request.user, order_code)
        if error is not None:
            return error

        # Claim the rejection BEFORE touching Razorpay. The refund below
        # moves real money and is not idempotent on our side, so it must
        # be reachable by exactly one request even if the owner
        # double-taps or has two tabs open. Losing this claim means
        # another request already handled it — do nothing.
        was_paid = order.payment_status == Order.PAYMENT_PAID
        if not claim_order_status(order, Order.STATUS_PLACED, Order.STATUS_REJECTED):
            return stale_transition_response(order_code, Order.STATUS_PLACED)

        if was_paid:
            try:
                # "optimum" attempts an instant refund (small per-refund fee,
                # confirmed with Razorpay directly) rather than "normal"
                # (free, 5-7 business days) — deliberate choice: rejections
                # should be rare, and a student getting their money back
                # same-day after a bad experience (their order got declined)
                # matters more than the small fee.
                refund = get_razorpay_client().payment.refund(
                    order.razorpay_payment_id,
                    {"amount": rupees_to_paise(order.total_amount), "speed": "optimum"},
                )
            except Exception as exc:
                logger.exception("Razorpay refund failed for %s", order.order_code)
                # Razorpay's own error responses are already human-readable
                # (e.g. "refunds are not enabled for this account yet") —
                # surfacing that instead of a generic message is the
                # difference between an owner knowing this is a Razorpay
                # account-level hold (new accounts can't refund until their
                # first settlement clears) versus assuming the app is
                # broken and hammering "try again."
                razorpay_detail = None
                response_body = getattr(exc, "http_body", None)
                if isinstance(response_body, (str, bytes)):
                    try:
                        parsed = json.loads(response_body)
                        razorpay_detail = parsed.get("error", {}).get("description")
                    except (ValueError, AttributeError):
                        razorpay_detail = None
                # The Python SDK doesn't attach http_body — it raises its own
                # error types with Razorpay's description as the sole argument
                # (e.g. "Your account does not have enough balance to carry
                # out the refund operation"). Only trust str(exc) for those
                # types; a network/timeout traceback is not owner-readable.
                if not razorpay_detail and isinstance(
                    exc,
                    (
                        razorpay.errors.BadRequestError,
                        razorpay.errors.GatewayError,
                        razorpay.errors.ServerError,
                    ),
                ):
                    razorpay_detail = str(exc) or None
                detail = (
                    f"Refund failed: {razorpay_detail}"
                    if razorpay_detail
                    else "Could not process the refund right now. This can happen on a brand-new "
                    "Razorpay account before its first settlement clears — check Razorpay's dashboard "
                    "or contact their support if this keeps happening."
                )
                # Hand the claim back. The original behaviour here was to
                # leave the order un-rejected so the owner can just try
                # again rather than it silently ending up 'rejected' with
                # no refund actually issued — that has to be restored
                # explicitly now that the status was claimed up front.
                Order.objects.filter(pk=order.pk, status=Order.STATUS_REJECTED).update(
                    status=Order.STATUS_PLACED, updated_at=timezone.now()
                )
                order.status = Order.STATUS_PLACED
                return Response({"detail": detail}, status=status.HTTP_502_BAD_GATEWAY)
            order.razorpay_refund_id = refund["id"]
            order.payment_status = Order.PAYMENT_REFUNDED

        order.save(update_fields=["status", "payment_status", "razorpay_refund_id", "updated_at"])
        refunded = order.payment_status == Order.PAYMENT_REFUNDED
        send_order_push(
            order, "Order declined",
            f"{order.restaurant.name} couldn't take this order — your payment is being refunded."
            if refunded
            else f"{order.restaurant.name} couldn't take this order.",
        )
        send_order_rejected_email(order, refunded)
        return Response(OwnerOrderSerializer(order).data)


class MarkOrderReadyView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, order_code):
        order, error = get_order_for_owner(request.user, order_code)
        if error is not None:
            return error
        if not claim_order_status(order, Order.STATUS_PREPARING, Order.STATUS_READY):
            return stale_transition_response(order_code, Order.STATUS_PREPARING)
        send_order_push(order, "Ready for pickup!", f"Your order from {order.restaurant.name} is ready — go collect it.")
        return Response(OwnerOrderSerializer(order).data)


class CompleteOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, order_code):
        order, error = get_order_for_owner(request.user, order_code)
        if error is not None:
            return error
        if not claim_order_status(order, Order.STATUS_READY, Order.STATUS_COMPLETED):
            return stale_transition_response(order_code, Order.STATUS_READY)
        return Response(OwnerOrderSerializer(order).data)
