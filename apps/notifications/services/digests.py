"""
The weekly digest (SRS §13.5).

For people who want to know what happened without being interrupted while it
happens. One message, everything unread, role-appropriate wording — and it
respects the same suppression list and daily cap as everything else, because a
digest that ignores a bounce is the same reputational problem as any other
message that does.

Digest recipients are those whose `weekly_digest` preference has email on. It
is a preference group like any other, so switching it off genuinely stops it.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from ..models import (
    DeliveryStatus,
    EmailMessage,
    EmailStatus,
    Notification,
    NotificationPreference,
)
from . import channels

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

logger = logging.getLogger(__name__)

#: Below this there is nothing worth an email. One unread notification is
#: already visible on the bell, and a digest containing it is noise.
MIN_ITEMS = 2
MAX_ITEMS = 20


#: The weekly job runs once a week; anything sent inside this window means this
#: week's digest already went. A day short of seven, so a run that starts a few
#: minutes late next Monday is not mistaken for a repeat.
RESEND_AFTER = dt.timedelta(days=6)


def run(*, frequency: str = "weekly", chunk_size: int = 500) -> dict[str, int]:
    """Send one digest per eligible person — every eligible person.

    It used to take the first thousand preference rows, in no order, once a
    week: past a thousand opted-in accounts the rest never received a digest,
    and which thousand did was up to the database.
    """
    recipients = (
        NotificationPreference.objects.filter(
            event_key=NotificationPreference.Event.WEEKLY_DIGEST,
            email=True,
            user__is_active=True,
        )
        .select_related("user")
        .order_by("pk")
    )

    considered = sent = 0
    for preference in recipients.iterator(chunk_size=chunk_size):
        considered += 1
        if send_to(user=preference.user):
            sent += 1

    return {"considered": considered, "sent": sent}


@transaction.atomic
def send_to(*, user: User) -> bool:
    """One person's digest. False when there was nothing worth sending.

    Safe to run twice: Celery delivers a scheduled task at least once, and a
    retried or duplicated weekly run used to send everybody a second copy. The
    user row is locked so two runs at the same moment settle one after the
    other, and a digest already sent this week stops the second.
    """
    from apps.accounts.models import User as UserModel

    from . import feed as feed_service

    locked = UserModel.objects.select_for_update().filter(pk=user.pk, is_active=True).first()
    if locked is None:
        # Deactivated or erased: erasure leaves the preference row behind and
        # replaces the address with a tombstone, which is not somebody to mail.
        return False

    already = EmailMessage.objects.filter(
        user=user,
        template_key="digest.weekly",
        status=EmailStatus.SENT,
        created_at__gte=timezone.now() - RESEND_AFTER,
    ).exists()
    if already:
        return False

    items = list(feed_service.feed(user).filter(read_at__isnull=True)[:MAX_ITEMS])
    if len(items) < MIN_ITEMS:
        return False

    address = (locked.email or "").strip()
    if not address or channels.is_suppressed(address):
        return False
    if channels.over_daily_cap(address):
        # The docstring always promised this; the code only checked suppression.
        return False

    subject = f"{len(items)} updates waiting for you"
    body = _compose(items)

    message = EmailMessage.objects.create(
        user=user,
        to_email=address,
        template_key="digest.weekly",
        params={"count": len(items)},
    )
    status, _ = channels.send_message(message, subject=subject, body=body)
    return status == DeliveryStatus.SENT


def _compose(items: list[Notification]) -> str:
    """Plain text, one line per item.

    Deliberately not a summary the platform wrote: a digest that paraphrases is
    a digest that can be wrong, and every line here is already the wording the
    event itself declared.
    """
    lines = ["Here is what you have not read yet:", ""]
    for item in items:
        prefix = f"({item.count}x) " if item.count > 1 else ""
        lines.append(f"- {prefix}{item.title}")
        if item.body:
            lines.append(f"  {item.body}")
    lines += ["", f"Sent {timezone.now():%d %B %Y}. Change what you receive in your settings."]
    return "\n".join(lines)
