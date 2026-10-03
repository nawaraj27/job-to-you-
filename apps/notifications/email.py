"""
Outbound email seam (P1 scope).

The full provider — templates in the database, delivery status, suppression
list, digests — is P10. P1 needs verification and invitation email, so this is
the minimum behind the interface P10 will fill: one function, plain text, no
provider-specific behaviour.

Deliberately not a domain event. Sending mail is an *effect* of the notification
pipeline, not a fact about the world, and putting effects on the event bus
invites loops (SRS §13.1).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail as django_send_mail

logger = logging.getLogger(__name__)


def send(*, to: str, subject: str, body: str) -> bool:
    """Send one message. Returns False rather than raising.

    A failed notification must never break the transaction that caused it: the
    account was created, the invitation exists, and the user can request another
    link. P10 replaces this with a stored delivery record and a retry.
    """
    try:
        django_send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[to],
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised into a service
        # The recipient's domain is what diagnoses a delivery problem; their
        # address is personal data, and the provider's error text often repeats
        # it (DEVELOPER.md: log identifiers, never what you should not).
        logger.warning(
            "email send failed",
            extra={
                "to": masked(to),
                "error": type(exc).__name__,
                "detail": str(exc).replace(to, masked(to))[:300],
            },
        )
        return False
    return True


def masked(address: str) -> str:
    """`m***@example.com`: the domain, and nothing that names the person."""
    local, sep, domain = (address or "").partition("@")
    if not sep:
        return "***"
    return f"{local[:1]}***@{domain}"
