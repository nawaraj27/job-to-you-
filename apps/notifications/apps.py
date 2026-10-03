from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    label = "notifications"

    def ready(self) -> None:
        # Importing the module is what registers its @subscribe functions.
        # Without this, the outbox drains into silence.
        from . import (
            consumers,  # noqa: F401
            delivery_consumers,  # noqa: F401
        )
