"""
Announcements and events a company shares with its followers (FOL-03).

Small on purpose: an update is written by someone allowed to edit the company
(`company.edit`), shown on the public company page, and announced to followers
through the notification system — `company.update_published`, audience
`company_followers`, preference "Companies you follow". There is no second
path by which a company can reach somebody.

The follower rules are the audience's, not repeated here: only follows made
before the update was published, never somebody who blocked the company.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState
from apps.notifications import events

from .. import policies
from ..models import Company, CompanyUpdate, CompanyUpdateKind

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

    from ..models import CompanyMembership

MAX_TITLE = 160
MAX_BODY = 2000
#: How many updates the public page shows.
PUBLIC_LIMIT = 6


def live_for(company: Company) -> dict[str, list[CompanyUpdate]]:
    """What the public page shows: live updates, upcoming events first."""
    now = timezone.now()
    live = CompanyUpdate.objects.filter(company=company, withdrawn_at__isnull=True)
    upcoming = list(
        live.filter(kind=CompanyUpdateKind.EVENT, starts_at__gte=now).order_by("starts_at")[
            :PUBLIC_LIMIT
        ]
    )
    news = list(
        live.filter(kind=CompanyUpdateKind.ANNOUNCEMENT).order_by("-created_at")[:PUBLIC_LIMIT]
    )
    return {"events": upcoming, "announcements": news}


@transaction.atomic
def publish(
    *,
    actor: User,
    membership: CompanyMembership | None,
    company: Company,
    kind: str,
    title: str,
    body: str = "",
    starts_at: dt.datetime | None = None,
    location: str = "",
) -> CompanyUpdate:
    """Post an update and tell the company's followers."""
    policies.require_can_edit_company(membership)
    policies.require_company_active(company)

    title = " ".join((title or "").split())[:MAX_TITLE]
    body = (body or "").strip()[:MAX_BODY]
    location = " ".join((location or "").split())[:160]
    if kind not in CompanyUpdateKind.values:
        raise InvalidState("Choose whether this is an announcement or an event.")
    if not title:
        raise InvalidState("Give the update a title.")
    if kind == CompanyUpdateKind.EVENT:
        if starts_at is None:
            raise InvalidState("An event needs a date and time.")
        if starts_at < timezone.now():
            raise InvalidState("That event time has already passed.")
    else:
        starts_at = None
        location = ""

    update = CompanyUpdate.objects.create(
        company=company,
        kind=kind,
        title=title,
        body=body,
        starts_at=starts_at,
        location=location,
        created_by=actor,
    )
    audit.record(verb="company.update_published", actor=actor, target=company)
    events.emit(
        "company.update_published",
        company,
        payload={
            "company_id": company.pk,
            "company_name": company.name,
            "update_public_id": str(update.public_id),
            "title": title,
            "summary": _summary(update),
            "kind": kind,
            "target_url": reverse("jobs:company", args=[company.slug]) + "#updates",
        },
    )
    return update


@transaction.atomic
def withdraw(*, actor: User, membership: CompanyMembership | None, update: CompanyUpdate) -> None:
    """Take an update off the public page. Delivered notices stay."""
    policies.require_can_edit_company(membership)
    if membership is None or update.company_id != membership.company_id:
        raise InvalidState("That update is not this company's.")
    if update.withdrawn_at is not None:
        return
    update.withdrawn_at = timezone.now()
    update.save(update_fields=["withdrawn_at", "updated_at"])
    audit.record(verb="company.update_withdrawn", actor=actor, target=update.company)


def _summary(update: CompanyUpdate) -> str:
    if update.kind == CompanyUpdateKind.EVENT and update.starts_at is not None:
        when = timezone.localtime(update.starts_at).strftime("%a %d %b, %H:%M")
        return f"Event · {when}" + (f" · {update.location}" if update.location else "")
    text = " ".join(update.body.split())
    return (text[:140] + "…") if len(text) > 140 else (text or "Announcement")
