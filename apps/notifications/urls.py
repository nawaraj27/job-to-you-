from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationsView.as_view(), name="index"),
    path("act/", views.NotificationCommandView.as_view(), name="act"),
    path("<uuid:public_id>/open/", views.OpenView.as_view(), name="open"),
]
