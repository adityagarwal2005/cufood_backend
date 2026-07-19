from django.urls import path

from . import views

urlpatterns = [
    path("csrf/", views.CSRFView.as_view()),
    path("locations/", views.LocationListView.as_view()),
    path("restaurants/", views.RestaurantListView.as_view()),
    path("restaurants/<slug:slug>/", views.RestaurantDetailView.as_view()),
    path("search/", views.SearchView.as_view()),
    path("login/", views.LoginView.as_view()),
    path("logout/", views.LogoutView.as_view()),
    path("me/restaurant/", views.MyRestaurantView.as_view()),
    path("me/restaurant/toggle-open/", views.ToggleRestaurantOpenView.as_view()),
    path(
        "me/menu-items/<int:item_id>/toggle-today/",
        views.ToggleMenuItemTodayView.as_view(),
    ),
    path("me/menu-items/", views.MenuItemCreateView.as_view()),
    path("me/menu-items/<int:item_id>/", views.MenuItemDeleteView.as_view()),
]
