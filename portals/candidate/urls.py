"""
Candidate screens.

The namespace stays `dashboard` even though the module moved: the templates,
the rail navigation and the user menu all reference `dashboard:index` and
`dashboard:profile`, and renaming routes is not what P3 is for.
"""

from django.urls import path

from . import billing_views, views

app_name = "dashboard"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="index"),
    path("insights/", views.InsightsView.as_view(), name="insights"),
    path("plan/", billing_views.PlanView.as_view(), name="plan"),
    path("plan/start/", billing_views.StartPlanView.as_view(), name="plan_start"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("profile/act/", views.ProfileCommandView.as_view(), name="profile_act"),
]
