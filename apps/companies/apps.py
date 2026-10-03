from django.apps import AppConfig


class CompaniesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.companies"
    label = "companies"
    verbose_name = "Companies"

    def ready(self) -> None:
        # Declares the company and team events (SRS §13.2).
        from . import events  # noqa: F401
