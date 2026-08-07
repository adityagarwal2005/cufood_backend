from django.urls import path

from . import views

urlpatterns = [
    path("locations/", views.LocationListView.as_view()),
    path("restaurants/", views.RestaurantListView.as_view()),
    path("restaurants/<slug:slug>/", views.RestaurantDetailView.as_view()),
    path("search/", views.SearchView.as_view()),
    path("login/", views.LoginView.as_view()),
    path("logout/", views.LogoutView.as_view()),
    path("me/restaurant/", views.MyRestaurantView.as_view()),
    path("me/restaurant/toggle-open/", views.ToggleRestaurantOpenView.as_view()),
    path("me/restaurant/upi-id/", views.UpdateUpiIdView.as_view()),
    path(
        "me/menu-items/<int:item_id>/toggle-today/",
        views.ToggleMenuItemTodayView.as_view(),
    ),
    path("me/menu-items/", views.MenuItemCreateView.as_view()),
    path("me/menu-items/<int:item_id>/", views.MenuItemDetailView.as_view()),
    path("orders/create/", views.CreateOrderView.as_view()),
    path("orders/<str:order_code>/", views.OrderStatusView.as_view()),
    path("orders/<str:order_code>/retry-payment/", views.RetryPaymentView.as_view()),
    path("webhooks/razorpay/", views.RazorpayWebhookView.as_view()),
    path("me/orders/", views.MyOrdersView.as_view()),
    path("me/orders/<str:order_code>/accept/", views.AcceptOrderView.as_view()),
    path("me/orders/<str:order_code>/reject/", views.RejectOrderView.as_view()),
    path("me/orders/<str:order_code>/ready/", views.MarkOrderReadyView.as_view()),
    path("me/orders/<str:order_code>/complete/", views.CompleteOrderView.as_view()),
]
