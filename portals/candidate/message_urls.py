"""Candidate messaging routes."""

from django.urls import path

from . import message_views as views

app_name = "messages"

urlpatterns = [
    path("", views.InboxView.as_view(), name="inbox"),
]
