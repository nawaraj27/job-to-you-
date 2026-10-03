"""
Payment configuration checks (ADR-A58).

Two faults are refused at start-up rather than discovered at checkout:

  * **billing.E001** — `PAYMENTS_ENVIRONMENT` is anything but "test". This
    build is integrated against the providers' sandboxes only; a live merchant
    account, its keys and its settlement terms are a business decision nobody
    has made yet.
  * **billing.E002** — a gateway is switched on without what it needs. Left
    alone, the customer would see a button that fails after they press it.
  * **billing.E003** — a configured gateway URL points at a production host
    while the environment is "test".
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import Error, register

#: Hosts that take real money. A test build must not talk to them.
LIVE_HOSTS = ("://epay.esewa.com.np", "://esewa.com.np", "://khalti.com", "://pay.khalti.com")


@register("billing")
def check_payment_configuration(app_configs: Any, **kwargs: Any) -> list[Error]:
    from .gateways import base as gateways

    errors: list[Error] = []
    environment = getattr(settings, "PAYMENTS_ENVIRONMENT", "test")
    if environment != "test":
        errors.append(
            Error(
                f"PAYMENTS_ENVIRONMENT is {environment!r}; only 'test' is supported.",
                hint="Live payments need a merchant agreement and keys this build has not been given.",
                id="billing.E001",
            )
        )

    for key, gateway in gateways.registry().items():
        cfg = gateways.config(key)
        if not cfg.get("enabled"):
            continue
        for problem in gateway.problems():
            errors.append(
                Error(f"{gateway.label} is enabled but misconfigured: {problem}", id="billing.E002")
            )
        for name, value in cfg.items():
            if name.endswith("_url") and any(host in str(value) for host in LIVE_HOSTS):
                errors.append(
                    Error(
                        f"{gateway.label} {name} points at a live host ({value}) in test mode.",
                        id="billing.E003",
                    )
                )
    return errors
