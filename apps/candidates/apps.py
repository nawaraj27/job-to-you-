from django.apps import AppConfig


class CandidatesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.candidates"
    label = "candidates"
    verbose_name = "Candidates"

    def ready(self) -> None:
        # Declares the candidate profile events (SRS §13.2).
        from . import events  # noqa: F401
