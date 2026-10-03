from django.apps import AppConfig


class ApplicationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.applications"
    label = "applications"
    verbose_name = "Applications"

    def ready(self) -> None:
        # Declares the application events (SRS §13.2).
        from . import events  # noqa: F401
