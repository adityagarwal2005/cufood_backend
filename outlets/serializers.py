from urllib.parse import urlencode

from rest_framework import serializers

from .models import Location, MenuItem, Order, OrderItem, Restaurant


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ["name", "slug", "photo"]


class RestaurantListSerializer(serializers.ModelSerializer):
    logo = serializers.CharField(source="photo")

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
    # Needed right after placing the order — payment happens before the
    # restaurant ever sees it now, not after acceptance.
    restaurant_upi_id = serializers.CharField(source="restaurant.upi_id", read_only=True)
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "order_code",
            "restaurant_name",
            "restaurant_slug",
            "restaurant_upi_id",
            "student_name",
            "status",
            "payment_status",
            "payment_claimed_at",
            "total_amount",
            "estimated_ready_minutes",
            "estimated_ready_at",
            "created_at",
            "items",
        ]


class OwnerOrderSerializer(OrderSerializer):
    """Same as OrderSerializer plus, when relevant, a ready-to-tap UPI
    refund link — only ever handed to the restaurant that owns the order,
    never the public order-status lookup."""

    refund_upi_link = serializers.SerializerMethodField()

    class Meta(OrderSerializer.Meta):
        fields = OrderSerializer.Meta.fields + ["student_upi_id", "refund_upi_link"]

    def get_refund_upi_link(self, order):
        # Only meaningful for a paid order that got rejected — everything
        # else either doesn't need refunding or hasn't been paid at all.
        if order.status != Order.STATUS_REJECTED or order.payment_status != Order.PAYMENT_CLAIMED:
            return None
        if not order.student_upi_id:
            return None
        params = urlencode({
            "pa": order.student_upi_id,
            "pn": order.student_name,
            "am": str(order.total_amount),
            "cu": "INR",
            "tn": f"CUFood refund {order.order_code}",
        })
        return f"upi://pay?{params}"
