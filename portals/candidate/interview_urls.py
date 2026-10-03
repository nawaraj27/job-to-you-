"""
Candidate interview routes.

`finish/`, `result/`, `join/` and `book/` are matched before nothing else —
every path here is prefixed by a UUID or a literal, so no route can shadow
another.
"""

from django.urls import path

from . import interview_views as views

app_name = "interviews"

urlpatterns = [
    path("", views.InterviewHubView.as_view(), name="hub"),
    path("practice/", views.PracticeStartView.as_view(), name="practice"),
    path("book/<uuid:application_id>/", views.InterviewSlotsView.as_view(), name="book"),
    path("<uuid:public_id>/", views.InterviewSessionView.as_view(), name="session"),
    path("<uuid:public_id>/finish/", views.InterviewFinishView.as_view(), name="finish"),
    path("<uuid:public_id>/result/", views.InterviewResultView.as_view(), name="result"),
    path("<uuid:public_id>/join/", views.InterviewJoinView.as_view(), name="join"),
    path("<uuid:public_id>/transcribe/", views.TranscribeAnswerView.as_view(), name="transcribe"),
]
