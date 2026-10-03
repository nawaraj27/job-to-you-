from django.apps import AppConfig


class MessagingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.messaging"
    label = "messaging"
    verbose_name = "Messaging"

    def ready(self) -> None:
        # Declares the messaging events (SRS §13.2).
        from . import events  # noqa: F401
