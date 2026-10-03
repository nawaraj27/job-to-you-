from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
    label = "billing"
    verbose_name = "Billing"

    def ready(self) -> None:
        # Event declarations and the payment providers, which register
        # themselves on import (SRS §13.2, ADR-A15). No consumer: nothing in
        # billing reacts to another domain's event, and an empty consumer
        # module would only look like one that had been forgotten.
        from . import (
            checks,  # noqa: F401
            events,  # noqa: F401
        )
        from .gateways import esewa, khalti  # noqa: F401
        from .providers import fake, manual  # noqa: F401
