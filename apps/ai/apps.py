from django.apps import AppConfig


class AiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai"
    label = "ai"
    verbose_name = "AI"

    def ready(self) -> None:
        # Declares the AI events (SRS §13.2).
        from . import events  # noqa: F401
