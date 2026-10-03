"""
Deployment safety checks for shared infrastructure.

The failure this guards is silent by construction: with no encryption key
configured, nothing breaks at import time, nothing breaks at migrate time, and
the first symptom is a 500 the first time a candidate saves a phone number —
in production, on a real person's data.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import Error, register


@register("common")
def check_field_encryption_keys(app_configs: Any, **kwargs: Any) -> list[Error]:
    keys = getattr(settings, "FIELD_ENCRYPTION_KEYS", None) or []

    if not keys:
        return [
            Error(
                "FIELD_ENCRYPTION_KEYS is empty, so encrypted fields (candidate phone "
                "numbers) cannot be written or read.",
                hint=(
                    "Set FIELD_ENCRYPTION_KEYS to one or more Fernet keys, newest first. "
                    'Generate one with: python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"'
                ),
                id="common.E001",
            )
        ]

    try:
        from cryptography.fernet import Fernet

        for key in keys:
            Fernet(key)
    except Exception as exc:  # noqa: BLE001 - any rejection is the finding
        return [
            Error(
                f"FIELD_ENCRYPTION_KEYS contains a value that is not a valid Fernet key: {exc}",
                id="common.E002",
            )
        ]

    return []


#: Backends that never deliver a message to anybody.
UNDELIVERED_EMAIL_BACKENDS = frozenset(
    {
        "django.core.mail.backends.console.EmailBackend",
        "django.core.mail.backends.locmem.EmailBackend",
        "django.core.mail.backends.dummy.EmailBackend",
        "django.core.mail.backends.filebased.EmailBackend",
    }
)
SMTP_EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"


@register("common", deploy=True)
def check_email_is_delivered(app_configs: Any, **kwargs: Any) -> list[Error]:
    """Production must actually send email.

    `EMAIL_BACKEND` defaults to the console backend so development needs no
    provider, and production did not override it: a deploy that forgot
    `DJANGO_EMAIL_BACKEND` printed every verification link, password reset and
    invitation to the container log and told nobody. SMTP with no host is the
    same failure one step later.
    """
    if settings.DEBUG:
        return []
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if backend in UNDELIVERED_EMAIL_BACKENDS:
        return [
            Error(
                f"EMAIL_BACKEND is {backend.rsplit('.', 2)[-2]}, which delivers no email.",
                hint=(
                    "Set DJANGO_EMAIL_BACKEND (for example the SMTP backend) and EMAIL_HOST; "
                    "see docs/DEPLOYMENT.md."
                ),
                id="common.E003",
            )
        ]
    if backend == SMTP_EMAIL_BACKEND and not getattr(settings, "EMAIL_HOST", ""):
        return [
            Error(
                "EMAIL_BACKEND is SMTP but EMAIL_HOST is empty, so no email is sent.",
                hint="Set EMAIL_HOST, EMAIL_PORT and credentials; see docs/DEPLOYMENT.md.",
                id="common.E004",
            )
        ]
    return []
