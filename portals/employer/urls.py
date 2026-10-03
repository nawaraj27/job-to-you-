"""
Employer URLs, scoped by company (ADR-A18).

`<company_slug>` is not decoration: CompanyContextMiddleware resolves it against
an active membership on every request, so two browser tabs on two companies
cannot write into each other. The slug is immutable for the life of the company
because it is in every link a team shares.

Order matters. `new/` and `invites/` are matched before `<company_slug>/`, so a
company can never be created with a slug that shadows a route.
"""

from django.urls import path

from . import (
    analytics_views,
    application_views,
    billing_views,
    interview_views,
    job_views,
    message_views,
    talent_views,
    views,
    webhook_views,
)

app_name = "employer"

urlpatterns = [
    path("", views.SwitcherView.as_view(), name="index"),
    path("new/", views.CreateCompanyView.as_view(), name="create"),
    path("invites/<str:token>/", views.AcceptInviteView.as_view(), name="accept_invite"),
    # Company-less links (the site footer): resolved to the member's company.
    path("go/<slug:target>/", views.ShortcutView.as_view(), name="go"),
    path("<slug:company_slug>/", views.OverviewView.as_view(), name="overview"),
    path("<slug:company_slug>/team/", views.TeamView.as_view(), name="team"),
    # `export/` before `<uuid:public_id>/`, so the command can never be read as
    # a candidate identifier.
    path(
        "<slug:company_slug>/analytics/",
        analytics_views.AnalyticsView.as_view(),
        name="analytics",
    ),
    path(
        "<slug:company_slug>/billing/",
        billing_views.BillingView.as_view(),
        name="billing",
    ),
    path(
        "<slug:company_slug>/interviews/",
        interview_views.InterviewListView.as_view(),
        name="interviews",
    ),
    # `slots/` before `<uuid:public_id>/`, so the command can never be read as
    # a session identifier.
    path(
        "<slug:company_slug>/interviews/slots/",
        interview_views.InterviewSlotsView.as_view(),
        name="interview_slots",
    ),
    path(
        "<slug:company_slug>/interviews/<uuid:public_id>/",
        interview_views.InterviewDetailView.as_view(),
        name="interview_detail",
    ),
    path(
        "<slug:company_slug>/interviews/<uuid:public_id>/join/",
        interview_views.InterviewJoinView.as_view(),
        name="interview_join",
    ),
    path(
        "<slug:company_slug>/talent/",
        talent_views.TalentSearchView.as_view(),
        name="talent",
    ),
    path(
        "<slug:company_slug>/talent/export/",
        talent_views.TalentExportView.as_view(),
        name="talent_export",
    ),
    path(
        "<slug:company_slug>/talent/<uuid:public_id>/",
        talent_views.TalentProfileView.as_view(),
        name="talent_profile",
    ),
    path("<slug:company_slug>/profile/", views.ProfileView.as_view(), name="profile"),
    path("<slug:company_slug>/updates/", views.UpdatesView.as_view(), name="updates"),
    path(
        "<slug:company_slug>/verification/",
        views.VerificationView.as_view(),
        name="verification",
    ),
    # Jobs. `new/` precedes `<uuid:public_id>/` so the two can never collide.
    path("<slug:company_slug>/jobs/", job_views.JobListView.as_view(), name="job_list"),
    path("<slug:company_slug>/jobs/new/", job_views.JobCreateView.as_view(), name="job_create"),
    path(
        "<slug:company_slug>/jobs/<uuid:public_id>/",
        job_views.JobDetailView.as_view(),
        name="job_detail",
    ),
    path(
        "<slug:company_slug>/jobs/<uuid:public_id>/edit/",
        job_views.JobEditView.as_view(),
        name="job_edit",
    ),
    path(
        "<slug:company_slug>/jobs/<uuid:public_id>/act/",
        job_views.JobActionView.as_view(),
        name="job_act",
    ),
    path(
        "<slug:company_slug>/jobs/<uuid:public_id>/skills/",
        job_views.JobSkillsView.as_view(),
        name="job_skills",
    ),
    path(
        "<slug:company_slug>/jobs/<uuid:public_id>/questions/",
        job_views.JobQuestionsView.as_view(),
        name="job_questions",
    ),
    path(
        "<slug:company_slug>/jobs/<uuid:public_id>/team/",
        job_views.JobTeamView.as_view(),
        name="job_team",
    ),
    # Applicants. The pipeline board, one application, and what happens to it.
    path(
        "<slug:company_slug>/applicants/",
        application_views.ApplicantListView.as_view(),
        name="applicants",
    ),
    path(
        "<slug:company_slug>/applicants/<uuid:public_id>/",
        application_views.ApplicationDetailView.as_view(),
        name="application_detail",
    ),
    path(
        "<slug:company_slug>/applicants/<uuid:public_id>/act/",
        application_views.ApplicationActionView.as_view(),
        name="application_act",
    ),
    path(
        "<slug:company_slug>/applicants/<uuid:public_id>/offer/",
        application_views.OfferView.as_view(),
        name="application_offer",
    ),
    path(
        "<slug:company_slug>/pipeline/",
        application_views.PipelineSettingsView.as_view(),
        name="pipeline",
    ),
    path("<slug:company_slug>/pools/", application_views.TalentPoolView.as_view(), name="pools"),
    # Messaging (P9). A thread is started from an application, never from a
    # "message anyone" box — that is the interface bulk outreach wants.
    path(
        "<slug:company_slug>/messages/",
        message_views.InboxView.as_view(),
        name="messages",
    ),
    path(
        "<slug:company_slug>/applicants/<uuid:public_id>/message/",
        message_views.StartConversationView.as_view(),
        name="application_message",
    ),
    # Webhooks (P10). A user-supplied URL this server fetches, so the SSRF
    # guard in the service is not optional (SRS §15.4).
    path(
        "<slug:company_slug>/webhooks/",
        webhook_views.WebhookView.as_view(),
        name="webhooks",
    ),
]
