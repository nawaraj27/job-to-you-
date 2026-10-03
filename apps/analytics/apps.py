from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    label = "analytics"
    verbose_name = "Analytics"

    def ready(self) -> None:
        # The wildcard outbox subscriber (SRS §16.1). Analytics owns no events
        # of its own — it counts everybody else's.
        from . import consumers  # noqa: F401
