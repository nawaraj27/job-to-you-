from django.urls import path

from . import views

app_name = "files"

urlpatterns = [
    path("<uuid:public_id>/", views.FileAccessView.as_view(), name="access"),
]
