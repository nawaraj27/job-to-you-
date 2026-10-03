from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.jobs"
    label = "jobs"
    verbose_name = "Jobs"

    def ready(self) -> None:
        # Declares the job events (SRS §13.2).
        from . import events  # noqa: F401
