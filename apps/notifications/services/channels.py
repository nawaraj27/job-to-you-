"""
The channels a delivery can take (SRS §13.4, §13.5).

Two here — in-app and email — and both are written so the fan-out worker can
treat them identically: given an event, a spec and a recipient, do the thing
and say what happened.

Quiet hours and send caps live here rather than in fan-out, because they are
about the *person*, not about the event: a burst of forty events at 03:00 is
one decision, made once per recipient.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from ..models import (
    DeliveryStatus,
    EmailMessage,
    EmailStatus,
    EmailSuppression,
    Notification,
    NotificationPreference,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

    from ..events import EventSpec
    from ..models import OutboxEvent

logger = logging.getLogger(__name__)

#: How many emails one person may receive in a day, across everything. A cap
#: rather than a limit per event type: the person experiences the total, and a
#: product that sends forty "helpful" messages has stopped being helpful.
EMAIL_CAP_PER_DAY = 25


def render(spec: EventSpec, payload: dict[str, Any]) -> tuple[str, str]:
    """Fill the event's wording from its own payload.

    A missing key renders as a blank rather than raising: a notification with a
    gap in it is a small failure, and a fan-out chunk that dies because one
    payload was short is a large one.
    """
    return _fill(spec.title, payload), _fill(spec.body, payload)


def _fill(template: str, payload: dict[str, Any]) -> str:
    if not template:
        return ""
    try:
        return template.format_map(_Blank(payload))
    except Exception:  # noqa: BLE001 - wording must never break delivery
        return template


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


# --- in-app ---------------------------------------------------------------


def deliver_in_app(*, event: OutboxEvent, spec: EventSpec, recipient: User) -> Notification:
    """Write one row into somebody's feed.

    `dedupe_key` collapses a burst into one line that counts: forty
    applications to one posting in an hour is one notification saying forty,
    not forty notifications. The collapse only applies while the row is
    unread — once somebody has read it, the next arrival starts a fresh line
    rather than being swallowed by one they have already seen.
    """
    title, body = render(spec, event.payload)
    dedupe = _fill(spec.dedupe, event.payload) if spec.dedupe else ""

    if dedupe:
        collapsed = _reinforce(recipient, dedupe, title, body)
        if collapsed is not None:
            return collapsed

    return Notification.objects.create(
        recipient=recipient,
        kind=event.type,
        event_key=spec.preference_group,
        title=title[:160],
        body=body[:300],
        target_url=str(event.payload.get("target_url") or "")[:300],
        payload=event.payload,
        dedupe_key=dedupe[:120],
    )


def _reinforce(recipient: User, dedupe: str, title: str, body: str) -> Notification | None:
    """Bump the existing unread row for this key, if there is one."""
    existing = Notification.objects.unread().filter(recipient=recipient, dedupe_key=dedupe).first()
    if existing is None:
        return None

    Notification.objects.filter(pk=existing.pk).update(
        count=F("count") + 1, title=title[:160], body=body[:300], updated_at=timezone.now()
    )
    existing.refresh_from_db()
    return existing


# --- email ----------------------------------------------------------------


def deliver_email(*, event: OutboxEvent, spec: EventSpec, recipient: User) -> tuple[str, str]:
    """Queue one email. Returns (delivery status, reason).

    Four gates before anything is written, in the order that costs least:
    suppression, quiet hours, the daily cap, then the send itself.
    """
    address = (recipient.email or "").strip()
    if not address:
        return DeliveryStatus.FAILED, "no address"

    if is_suppressed(address):
        return DeliveryStatus.SUPPRESSED, "suppressed"

    if in_quiet_hours(recipient, spec.preference_group):
        # Not sent, and not re-sent later: the in-app row is still written, and
        # an unread one goes into the weekly digest for somebody who receives
        # it. This comment used to say the digest "picks it up", which is true
        # only for people with the digest on — the settings screen now says
        # exactly that rather than promising more.
        return DeliveryStatus.MUTED, "quiet hours"

    if over_daily_cap(address):
        return DeliveryStatus.SUPPRESSED, "daily cap"

    title, body = render(spec, event.payload)
    message = EmailMessage.objects.create(
        user=recipient,
        to_email=address,
        template_key=event.type,
        # Minimal (SRS §5.12): the identifiers a re-render needs, never a
        # context dump that duplicates personal data into a table with a
        # different retention.
        params={"title": title, "event": event.type},
    )
    return send_message(message, subject=title, body=body)


def send_message(message: EmailMessage, *, subject: str, body: str) -> tuple[str, str]:
    """Hand one queued message to the provider and record what happened.

    Public because the digest sends through it too: one place that knows how a
    send is recorded, rather than two that will eventually disagree about what
    a failure looks like.
    """
    from .. import email as provider

    if provider.send(to=message.to_email, subject=subject, body=body):
        message.status = EmailStatus.SENT
        message.sent_at = timezone.now()
        message.save(update_fields=["status", "sent_at", "updated_at"])
        return DeliveryStatus.SENT, ""

    message.status = EmailStatus.FAILED
    message.error = "provider refused"
    message.save(update_fields=["status", "error", "updated_at"])
    return DeliveryStatus.FAILED, "provider refused"


def is_suppressed(email: str) -> bool:
    return EmailSuppression.objects.filter(email__iexact=email).exists()


def suppress(*, email: str, reason: str, detail: str = "") -> EmailSuppression:
    """Stop writing to an address. Idempotent.

    The insert has its **own** atomic block, and the surrounding function has
    none. Catching an `IntegrityError` raised inside the transaction you are
    still in leaves that transaction unusable — every later query in it fails
    with `TransactionManagementError`, and the caller sees a crash that has
    nothing to do with what they asked for. The savepoint is what makes the
    recovery legal.
    """
    address = email.strip().lower()
    try:
        with transaction.atomic():
            return EmailSuppression.objects.create(
                email=address, reason=reason, detail=detail[:300]
            )
    except IntegrityError:
        return EmailSuppression.objects.get(email__iexact=address)


def unsuppress(*, email: str) -> int:
    """Staff undo, for an address suppressed by mistake."""
    removed, _ = EmailSuppression.objects.filter(email__iexact=email).delete()
    return removed


def over_daily_cap(email: str) -> bool:
    """Has this address had its day's allowance? Shared with the digest."""
    return _over_cap(email)


def _over_cap(email: str) -> bool:
    since = timezone.now() - dt.timedelta(days=1)
    return (
        EmailMessage.objects.filter(
            to_email__iexact=email, created_at__gte=since, status=EmailStatus.SENT
        ).count()
        >= EMAIL_CAP_PER_DAY
    )


def in_quiet_hours(user: User, event_key: str) -> bool:
    """Is it the middle of this person's night?

    Stored per preference row as `{"from": "22:00", "to": "07:00", "tz": ...}`.
    A window that wraps midnight is the normal case, so it is handled first
    rather than as an edge.
    """
    if not event_key:
        return False

    preference = NotificationPreference.objects.filter(user=user, event_key=event_key).first()
    window = (preference.quiet_hours if preference else None) or {}
    start, end = window.get("from"), window.get("to")
    if not start or not end:
        return False

    try:
        zone = ZoneInfo(window.get("tz") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")

    now = timezone.now().astimezone(zone).time()
    try:
        opens = dt.time.fromisoformat(start)
        closes = dt.time.fromisoformat(end)
    except ValueError:
        return False

    if opens <= closes:
        return opens <= now < closes
    # Wraps midnight: 22:00 to 07:00 is two intervals, not one.
    return now >= opens or now < closes
