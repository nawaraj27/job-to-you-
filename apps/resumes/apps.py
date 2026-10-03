from django.apps import AppConfig


class ResumesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.resumes"
    label = "resumes"
    verbose_name = "Resumes"

    def ready(self) -> None:
        # Declares the ingestion events and the scan subscriber (SRS §13.2).
        from . import consumers, events  # noqa: F401
