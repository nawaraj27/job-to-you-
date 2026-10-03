"""
Notification and webhook reads (SRS §7.1, §5.12).

Every function takes the company it is scoped to. A webhook endpoint names a
URL the platform will POST to on that company's behalf, and its deliveries name
what was sent — neither is a thing one tenant may read about another, so there
is no unscoped queryset in this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import QuerySet

from apps.common.ids import as_uuid

from .delivery_models import WebhookDelivery, WebhookEndpoint

if TYPE_CHECKING:  # pragma: no cover
    from apps.companies.models import Company


def endpoints_for(company: Company) -> QuerySet[WebhookEndpoint]:
    return WebhookEndpoint.objects.filter(company=company)


def endpoint_for(company: Company, public_id: str) -> WebhookEndpoint | None:
    """One endpoint, scoped: an id from another tenant resolves to nothing."""
    if as_uuid(public_id) is None:
        return None
    return WebhookEndpoint.objects.filter(company=company, public_id=public_id).first()


def recent_deliveries(company: Company, *, limit: int = 20) -> list[WebhookDelivery]:
    return list(
        WebhookDelivery.objects.filter(endpoint__company=company).select_related(
            "endpoint", "event"
        )[:limit]
    )
