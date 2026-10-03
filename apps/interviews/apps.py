from django.apps import AppConfig


class InterviewsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.interviews"
    label = "interviews"
    verbose_name = "Interviews"

    def ready(self) -> None:
        # Interview events and the pipeline subscriber (SRS §13.2).
        from . import checks, consumers, events  # noqa: F401
