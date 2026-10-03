"""
Staff routes (ADR-A5).

`/staff/` rather than under `/admin/`: the Django admin is a different surface
with a different permission model, and mounting purpose-built screens inside it
would make one URL prefix mean two authorities.
"""

from django.urls import path

from . import moderation_views, views

app_name = "staff"

urlpatterns = [
    path("", views.OperationsView.as_view(), name="index"),
    path("verification/", views.VerificationQueueView.as_view(), name="verification"),
    path(
        "verification/documents/<int:document_id>/",
        views.VerificationDocumentView.as_view(),
        name="verification_document",
    ),
    path("anomalies/", views.AnomalyQueueView.as_view(), name="anomalies"),
    path("operations/", views.OperationsView.as_view(), name="operations"),
    path("companies/", moderation_views.CompanyListView.as_view(), name="companies"),
    path("companies/<slug:slug>/", moderation_views.CompanyDetailView.as_view(), name="company"),
    path("people/", moderation_views.PeopleListView.as_view(), name="people"),
    path("people/<int:user_id>/", moderation_views.PersonDetailView.as_view(), name="person"),
    path("team/", moderation_views.StaffTeamView.as_view(), name="team"),
    path(
        "impersonation/stop/",
        moderation_views.ImpersonationStopView.as_view(),
        name="impersonation_stop",
    ),
]
