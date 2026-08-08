import json
import logging
import re

import razorpay
from django.conf import settings
from django.contrib.auth import authenticate
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Location, MenuItem, Order, OrderItem, Restaurant, is_within_business_hours

logger = logging.getLogger(__name__)


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
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.user.auth_token.delete()
        return Response({"detail": "Logged out"})


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
    an order as paid."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "orders"

    def post(self, request):
        restaurant_slug = request.data.get("restaurant_slug")
        student_name = (request.data.get("student_name") or "").strip()
        student_phone_number = (request.data.get("student_phone_number") or "").strip()
        raw_items = request.data.get("items")

        if not restaurant_slug:
            return Response({"detail": "restaurant_slug is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not student_name:
            return Response({"detail": "Your name is required."}, status=status.HTTP_400_BAD_REQUEST)
        # Digits only, 10-13 long — covers a plain 10-digit Indian mobile
        # number and one with a +91/91 prefix, without being too strict
        # about exactly how it's formatted.
        if len(re.sub(r"\D", "", student_phone_number)) not in range(10, 14):
            return Response(
                {"detail": "A valid phone number is required, so the restaurant can reach you about your order."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not raw_items or not isinstance(raw_items, list):
            return Response({"detail": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

        scheduled_for, schedule_error = parse_scheduled_for(request.data.get("scheduled_for"))
        if schedule_error:
            return Response({"detail": schedule_error}, status=status.HTTP_400_BAD_REQUEST)

        # Every outlet on campus runs the same real-world hours — this is a
        # campus-wide rule, not a per-restaurant setting, so it's checked
        # here independent of the restaurant's own is_open_today toggle.
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

        restaurant = get_object_or_404(Restaurant, slug=restaurant_slug)
        if not restaurant.is_open_today:
            return Response(
                {"detail": f"{restaurant.name} is closed today."}, status=status.HTTP_400_BAD_REQUEST
            )

        pending_items = []
        total_amount = 0
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

            total_amount += unit_price * quantity
            pending_items.append(
                OrderItem(
                    menu_item=menu_item,
                    name=menu_item.name,
                    size_label=size_label,
                    unit_price=unit_price,
                    quantity=quantity,
                )
            )

        order = Order.objects.create(
            restaurant=restaurant,
            student_name=student_name,
            student_phone_number=student_phone_number,
            total_amount=total_amount,
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
        # transition pending -> paid once; a redelivery after that is a
        # no-op, not a re-processing.
        if order.payment_status == Order.PAYMENT_PENDING:
            order.payment_status = Order.PAYMENT_PAID
            order.razorpay_payment_id = razorpay_payment_id
            order.payment_confirmed_at = timezone.now()
            order.save(update_fields=[
                "payment_status", "razorpay_payment_id", "payment_confirmed_at", "updated_at",
            ])

        return Response(status=status.HTTP_200_OK)


class OrderStatusView(APIView):
    """Public lookup for a student checking their own order — no login,
    just the 6-char pickup code. Throttled to make brute-force
    enumeration of other students' order codes impractical."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "orders"

    def get(self, request, order_code):
        order = get_object_or_404(Order, order_code=order_code.upper())
        return Response(OrderSerializer(order).data)


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
                refund = get_razorpay_client().payment.refund(
                    order.razorpay_payment_id,
                    {"amount": rupees_to_paise(order.total_amount), "speed": "optimum"},
                )
            except Exception:
                logger.exception("Razorpay refund failed for %s", order.order_code)
                return Response(
                    {"detail": "Could not process the refund right now. Please try rejecting again in a moment."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            order.razorpay_refund_id = refund["id"]
            order.payment_status = Order.PAYMENT_REFUNDED

        order.status = Order.STATUS_REJECTED
        order.save(update_fields=["status", "payment_status", "razorpay_refund_id", "updated_at"])
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
