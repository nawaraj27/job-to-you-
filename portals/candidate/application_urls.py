"""
Candidate application routes.

`app_name = "applications"` is a new namespace: the mock used `jobs:applications`
for a screen that showed sample data, and the real pipeline is its own surface
with its own detail pages. The nav entry moves with it.
"""

from django.urls import path

from . import application_views as views

app_name = "applications"

urlpatterns = [
    path("", views.ApplicationListView.as_view(), name="index"),
    path("visibility/", views.VisibilityView.as_view(), name="visibility"),
    path("<uuid:public_id>/", views.ApplicationDetailView.as_view(), name="detail"),
]
