from rest_framework import serializers

from .models import Location, MenuItem, Order, OrderItem, Restaurant


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ["name", "slug", "photo"]


class RestaurantListSerializer(serializers.ModelSerializer):
    logo = serializers.CharField(source="photo")
    is_open_today = serializers.BooleanField(source="is_currently_open", read_only=True)

    class Meta:
        model = Restaurant
        fields = ["id", "name", "slug", "logo", "is_open_today"]


class PublicMenuItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        # Frontend contract: price_tiers (if non-null) takes priority over
        # price_half/price_full, which take priority over plain price.
        # "id" is required so the cart can reference which item was added.
        fields = ["id", "name", "category", "price", "price_half", "price_full", "price_tiers"]


class RestaurantDetailSerializer(serializers.ModelSerializer):
    logo = serializers.CharField(source="photo")
    location = LocationSerializer(read_only=True)
    menu_items = serializers.SerializerMethodField()
    is_open_today = serializers.BooleanField(source="is_currently_open", read_only=True)

    class Meta:
        model = Restaurant
        fields = [
            "id",
            "name",
            "slug",
            "logo",
            "is_open_today",
            "location",
            "contact_number",
            "menu_items",
        ]

    def get_menu_items(self, restaurant):
        available_items = restaurant.menu_items.filter(
            is_permanently_active=True, is_available_today=True
        )
        return PublicMenuItemSerializer(available_items, many=True).data


class OwnerMenuItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        fields = [
            "id",
            "name",
            "category",
            "price",
            "price_half",
            "price_full",
            "price_tiers",
            "is_permanently_active",
            "is_available_today",
        ]


class OwnerRestaurantSerializer(serializers.ModelSerializer):
    logo = serializers.CharField(source="photo")
    menu_items = OwnerMenuItemSerializer(many=True, read_only=True)

    class Meta:
        model = Restaurant
        fields = ["id", "name", "slug", "logo", "is_open_today", "upi_id", "menu_items"]


class MenuItemCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        fields = ["id", "name", "category", "price"]


class OrderItemSerializer(serializers.ModelSerializer):
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ["name", "size_label", "unit_price", "quantity", "subtotal"]

    def get_subtotal(self, item):
        return item.unit_price * item.quantity


class OrderSerializer(serializers.ModelSerializer):
    restaurant_name = serializers.CharField(source="restaurant.name", read_only=True)
    restaurant_slug = serializers.CharField(source="restaurant.slug", read_only=True)
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "order_code",
            "restaurant_name",
            "restaurant_slug",
            "student_name",
            "status",
            "payment_status",
            "payment_confirmed_at",
            "total_amount",
            "platform_fee",
            "estimated_ready_minutes",
            "estimated_ready_at",
            "scheduled_for",
            "created_at",
            "items",
        ]


class OwnerOrderSerializer(OrderSerializer):
    """Same as OrderSerializer plus the student's phone number — only ever
    handed to the restaurant that owns the order, never the public
    order-status lookup. Now just a contact number (e.g. to reach the
    student about an issue) — refunds go through Razorpay automatically on
    reject, not via the owner paying the student back themselves."""

    # What the restaurant actually gets — total_amount is what the student
    # paid, which includes the platform fee that stays with the platform.
    # Showing that raw total_amount to an owner as "your money" would be
    # wrong by exactly platform_fee every time, so this is computed here
    # rather than left for the dashboard to (maybe incorrectly) work out.
    restaurant_payout = serializers.SerializerMethodField()

    class Meta(OrderSerializer.Meta):
        fields = OrderSerializer.Meta.fields + ["student_phone_number", "restaurant_payout"]

    def get_restaurant_payout(self, order):
        return order.total_amount - order.platform_fee
