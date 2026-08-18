import json
import logging
import random
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import razorpay
import resend
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError
from django.db.models import F, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from pywebpush import WebPushException, webpush
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

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
    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_CLAIM_EMAIL},
            )
        except WebPushException as err:
            status_code = getattr(err.response, "status_code", None)
            if status_code == 410:
                sub.delete()
            else:
                logger.warning("Push failed for %s: %s", context_label, err)
        except Exception:
            logger.exception("Unexpected error sending push for %s", context_label)


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

    throttle_classes = [ScopedRateThrottle]
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


def find_student_by_identifier(identifier):
    """A student logs in with either their username or their email — try
    both. student_profile__isnull=False keeps this from ever matching a
    restaurant-owner account that happens to share a username/email."""
    if not identifier:
        return None
    return User.objects.filter(
        Q(username__iexact=identifier) | Q(email__iexact=identifier),
        student_profile__isnull=False,
    ).first()


def send_otp_email(email, code):
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY is not configured.")
    resend.api_key = settings.RESEND_API_KEY
    resend.Emails.send({
        "from": settings.OTP_FROM_EMAIL,
        "to": [email],
        "subject": f"Your CUFood login code is {code}",
        "html": (
            f"<p>Your CUFood login code is <strong>{code}</strong>.</p>"
            f"<p>It expires in {EmailOTP.OTP_TTL_MINUTES} minutes. "
            f"If you didn't request this, you can ignore this email.</p>"
        ),
    })


def student_auth_response(user):
    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "username": user.username, "email": user.email})


class StudentRegisterView(APIView):
    """One-time account creation — username + email + password. No email
    verification step on registration itself (keeps sign-up to a single
    screen); choosing "log in with a code" later implicitly proves the
    student actually owns that inbox."""

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

        if User.objects.filter(username__iexact=username).exists():
            return Response({"detail": "That username is already taken."}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(email__iexact=email).exists():
            return Response({"detail": "An account already exists for that email."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.create_user(username=username, email=email, password=password)
        except IntegrityError:
            return Response({"detail": "That username or email is already taken."}, status=status.HTTP_400_BAD_REQUEST)
        StudentProfile.objects.create(user=user)
        return student_auth_response(user)


class StudentRequestOtpView(APIView):
    """Sends a 6-digit login code to the account's email — used for both
    the "email + OTP" and "username + OTP" login modes (identifier can be
    either; the code always goes to the email on file)."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        identifier = (request.data.get("identifier") or "").strip()
        user = find_student_by_identifier(identifier)
        if user is None:
            return Response({"detail": "No account found for that username or email."}, status=status.HTTP_404_NOT_FOUND)

        code = f"{random.randint(0, 999999):06d}"
        EmailOTP.objects.create(email=user.email, code=code)
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
        return Response({"detail": "A login code has been sent to your email."})


class StudentLoginView(APIView):
    """Handles all four login modes from one endpoint: identifier is either
    a username or an email, and exactly one of password/otp is provided."""

    throttle_classes = [ScopedRateThrottle]
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
            otp_row = (
                EmailOTP.objects.filter(email=user.email, consumed=False)
                .order_by("-created_at")
                .first()
            )
            if otp_row is None or otp_row.is_expired or otp_row.attempts >= EmailOTP.MAX_ATTEMPTS:
                return Response({"detail": "That code has expired. Request a new one."}, status=status.HTTP_401_UNAUTHORIZED)
            if otp_row.code != otp:
                otp_row.attempts += 1
                otp_row.save(update_fields=["attempts"])
                return Response({"detail": "Incorrect code."}, status=status.HTTP_401_UNAUTHORIZED)
            otp_row.consumed = True
            otp_row.save(update_fields=["consumed"])
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
        orders = Order.objects.filter(student=request.user).order_by("-created_at")[:100]
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
        order = Order.objects.create(
            restaurant=restaurant,
            student=request.user,
            student_name=request.user.username,
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
    throttle_scope = "orders"

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


class RazorpayWebhookView(APIView):
    """Public, unauthenticated — but not unverified. Razorpay's servers
    call this directly the moment a payment actually succeeds, independent
    of the student's browser/device. The signature check below is what
    stops anyone else from being able to POST a fake 'payment succeeded'
    here; nothing is trusted until that passes."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "orders"

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

        # Idempotent: Razorpay can and does redeliver webhooks. Only ever
        # transition into paid once; a redelivery after that is a no-op,
        # not a re-processing. EXPIRED is included alongside PENDING here
        # on purpose — expire_if_stale() only stops the app from showing a
        # stale checkout, it's never a claim that no payment could still
        # land. If one genuinely did (this webhook firing proves it), that
        # takes priority over the lazy expiry every time; real money moving
        # always wins over a UI-only status.
        if order.payment_status in (Order.PAYMENT_PENDING, Order.PAYMENT_EXPIRED):
            order.payment_status = Order.PAYMENT_PAID
            order.razorpay_payment_id = razorpay_payment_id
            order.payment_confirmed_at = timezone.now()
            order.save(update_fields=[
                "payment_status", "razorpay_payment_id", "payment_confirmed_at", "updated_at",
            ])
            item_summary = ", ".join(f"{item.quantity}x {item.name}" for item in order.items.all())
            send_owner_push(
                order.restaurant, "New order!",
                f"{item_summary} — ₹{order.total_amount}",
            )

        return Response(status=status.HTTP_200_OK)


class OrderStatusView(APIView):
    """Public lookup for a student checking their own order — no login,
    just the 6-char pickup code. Throttled to make brute-force
    enumeration of other students' order codes impractical."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "orders"

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
    throttle_scope = "orders"

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
        orders = Order.objects.filter(restaurant=restaurant).exclude(
            status=Order.STATUS_PLACED, payment_status=Order.PAYMENT_PENDING
        ).order_by("-created_at")[:100]
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
        if order.status != Order.STATUS_PLACED:
            return Response(
                {"detail": f"Order is '{order.status}', not awaiting a decision."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.payment_status != Order.PAYMENT_PAID:
            return Response(
                {"detail": "This order hasn't been paid yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order.mark_preparing()
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
        if order.status != Order.STATUS_PLACED:
            return Response(
                {"detail": f"Order is '{order.status}', not awaiting a decision."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if order.payment_status == Order.PAYMENT_PAID:
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
                response_body = getattr(exc, "http_body", None) or getattr(exc, "message", None)
                if isinstance(response_body, (str, bytes)):
                    try:
                        parsed = json.loads(response_body)
                        razorpay_detail = parsed.get("error", {}).get("description")
                    except (ValueError, AttributeError):
                        razorpay_detail = None
                detail = (
                    f"Refund failed: {razorpay_detail}"
                    if razorpay_detail
                    else "Could not process the refund right now. This can happen on a brand-new "
                    "Razorpay account before its first settlement clears — check Razorpay's dashboard "
                    "or contact their support if this keeps happening."
                )
                return Response({"detail": detail}, status=status.HTTP_502_BAD_GATEWAY)
            order.razorpay_refund_id = refund["id"]
            order.payment_status = Order.PAYMENT_REFUNDED

        order.status = Order.STATUS_REJECTED
        order.save(update_fields=["status", "payment_status", "razorpay_refund_id", "updated_at"])
        send_order_push(
            order, "Order declined",
            f"{order.restaurant.name} couldn't take this order — your payment is being refunded."
            if order.payment_status == Order.PAYMENT_REFUNDED
            else f"{order.restaurant.name} couldn't take this order.",
        )
        return Response(OwnerOrderSerializer(order).data)


class MarkOrderReadyView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, order_code):
        order, error = get_order_for_owner(request.user, order_code)
        if error is not None:
            return error
        if order.status != Order.STATUS_PREPARING:
            return Response(
                {"detail": f"Order is '{order.status}', not being prepared."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order.status = Order.STATUS_READY
        order.save(update_fields=["status", "updated_at"])
        send_order_push(order, "Ready for pickup!", f"Your order from {order.restaurant.name} is ready — go collect it.")
        return Response(OwnerOrderSerializer(order).data)


class CompleteOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, order_code):
        order, error = get_order_for_owner(request.user, order_code)
        if error is not None:
            return error
        if order.status != Order.STATUS_READY:
            return Response(
                {"detail": f"Order is '{order.status}', not ready for pickup."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order.status = Order.STATUS_COMPLETED
        order.save(update_fields=["status", "updated_at"])
        return Response(OwnerOrderSerializer(order).data)
