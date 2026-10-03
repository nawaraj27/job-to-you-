"""
Resume screens.

The `resume` namespace is unchanged from the module this replaces: a dozen
templates outside this portal link to `resume:builder`, `resume:upload` and
`resume:templates`, and P5 is not a renaming exercise.

Document-scoped routes take a `document_id` (the public UUID) but also answer
without one, falling back to the candidate's primary CV — the builder is
reached from a dozen places that have no document in hand.
"""

from django.urls import path

from . import resume_views as views

app_name = "resume"

urlpatterns = [
    path("start/", views.GuestUploadView.as_view(), name="guest_upload"),
    path("upload/", views.UploadView.as_view(), name="upload"),
    path("upload/receive/", views.UploadReceiveView.as_view(), name="upload_receive"),
    path("upload/<uuid:upload_id>/delete/", views.UploadDeleteView.as_view(), name="upload_delete"),
    path("upload/processing/", views.ProcessingView.as_view(), name="processing"),
    path("upload/progress-chip/", views.ProgressChipView.as_view(), name="progress_chip"),
    path("upload/import/", views.ImportView.as_view(), name="import_act"),
    path("analysis/", views.AnalysisView.as_view(), name="analysis"),
    path("analysis/run/", views.AnalysisRunView.as_view(), name="analysis_run"),
    path("analysis/fix/", views.FindingFixView.as_view(), name="analysis_fix"),
    path("exports/", views.ExportHistoryView.as_view(), name="exports"),
    path("exports/<int:render_id>/", views.DownloadView.as_view(), name="download"),
    path("builder/", views.BuilderHubView.as_view(), name="builder"),
    path("builder/act/", views.DocumentCommandView.as_view(), name="document_act"),
    path("builder/editor/", views.EditorView.as_view(), name="editor_default"),
    path("builder/guided/", views.GuidedBuilderView.as_view(), name="guided_default"),
    path("builder/describe/", views.AssistantView.as_view(), name="ai"),
    path("builder/describe/start/", views.AssistantStartView.as_view(), name="assistant_start"),
    path("builder/print/", views.PrintPreviewView.as_view(), name="print_preview"),
    path("builder/<uuid:document_id>/", views.EditorView.as_view(), name="editor"),
    path("builder/<uuid:document_id>/guided/", views.GuidedBuilderView.as_view(), name="guided"),
    path("builder/<uuid:document_id>/describe/", views.AssistantView.as_view(), name="assistant"),
    path(
        "builder/<uuid:document_id>/target/",
        views.AssistantTargetView.as_view(),
        name="assistant_target",
    ),
    path("builder/<uuid:document_id>/print/", views.PrintPreviewView.as_view(), name="print"),
    path("builder/<uuid:document_id>/act/", views.BuilderCommandView.as_view(), name="builder_act"),
    path(
        "builder/<uuid:document_id>/improve/",
        views.ImprovementPanelView.as_view(),
        name="improvement_panel",
    ),
    path(
        "builder/<uuid:document_id>/job/",
        views.ImprovementJobView.as_view(),
        name="improvement_job",
    ),
    path(
        "builder/<uuid:document_id>/transcript/",
        views.TranscriptView.as_view(),
        name="transcript",
    ),
    path("builder/<uuid:document_id>/live/", views.EditorLiveView.as_view(), name="editor_live"),
    path("builder/<uuid:document_id>/export/", views.ExportView.as_view(), name="export"),
    path("templates/", views.TemplateGalleryView.as_view(), name="templates"),
    path("templates/act/", views.TemplateCommandView.as_view(), name="template_act"),
    path("templates/compare/", views.TemplateCompareView.as_view(), name="template_compare"),
    # Catch-all last, so "act" and "compare" are never read as slugs.
    path("templates/<slug:slug>/", views.TemplateDetailView.as_view(), name="template_detail"),
]
