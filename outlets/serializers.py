from rest_framework import serializers

from .models import Location, MenuItem, Restaurant


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
        fields = ["name", "category", "price", "price_half", "price_full", "price_tiers"]


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
        fields = ["id", "name", "slug", "logo", "is_open_today", "menu_items"]


class MenuItemCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = MenuItem
        fields = ["id", "name", "category", "price"]
