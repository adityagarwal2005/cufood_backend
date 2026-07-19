from django.contrib import admin

from .models import Location, MenuItem, Restaurant


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "location", "is_open_today", "owner")
    list_filter = ("location", "is_open_today")
    fields = (
        "name",
        "slug",
        "photo",
        "location",
        "contact_number",
        "is_open_today",
        "owner",
    )
    prepopulated_fields = {"slug": ("name",)}


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "restaurant",
        "category",
        "price",
        "is_permanently_active",
        "is_available_today",
    )
    list_filter = ("restaurant", "is_permanently_active", "is_available_today")
