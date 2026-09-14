import json
import logging
import secrets
import re
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
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
from django.db.models.functions import TruncDate
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import escape
from py_vapid import Vapid
from pywebpush import WebPushException, webpush
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .throttles import LoginIdentifierThrottle, OrderStatusThrottle
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
    StudentPushSubscription,
    is_within_business_hours,
)

logger = logging.getLogger(__name__)


_vapid_signer = None


def get_vapid_signer():
    """Parse the VAPID private key once, in whatever format it is stored.

    pywebpush's own parsing (Vapid.from_string) only understands a raw or
    DER key. Ours is stored as PEM, so every send raised "Could not
    deserialize key data" before anything left the server — not one
    notification had ever been delivered to a student or an owner. Parsing
    the PEM here and handing pywebpush the ready signer fixes it without
    rotating the key, which would have invalidated every subscription."""
    global _vapid_signer
    if _vapid_signer is None:
        key = settings.VAPID_PRIVATE_KEY.strip()
        _vapid_signer = (
            Vapid.from_pem(key.encode()) if key.startswith("-----BEGIN")
            else Vapid.from_string(private_key=key)
        )
    return _vapid_signer


def _send_webpush_to(subscriptions, title, body, url, context_label, *, tag, kind, ttl):
    """Shared send loop for both push flows below — the only difference
    between a student's per-order subscription and an owner's per-restaurant
    one is what they're stored against, not how sending/cleanup works.
    Best-effort: failures here should never break the status transition
    that triggered them, so every exception is swallowed after logging.
    A 404/410 means the browser/OS revoked that subscription (uninstalled,
    permission revoked, etc.) — deleting it rather than retrying it forever.

    Urgency "high" is what gets a push through to a phone that is asleep:
    Android holds normal-priority messages back while the device dozes,
    which for an outlet with three minutes to accept is the same as never
    sending it."""
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
            response = webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=json.dumps({"title": title, "body": body, "url": url, "tag": tag, "kind": kind}),
                vapid_private_key=get_vapid_signer(),
                vapid_claims={"sub": settings.VAPID_CLAIM_EMAIL},
                timeout=PUSH_TIMEOUT_SECONDS,
                ttl=ttl,
                headers={"Urgency": "high"},
            )
            logger.info("Push sent for %s (%s): HTTP %s", context_label, kind,
                        getattr(response, "status_code", "?"))
        except WebPushException as err:
            status_code = getattr(err.response, "status_code", None)
            if status_code in (404, 410):
                sub.delete()
                logger.info("Removed expired push subscription for %s", context_label)
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
# How long a push service may hold a notification for a phone that is
# asleep or offline before giving up. pywebpush defaults to 0, meaning
# "deliver this instant or drop it", so a locked phone or a closed app could
# simply never get it. An outlet's alert is worthless once its three-minute
# window has closed; a student's order update stays useful for about as
# long as the food does.
OWNER_PUSH_TTL_SECONDS = 300
STUDENT_PUSH_TTL_SECONDS = 3600


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
    """Tell the student about their order on every phone they have signed
    up: those signed up on their account, plus any signed up on this one
    order (the older flow). A phone on both lists is notified once.

    An open status page still updates by polling without this; this is what
    reaches a student whose app is closed."""
    subscriptions = {sub.endpoint: sub for sub in order.push_subscriptions.all()}
    if order.student_id:
        for sub in StudentPushSubscription.objects.filter(user_id=order.student_id):
            subscriptions.setdefault(sub.endpoint, sub)
    _send_webpush_to(
        list(subscriptions.values()), title, body,
        f"/order-status.html?code={order.order_code}", f"order {order.order_code}",
        tag=f"order-{order.order_code}", kind="order_update", ttl=STUDENT_PUSH_TTL_SECONDS,
    )


def send_owner_push(restaurant, title, body, tag):
    """Fired the instant a new order's payment is confirmed (see
    RazorpayWebhookView) — that's the same moment it first becomes
    visible/actionable on the dashboard (see MyOrdersView), so a real
    system notification here means an owner doesn't have to keep the
    dashboard tab open to know a new order just came in."""
    _send_webpush_to(
        restaurant.push_subscriptions.all(), title, body,
        "/dashboard.html", f"restaurant {restaurant.slug}",
        tag=tag, kind="new_order", ttl=OWNER_PUSH_TTL_SECONDS,
    )


# The Razorpay SDK makes its HTTP calls with no timeout at all. A hung
# connection would then hold its thread until gunicorn's 60s worker timeout
# kills the whole worker, taking every other in-flight request down with
# it. Since the refund sweep now rides on dashboard polls, that is a real
# path rather than a theoretical one.
RAZORPAY_TIMEOUT_SECONDS = 15


class _RazorpayTimeoutSession(requests.Session):
    def request(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = RAZORPAY_TIMEOUT_SECONDS
        return super().request(*args, **kwargs)


def get_razorpay_client():
    return razorpay.Client(
        session=_RazorpayTimeoutSession(),
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
    )


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
    # In the order they were added, not alphabetically: blocks are shown as
    # campus knows them (Food Republic, Pentagon, then newer ones like D7),
    # and alphabetical order would put a newly added "D7" first.
    queryset = Location.objects.all().order_by("id")
    serializer_class = LocationSerializer


class RestaurantListView(ListAPIView):
    serializer_class = RestaurantListSerializer

    def get_queryset(self):
        location_slug, error = get_valid_location_or_error(self.request)
        if error is not None:
            return Restaurant.objects.none()
        return Restaurant.objects.filter(location__slug=location_slug, is_listed=True).order_by("name")

    def list(self, request, *args, **kwargs):
        _, error = get_valid_location_or_error(request)
        if error is not None:
            return error
        return super().list(request, *args, **kwargs)


class RestaurantDetailView(RetrieveAPIView):
    # select_related("location") folds what would otherwise be a second
    # query (for RestaurantDetailSerializer.location) into the same query
    # via a SQL JOIN — one less round-trip on the page a student hits most.
    queryset = Restaurant.objects.filter(is_listed=True).select_related("location")
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
            restaurant__is_listed=True,
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
        # Only orders the student actually paid for. A checkout that was
        # abandoned, expired, or declined before payment is not something
        # they placed — it is a dead attempt, and listing it as history
        # would leave them scrolling past rows for food they never bought
        # and were never charged for. PAID and REFUNDED are exactly the
        # orders where money moved: one they got, one they got back.
        #
        # select_related/prefetch_related are load-bearing, not a
        # micro-optimisation: both serializers read order.restaurant and
        # order.items per row, so without them this is 1 + 2N queries —
        # ~201 for a full page. active-orders.js polls this every 30s.
        #
        # That polling also makes this a good place to enforce the outlet's
        # deadline: a student who paid and went back to the home page is
        # still waiting on exactly these orders.
        auto_decline_unanswered_orders(list(
            Order.objects.filter(
                student=request.user,
                status=Order.STATUS_PLACED,
                payment_status=Order.PAYMENT_PAID,
            ).select_related("restaurant")
        ))
        sweep_all_unanswered_orders()
        orders = (
            Order.objects.filter(
                student=request.user,
                payment_status__in=(Order.PAYMENT_PAID, Order.PAYMENT_REFUNDED),
            )
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
        # PATCH means "change what I sent", so a request that doesn't
        # mention upi_id must leave it alone. Reading it with a default of
        # "" meant any malformed or partial PATCH silently blanked the
        # field an outlet's earnings are forwarded to — a value they set
        # once and would have no reason to re-check.
        #
        # An explicitly sent empty string still clears it: that is an
        # owner deliberately removing it, which is theirs to do.
        if "upi_id" not in request.data:
            return Response(
                {"detail": "upi_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
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

        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug, is_listed=True)

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
        platform_fee = Order.platform_fee_for(subtotal)
        order = create_order_with_unique_code(
            restaurant=restaurant,
            student=request.user,
            student_name=student_name,
            special_instructions=special_instructions,
            total_amount=subtotal + platform_fee,
            platform_fee=platform_fee,
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

    throttle_classes = [OrderStatusThrottle]
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
    # The outlet's share, not what the student paid: owners never see the
    # platform fee anywhere else either.
    send_owner_push(
        order.restaurant, "New order!",
        f"{item_summary} — ₹{order.total_amount - order.platform_fee}. "
        f"Accept within {Order.DECISION_WINDOW_MINUTES} minutes.",
        tag=f"new-{order.order_code}",
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

    throttle_classes = [OrderStatusThrottle]
    throttle_scope = "order_status"

    def get(self, request, order_code):
        order = get_object_or_404(Order, order_code=order_code.upper())
        order.expire_if_stale()
        # A student watching their own order shouldn't have to wait for
        # the outlet's dashboard to be open for the deadline to mean
        # anything — this page is polling anyway, so it enforces it too.
        if auto_decline_unanswered_orders([order]):
            order.refresh_from_db()
        return Response(OrderSerializer(order).data)


def parse_push_subscription(data):
    """(endpoint, p256dh, auth) from a browser's PushSubscription JSON, or
    None if it isn't one. Strict because it is stored and later sent to:
    malformed input would otherwise surface as a 500 when it overflows a
    column, or as a failed notification long after the request."""
    if not isinstance(data, dict):
        return None
    endpoint, keys = data.get("endpoint"), data.get("keys")
    if not isinstance(endpoint, str) or not isinstance(keys, dict):
        return None
    if not endpoint.startswith("https://") or len(endpoint) > 500:
        return None
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not all(isinstance(k, str) and 0 < len(k) <= 255 for k in (p256dh, auth)):
        return None
    return endpoint, p256dh, auth


class SubscribeOrderPushView(APIView):
    """Called from order-status.html once a student grants notification
    permission — stores their browser's Web Push subscription against this
    specific order so send_order_push() (see AcceptOrderView/RejectOrderView/
    MarkOrderReadyView) can reach them even after they close the tab/app.
    No login involved, same as the rest of the order-status flow — anyone
    with the order code can subscribe, which is fine since that's already
    the same amount of access the status page itself grants."""

    throttle_classes = [OrderStatusThrottle]
    throttle_scope = "order_status"

    def post(self, request, order_code):
        order = get_object_or_404(Order, order_code=order_code.upper())
        parsed = parse_push_subscription(request.data)
        if parsed is None:
            return Response({"detail": "Invalid subscription."}, status=status.HTTP_400_BAD_REQUEST)
        endpoint, p256dh, auth = parsed

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
        parsed = parse_push_subscription(request.data)
        if parsed is None:
            return Response({"detail": "Invalid subscription."}, status=status.HTTP_400_BAD_REQUEST)
        endpoint, p256dh, auth = parsed

        RestaurantPushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={"restaurant": restaurant, "p256dh": p256dh, "auth": auth},
        )
        return Response(status=status.HTTP_201_CREATED)


class StudentPushSubscribeView(APIView):
    """Signs a student's phone up for updates on all of their orders (see
    StudentPushSubscription). The app calls this on every open once
    notifications are allowed, so a subscription the browser rotates is
    picked up without the student noticing. Keyed on the endpoint, so it is
    idempotent and moves a shared phone to whoever is signed in now."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not hasattr(request.user, "student_profile"):
            return Response({"detail": "Not a student account."}, status=status.HTTP_404_NOT_FOUND)
        parsed = parse_push_subscription(request.data)
        if parsed is None:
            return Response({"detail": "Invalid subscription."}, status=status.HTTP_400_BAD_REQUEST)
        endpoint, p256dh, auth = parsed

        StudentPushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={"user": request.user, "p256dh": p256dh, "auth": auth},
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
        # Today only (IST, the campus's day). Yesterday's orders are not
        # something an outlet can act on — at 3pm nobody is going to start
        # cooking an order placed at 10am — and leaving them on the board
        # buries the ones that still matter. History lives in the sales
        # report, not the live queue.
        day_start = datetime.combine(
            timezone.now().astimezone(IST).date(), time.min, tzinfo=IST
        )

        # Same 1 + 2N problem as StudentOrdersView — and this one is
        # polled continuously by every open dashboard.
        orders = list(
            Order.objects.filter(restaurant=restaurant, created_at__gte=day_start)
            .exclude(status=Order.STATUS_PLACED, payment_status=Order.PAYMENT_PENDING)
            .select_related("restaurant")
            .prefetch_related("items")
            .order_by("-created_at")[:100]
        )

        # The sweep is deliberately NOT limited to today. The day filter
        # above is about what an owner should be looking at; this is about
        # money that has been taken and not yet answered for, and those
        # are different questions. Scoping the sweep to today too meant an
        # order that survived past midnight — student paid, closed the
        # tab, nobody opened the dashboard again that day — was never
        # looked at again, stranding their payment with no food and no
        # refund. Rare, and the worst outcome the system can produce, so
        # it is swept regardless of age.
        unanswered = list(
            Order.objects.filter(
                restaurant=restaurant,
                status=Order.STATUS_PLACED,
                payment_status=Order.PAYMENT_PAID,
            ).select_related("restaurant").prefetch_related("items")
        )
        if auto_decline_unanswered_orders(unanswered):
            orders = list(
                Order.objects.filter(restaurant=restaurant, created_at__gte=day_start)
                .exclude(status=Order.STATUS_PLACED, payment_status=Order.PAYMENT_PENDING)
                .select_related("restaurant")
                .prefetch_related("items")
                .order_by("-created_at")[:100]
            )
        sweep_all_unanswered_orders()
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

        # Anything still overdue straight after a forced sweep is an order
        # whose refund is failing — almost always the Razorpay balance. A
        # student is owed that money, so it is listed rather than buried in
        # a log line.
        sweep_all_unanswered_orders(force=True)
        stuck_refunds = [
            {
                "order_code": o.order_code,
                "restaurant_name": o.restaurant.name,
                "total_amount": o.total_amount,
                "paid_at": o.payment_confirmed_at or o.created_at,
            }
            for o in overdue_unanswered_orders().select_related("restaurant").order_by("created_at")[:50]
        ]

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
            "stuck_refunds": stuck_refunds,
            # Devices signed up for new-order alerts, per outlet. An outlet
            # at zero only finds out about orders by staring at the
            # dashboard, and anything it misses for three minutes is
            # declined and refunded.
            "outlet_alerts": [
                {"restaurant_name": r.name, "devices": r.devices}
                for r in Restaurant.objects.filter(is_listed=True).annotate(devices=Count("push_subscriptions"))
                .order_by("devices", "name")
            ],
        })


class AdminReportView(APIView):
    """Superuser-only sales report over an arbitrary date range.

    AdminStatsView answers "how is today going"; this answers "how did
    outlet X do between two dates", which is a different question and
    needs different definitions:

      successful  payment_status=paid AND the outlet accepted it
                  (preparing / ready / completed). These are the orders
                  that earned money.
      rejected    the outlet declined it. Counted, not summed, because
                  the interesting number is how often it happens — each
                  one is a refund that costs the platform its fee.
      awaiting    paid but still undecided. Normally zero for a past
                  range; surfaced anyway so the three buckets add up to
                  every paid order and the report can be trusted.

    Dates are IST calendar days (the campus's day), not UTC, so "27th"
    means what the outlet thinks it means.

    Deliberately two aggregate queries plus one for items regardless of
    how many restaurants or days are in range — building this per
    restaurant would be a query per row on the one page that reads the
    whole order table."""

    permission_classes = [IsAuthenticated]

    ACCEPTED_STATUSES = (Order.STATUS_PREPARING, Order.STATUS_READY, Order.STATUS_COMPLETED)
    MAX_RANGE_DAYS = 400

    def get(self, request):
        if not request.user.is_superuser:
            return Response({"detail": "Not authorized."}, status=status.HTTP_403_FORBIDDEN)

        today = timezone.now().astimezone(IST).date()
        try:
            end_date = (
                date.fromisoformat(request.query_params["end"])
                if request.query_params.get("end") else today
            )
            start_date = (
                date.fromisoformat(request.query_params["start"])
                if request.query_params.get("start") else end_date - timedelta(days=6)
            )
        except ValueError:
            return Response({"detail": "Invalid date, expected YYYY-MM-DD."},
                            status=status.HTTP_400_BAD_REQUEST)

        if start_date > end_date:
            start_date, end_date = end_date, start_date
        if (end_date - start_date).days > self.MAX_RANGE_DAYS:
            return Response(
                {"detail": f"Range too large — {self.MAX_RANGE_DAYS} days maximum."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Half-open [start, end+1) so the end date is included in full.
        range_start = datetime.combine(start_date, time.min, tzinfo=IST)
        range_end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=IST)
        in_range = Order.objects.filter(created_at__gte=range_start, created_at__lt=range_end)

        buckets = {}

        def bucket(restaurant_id, name, location, upi_id):
            return buckets.setdefault(restaurant_id, {
                "restaurant_id": restaurant_id,
                "restaurant_name": name,
                "location_name": location or "",
                "upi_id": upi_id or "",
                "successful_orders": 0,
                "picked_up": 0,
                "total_sales": Decimal("0.00"),
                "platform_revenue": Decimal("0.00"),
                "rejected_orders": 0,
                "refunded_amount": Decimal("0.00"),
                "awaiting_decision": 0,
                "_days": {},
            })

        accepted = (
            in_range.filter(payment_status=Order.PAYMENT_PAID, status__in=self.ACCEPTED_STATUSES)
            .annotate(day=TruncDate("created_at", tzinfo=IST))
            .values("restaurant_id", "restaurant__name", "restaurant__location__name",
                    "restaurant__upi_id", "day")
            .annotate(
                orders=Count("id"),
                sales=Sum("total_amount"),
                fees=Sum("platform_fee"),
                completed=Count("id", filter=Q(status=Order.STATUS_COMPLETED)),
            )
        )
        for row in accepted:
            b = bucket(row["restaurant_id"], row["restaurant__name"],
                       row["restaurant__location__name"], row["restaurant__upi_id"])
            b["successful_orders"] += row["orders"]
            b["picked_up"] += row["completed"]
            b["total_sales"] += row["sales"] or Decimal("0.00")
            b["platform_revenue"] += row["fees"] or Decimal("0.00")
            day = b["_days"].setdefault(row["day"], {
                "date": row["day"].isoformat(), "orders": 0,
                "sales": Decimal("0.00"), "fees": Decimal("0.00"), "items": {},
            })
            day["orders"] += row["orders"]
            day["sales"] += row["sales"] or Decimal("0.00")
            day["fees"] += row["fees"] or Decimal("0.00")

        # Rejections and still-undecided orders, in one pass.
        others = (
            in_range.filter(
                Q(status=Order.STATUS_REJECTED)
                | Q(payment_status=Order.PAYMENT_PAID, status=Order.STATUS_PLACED)
            )
            .values("restaurant_id", "restaurant__name", "restaurant__location__name",
                    "restaurant__upi_id", "status")
            .annotate(orders=Count("id"), refunded=Sum("total_amount"))
        )
        for row in others:
            b = bucket(row["restaurant_id"], row["restaurant__name"],
                       row["restaurant__location__name"], row["restaurant__upi_id"])
            if row["status"] == Order.STATUS_REJECTED:
                b["rejected_orders"] += row["orders"]
                b["refunded_amount"] += row["refunded"] or Decimal("0.00")
            else:
                b["awaiting_decision"] += row["orders"]

        # What was actually sold, per outlet per day.
        items = (
            OrderItem.objects.filter(
                order__created_at__gte=range_start,
                order__created_at__lt=range_end,
                order__payment_status=Order.PAYMENT_PAID,
                order__status__in=self.ACCEPTED_STATUSES,
            )
            .annotate(day=TruncDate("order__created_at", tzinfo=IST))
            .values("order__restaurant_id", "day", "name")
            .annotate(qty=Sum("quantity"), revenue=Sum(F("unit_price") * F("quantity")))
        )
        for row in items:
            b = buckets.get(row["order__restaurant_id"])
            if b is None:
                continue
            day = b["_days"].get(row["day"])
            if day is None:
                continue
            entry = day["items"].setdefault(row["name"], {"name": row["name"], "quantity": 0,
                                                          "revenue": Decimal("0.00")})
            entry["quantity"] += row["qty"] or 0
            entry["revenue"] += row["revenue"] or Decimal("0.00")

        restaurants = []
        for b in buckets.values():
            days = []
            b["food_sales"] = Decimal("0.00")
            b["commission"] = Decimal("0.00")
            for day in sorted(b.pop("_days").values(), key=lambda d: d["date"], reverse=True):
                day["items"] = sorted(day["items"].values(), key=lambda i: -i["quantity"])
                # What to send the outlet for this day: the food the students
                # paid for (their total less the platform fee), less the
                # platform's commission on it. Refunded and undecided orders
                # never reach these sums.
                food = day.pop("sales") - day.pop("fees")
                commission = (food * Order.RESTAURANT_COMMISSION_RATE).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                day.update(sales=food, commission=commission, payout=food - commission)
                b["food_sales"] += food
                b["commission"] += commission
                days.append(day)
            b["days"] = days
            # Built from the rounded daily figures, so the range total is
            # always exactly the sum of the days shown under it.
            b["payout"] = b["food_sales"] - b["commission"]
            b["earnings"] = b["platform_revenue"] + b["commission"]
            restaurants.append(b)
        restaurants.sort(key=lambda r: (-r["total_sales"], r["restaurant_name"]))

        return Response({
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "totals": {
                "successful_orders": sum(r["successful_orders"] for r in restaurants),
                "picked_up": sum(r["picked_up"] for r in restaurants),
                "total_sales": sum((r["total_sales"] for r in restaurants), Decimal("0.00")),
                "platform_revenue": sum((r["platform_revenue"] for r in restaurants), Decimal("0.00")),
                "rejected_orders": sum(r["rejected_orders"] for r in restaurants),
                "refunded_amount": sum((r["refunded_amount"] for r in restaurants), Decimal("0.00")),
                "awaiting_decision": sum(r["awaiting_decision"] for r in restaurants),
                "food_sales": sum((r["food_sales"] for r in restaurants), Decimal("0.00")),
                "commission": sum((r["commission"] for r in restaurants), Decimal("0.00")),
                "payout": sum((r["payout"] for r in restaurants), Decimal("0.00")),
                "earnings": sum((r["earnings"] for r in restaurants), Decimal("0.00")),
            },
            "restaurants": restaurants,
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


def razorpay_error_detail(exc):
    """Pull Razorpay's own human-readable description out of an exception.

    Their messages are already written for a person ("Your account does
    not have enough balance to carry out the refund operation"), and
    showing that instead of a generic failure is the difference between an
    owner knowing this is an account-level hold on Razorpay's side versus
    assuming the app is broken and hammering the button."""
    response_body = getattr(exc, "http_body", None)
    if isinstance(response_body, (str, bytes)):
        try:
            return json.loads(response_body).get("error", {}).get("description")
        except (ValueError, AttributeError):
            pass
    # The Python SDK doesn't attach http_body — it raises its own error
    # types with the description as the sole argument. Only trust str(exc)
    # for those; a network/timeout traceback is not owner-readable.
    if isinstance(exc, (razorpay.errors.BadRequestError,
                        razorpay.errors.GatewayError,
                        razorpay.errors.ServerError)):
        return str(exc) or None
    return None


def release_order_claim(order, claimed_status, revert_to):
    """Undo a claim_order_status() move after the work behind it failed.

    Conditional on the row still being where we put it, so a concurrent
    transition isn't clobbered by an unwind."""
    Order.objects.filter(pk=order.pk, status=claimed_status).update(
        status=revert_to, updated_at=timezone.now()
    )
    order.status = revert_to


def refund_order_payment(order):
    """Refund an order in full. Returns (ok, human_detail_on_failure).

    Shared by the outlet declining an order and by the platform declining
    on their behalf when they never answered, so both paths issue exactly
    the same refund and neither can drift from the other.

    "optimum" attempts an instant refund (small per-refund fee) rather
    than "normal" (free, 5-7 business days) — deliberate: declines should
    be rare, and a student getting their money back the same day after a
    bad experience matters more than the fee.

    Only mutates the in-memory order; the caller decides when to save."""
    try:
        refund = get_razorpay_client().payment.refund(
            order.razorpay_payment_id,
            {"amount": rupees_to_paise(order.total_amount), "speed": "optimum"},
        )
    except Exception as exc:
        logger.exception("Razorpay refund failed for %s", order.order_code)
        detail = razorpay_error_detail(exc)
        return False, (
            f"Refund failed: {detail}" if detail
            else "Could not process the refund right now. This can happen on a brand-new "
                 "Razorpay account before its first settlement clears — check Razorpay's "
                 "dashboard or contact their support if this keeps happening."
        )
    order.razorpay_refund_id = refund["id"]
    order.payment_status = Order.PAYMENT_REFUNDED
    return True, None


# A refund that just failed shouldn't be retried on the very next poll —
# the dashboard polls every few seconds, and the most likely cause of
# failure (an account-level hold on Razorpay, e.g. insufficient balance)
# will still be true a moment later. Waiting between attempts turns a
# persistent failure into a slow retry instead of a hammering loop.
AUTO_DECLINE_RETRY_SECONDS = 60


def auto_decline_unanswered_orders(orders):
    """Decline and refund paid orders the outlet never answered in time.

    Runs off the back of reads rather than a scheduler: the outlet's
    dashboard and the student's status page both poll constantly, so the
    orders that matter are looked at within seconds of their deadline
    without this project needing a cron it otherwise has no use for.

    Every mutation is claimed atomically first (see claim_order_status),
    so it does not matter how many pollers notice the same expired order
    at the same moment — exactly one of them refunds it.

    Returns the order codes it declined."""
    declined = []
    now = timezone.now()
    for order in orders:
        if order.status != Order.STATUS_PLACED or order.payment_status != Order.PAYMENT_PAID:
            continue
        if now <= order.decision_deadline:
            continue
        # Back off after a FAILED attempt, and only then. A failed sweep
        # claims the row and releases it, which is the only thing that
        # writes to a paid order after its deadline has passed — so
        # "updated_at is later than the deadline" identifies exactly that,
        # where a bare "updated_at is recent" would also catch an order
        # that has simply never been attempted and delay a student's
        # refund by up to a minute for no reason.
        if (
            order.updated_at
            and order.updated_at > order.decision_deadline
            and (now - order.updated_at).total_seconds() < AUTO_DECLINE_RETRY_SECONDS
        ):
            continue
        if not claim_order_status(order, Order.STATUS_PLACED, Order.STATUS_REJECTED):
            continue  # somebody else got there first

        ok, _detail = refund_order_payment(order)
        if not ok:
            # Never leave an order rejected without the money going back.
            release_order_claim(order, Order.STATUS_REJECTED, Order.STATUS_PLACED)
            continue

        order.auto_declined = True
        order.save(update_fields=[
            "status", "payment_status", "razorpay_refund_id", "auto_declined", "updated_at",
        ])
        declined.append(order.order_code)
        send_order_push(
            order, "Order not accepted",
            f"{order.restaurant.name} didn't confirm in time — your money is on its way back.",
        )
        send_order_rejected_email(order, refunded=True)
    return declined


# How often any one worker process runs the platform-wide sweep below, and
# how many orders one pass may try to refund.
GLOBAL_SWEEP_INTERVAL_SECONDS = 20
GLOBAL_SWEEP_BATCH = 5
_last_global_sweep = 0.0


def overdue_unanswered_orders():
    """Paid orders nobody answered whose decision window has closed."""
    cutoff = timezone.now() - timedelta(minutes=Order.DECISION_WINDOW_MINUTES)
    return Order.objects.filter(
        status=Order.STATUS_PLACED, payment_status=Order.PAYMENT_PAID,
    ).filter(
        Q(payment_confirmed_at__lte=cutoff)
        | Q(payment_confirmed_at__isnull=True, created_at__lte=cutoff)
    )


def sweep_all_unanswered_orders(force=False):
    """Run auto_decline_unanswered_orders across every outlet, not only the
    one whose dashboard happens to be open.

    The per-outlet sweep in MyOrdersView only fires while THAT outlet's
    dashboard is polling. An outlet that closes the tab, loses signal, or
    never opens the dashboard that day would otherwise hold a student's
    money indefinitely, because nothing else ever looks at those orders.
    With a dozen outlets live, some dashboard or student page is almost
    always polling, so riding along on those polls covers everyone without
    a scheduler.

    Rate-limited per process, so a dozen dashboards polling every few
    seconds cost one small query every GLOBAL_SWEEP_INTERVAL_SECONDS rather
    than hundreds a minute. Two threads racing past the check is harmless:
    the claim inside auto_decline_unanswered_orders keeps each refund
    single-shot. The batch cap bounds how long a poll can be held up when
    refunds are failing (e.g. an empty Razorpay balance); oldest-attempted
    first, so nothing starves.

    Never raises — it rides on read endpoints, and a sweep problem must not
    break the page that triggered it."""
    global _last_global_sweep
    now_mono = monotonic()
    if not force and now_mono - _last_global_sweep < GLOBAL_SWEEP_INTERVAL_SECONDS:
        return
    _last_global_sweep = now_mono
    try:
        batch = list(
            overdue_unanswered_orders()
            .select_related("restaurant")
            .order_by("updated_at")[:GLOBAL_SWEEP_BATCH]
        )
        if batch:
            auto_decline_unanswered_orders(batch)
    except Exception:
        logger.exception("Platform-wide unanswered-order sweep failed")


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
            ok, detail = refund_order_payment(order)
            if not ok:
                # Hand the claim back. An order must never end up
                # 'rejected' with no refund actually issued, so leaving it
                # un-rejected lets the owner simply try again.
                release_order_claim(order, Order.STATUS_REJECTED, Order.STATUS_PLACED)
                return Response({"detail": detail}, status=status.HTTP_502_BAD_GATEWAY)

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
