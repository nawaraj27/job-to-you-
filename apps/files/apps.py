from django.apps import AppConfig


class FilesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.files"
    label = "files"
    verbose_name = "Files"

    def ready(self) -> None:
        # Registers the deployment safety checks (apps/files/checks.py).
        from . import checks  # noqa: F401
