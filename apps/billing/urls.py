"""Online payment routes (ADR-A58). Mounted at `/billing/`."""

from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("invoices/<str:number>/pay/", views.PayView.as_view(), name="pay"),
    path(
        "return/<slug:gateway>/<slug:reference>/",
        views.ReturnView.as_view(),
        name="return",
    ),
    path(
        "return/<slug:gateway>/<slug:reference>/failed/",
        views.ReturnView.as_view(failed=True),
        name="return_failed",
    ),
    path("payments/<uuid:public_id>/", views.AttemptView.as_view(), name="attempt"),
]
