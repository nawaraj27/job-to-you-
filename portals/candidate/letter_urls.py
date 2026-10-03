"""
Cover letter routes (P14).

`app_name = "letters"` is a new namespace. The letters had no screen of their
own before this phase — they existed only as a textarea on the apply form — so
there is nothing to keep compatible.

`act/` is matched before `<uuid:public_id>/`, so a command can never be read as
a letter identifier.
"""

from django.urls import path

from . import letter_views as views

app_name = "letters"

urlpatterns = [
    path("", views.LetterListView.as_view(), name="list"),
    path("act/", views.LetterCommandView.as_view(), name="act"),
    path("<uuid:public_id>/", views.LetterDetailView.as_view(), name="detail"),
]
