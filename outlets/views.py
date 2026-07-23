import razorpay
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Location, MenuItem, Order, OrderItem, Restaurant
from .serializers import (
    LocationSerializer,
    MenuItemCreateSerializer,
    OrderSerializer,
    OwnerMenuItemSerializer,
    OwnerRestaurantSerializer,
    RestaurantDetailSerializer,
    RestaurantListSerializer,
)


def get_razorpay_client():
    """Returns a configured Razorpay client, or None if keys aren't set yet."""
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return None
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


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
    queryset = Restaurant.objects.all()
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


@method_decorator(ensure_csrf_cookie, name="get")
class CSRFView(APIView):
    """GET this once from the frontend before making POST/PATCH/DELETE
    requests, to receive a csrftoken cookie."""

    def get(self, request):
        return Response({"detail": "CSRF cookie set"})


class LoginView(APIView):
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
        login(request, user)
        return Response({"detail": "Logged in"})


class LogoutView(APIView):
    def post(self, request):
        logout(request)
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


class MenuItemDeleteView(APIView):
    permission_classes = [IsAuthenticated]

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


class CreatePaymentView(APIView):
    """Validates a student's cart server-side (never trust client-sent
    prices), creates our Order + OrderItems in payment_pending state, and
    opens a matching Razorpay order for the frontend Checkout modal."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "orders"

    def post(self, request):
        restaurant_slug = request.data.get("restaurant_slug")
        student_name = (request.data.get("student_name") or "").strip()
        student_uid = (request.data.get("student_uid") or "").strip()
        raw_items = request.data.get("items")

        if not restaurant_slug:
            return Response({"detail": "restaurant_slug is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not student_name:
            return Response({"detail": "Your name is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not student_uid:
            return Response({"detail": "Your UID is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not raw_items or not isinstance(raw_items, list):
            return Response({"detail": "Your cart is empty."}, status=status.HTTP_400_BAD_REQUEST)

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

        client = get_razorpay_client()
        if client is None:
            return Response(
                {"detail": "Online payments aren't set up yet. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            razorpay_order = client.order.create(
                {
                    "amount": int(total_amount * 100),  # paise
                    "currency": "INR",
                    "payment_capture": 1,
                }
            )
        except Exception:
            return Response(
                {"detail": "Could not start payment. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        order = Order.objects.create(
            restaurant=restaurant,
            student_name=student_name,
            student_uid=student_uid,
            total_amount=total_amount,
            razorpay_order_id=razorpay_order["id"],
        )
        for item in pending_items:
            item.order = order
        OrderItem.objects.bulk_create(pending_items)

        return Response(
            {
                "razorpay_order_id": razorpay_order["id"],
                "razorpay_key_id": settings.RAZORPAY_KEY_ID,
                "amount": razorpay_order["amount"],
                "currency": razorpay_order["currency"],
                "restaurant_name": restaurant.name,
                "order_code": order.order_code,
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyPaymentView(APIView):
    """Called by the frontend after Razorpay Checkout succeeds. Verifies the
    HMAC signature server-side before trusting the payment — this is the
    step that actually confirms money moved, not just that the Checkout
    modal closed without an error."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "orders"

    def post(self, request):
        razorpay_order_id = request.data.get("razorpay_order_id")
        razorpay_payment_id = request.data.get("razorpay_payment_id")
        razorpay_signature = request.data.get("razorpay_signature")

        if not all([razorpay_order_id, razorpay_payment_id, razorpay_signature]):
            return Response(
                {"detail": "Missing payment verification fields."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        order = Order.objects.filter(
            razorpay_order_id=razorpay_order_id, status=Order.STATUS_PAYMENT_PENDING
        ).first()
        if order is None:
            return Response(
                {"detail": "Order not found or already processed."},
                status=status.HTTP_404_NOT_FOUND,
            )

        client = get_razorpay_client()
        if client is None:
            return Response(
                {"detail": "Online payments aren't set up yet."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            client.utility.verify_payment_signature(
                {
                    "razorpay_order_id": razorpay_order_id,
                    "razorpay_payment_id": razorpay_payment_id,
                    "razorpay_signature": razorpay_signature,
                }
            )
        except razorpay.errors.SignatureVerificationError:
            order.payment_status = Order.PAYMENT_FAILED
            order.save(update_fields=["payment_status", "updated_at"])
            return Response(
                {"detail": "Payment verification failed."}, status=status.HTTP_400_BAD_REQUEST
            )

        order.payment_status = Order.PAYMENT_PAID
        order.status = Order.STATUS_PLACED
        order.razorpay_payment_id = razorpay_payment_id
        order.save(update_fields=["payment_status", "status", "razorpay_payment_id", "updated_at"])

        return Response(OrderSerializer(order).data, status=status.HTTP_200_OK)


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
    """Owner's order queue. Placed orders need a decision; accepted/ready
    ones are being tracked; everything else is recent history."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        restaurant = get_owned_restaurant(request.user)
        if restaurant is None:
            return Response(
                {"detail": "No restaurant linked to this account"},
                status=status.HTTP_404_NOT_FOUND,
            )
        orders = Order.objects.filter(restaurant=restaurant).exclude(
            status=Order.STATUS_PAYMENT_PENDING
        ).order_by("-created_at")[:100]
        return Response(OrderSerializer(orders, many=True).data)


class AcceptOrderView(APIView):
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
        order.mark_accepted()
        return Response(OrderSerializer(order).data)


class RejectOrderView(APIView):
    """Rejecting a paid order refunds it in full via Razorpay. If the
    refund call itself fails, the order is left untouched (still
    'placed') rather than being marked rejected/refunded when no money
    actually moved — the owner can retry."""

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

        client = get_razorpay_client()
        if client is None:
            return Response(
                {"detail": "Online payments aren't set up yet."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            refund = client.payment.refund(
                order.razorpay_payment_id, {"amount": int(order.total_amount * 100)}
            )
        except Exception:
            return Response(
                {"detail": "Could not process the refund. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        order.status = Order.STATUS_REJECTED
        order.payment_status = Order.PAYMENT_REFUNDED
        order.razorpay_refund_id = refund["id"]
        order.save(
            update_fields=["status", "payment_status", "razorpay_refund_id", "updated_at"]
        )
        return Response(OrderSerializer(order).data)


class MarkOrderReadyView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, order_code):
        order, error = get_order_for_owner(request.user, order_code)
        if error is not None:
            return error
        if order.status != Order.STATUS_ACCEPTED:
            return Response(
                {"detail": f"Order is '{order.status}', not being prepared."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        order.status = Order.STATUS_READY
        order.save(update_fields=["status", "updated_at"])
        return Response(OrderSerializer(order).data)


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
        return Response(OrderSerializer(order).data)
