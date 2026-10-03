from django.apps import AppConfig


class MatchingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.matching"
    label = "matching"
    verbose_name = "Matching"

    def ready(self) -> None:
        # Corpus-generation bumps and re-embedding (SRS §10.1, §13.3). Matching
        # owns no tables of its own — recommendations are computed on read
        # (ADR-A21) — so this app is services and subscribers.
        from . import consumers  # noqa: F401
