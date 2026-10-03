"""
Account settings.

Five pages rather than one long form: settings are looked up, not read through,
and a single scrolling page makes someone hunt for the one switch they came
for. The `settings` namespace is unchanged from the module this replaces.
"""

from django.urls import path

from . import security_views, views

app_name = "settings"

urlpatterns = [
    path("", views.AccountView.as_view(), name="index"),
    path("act/", views.AccountCommandView.as_view(), name="account_act"),
    path("notifications/", views.NotificationsView.as_view(), name="notifications"),
    path("notifications/act/", views.NotificationsCommandView.as_view(), name="notifications_act"),
    path("privacy/", views.PrivacyView.as_view(), name="privacy"),
    path("privacy/act/", views.PrivacyCommandView.as_view(), name="privacy_act"),
    path("appearance/", views.AppearanceView.as_view(), name="appearance"),
    path("appearance/act/", views.AppearanceCommandView.as_view(), name="appearance_act"),
    path("security/", security_views.SecurityView.as_view(), name="security"),
    path("security/act/", security_views.SecurityCommandView.as_view(), name="security_act"),
    path("data/", views.DataView.as_view(), name="data"),
    path("data/act/", views.DataCommandView.as_view(), name="data_act"),
    path("data/export.json", views.ExportView.as_view(), name="export"),
    path("data/export.zip", views.ExportArchiveView.as_view(), name="export_archive"),
]
