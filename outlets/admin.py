from django.contrib import admin

from .models import Location, MenuItem, Order, OrderItem, Restaurant


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


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("menu_item", "name", "size_label", "unit_price", "quantity")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_code",
        "restaurant",
        "student_name",
        "student_uid",
        "status",
        "payment_status",
        "total_amount",
        "created_at",
    )
    list_filter = ("restaurant", "status", "payment_status")
    search_fields = ("order_code", "student_name", "student_uid", "razorpay_payment_id")
    readonly_fields = (
        "razorpay_order_id",
        "razorpay_payment_id",
        "razorpay_refund_id",
        "created_at",
        "updated_at",
    )
    inlines = [OrderItemInline]
