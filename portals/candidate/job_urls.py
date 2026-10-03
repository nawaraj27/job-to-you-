"""
Candidate job routes.

`app_name = "jobs"` because the navigation, the user menu and the 404 page all
reverse `jobs:list`. The views moved into the portal layer where views belong
(DEVELOPER.md §1.1); the names candidates' links depend on did not move.

`saved/`, `act/` and `company/` are matched before `<slug:slug>/`, so a job can
never be created with a slug that shadows a route.
"""

from django.urls import path

from . import application_views
from . import job_views as views

app_name = "jobs"

urlpatterns = [
    path("", views.JobListView.as_view(), name="list"),
    path("matched/", views.RecommendedJobsView.as_view(), name="recommended"),
    path("saved/", views.SavedJobsView.as_view(), name="saved"),
    path("act/", views.JobActionView.as_view(), name="act"),
    path("company/<slug:slug>/", views.CompanyJobsView.as_view(), name="company"),
    path("company/<slug:slug>/follow/", views.CompanyFollowView.as_view(), name="company_follow"),
    path("<slug:slug>/", views.JobDetailView.as_view(), name="detail"),
    path("<slug:slug>/apply/", application_views.ApplyView.as_view(), name="apply"),
]
