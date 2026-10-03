"""
The in-app feed (SRS §13.5).

Two behaviours carried over from the mock this replaces, because both were
right and neither is obvious:

**Muting is real.** A notification whose preference group is switched off is
filtered out of the feed entirely, not collected and hidden behind a badge.
Otherwise "turn this off" means "keep sending it, quietly", and the count on
the bell goes on rising for things the person said they did not want.

**Dismissing is undoable.** It is a single click on a row of similar-looking
rows, and the thing it destroys cannot be got back from anywhere else. The last
dismissal is kept aside until the next one replaces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.common.ids import as_uuid

from ..models import Notification, NotificationPreference

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

PAGE_SIZE = 20


def muted_groups(user: User) -> set[str]:
    """Preference groups this person has switched off in-app."""
    return set(
        NotificationPreference.objects.filter(user=user, in_app=False).values_list(
            "event_key", flat=True
        )
    )


def feed(user: User) -> QuerySet[Notification]:
    """What this person should see, muted groups removed."""
    muted = muted_groups(user)
    queryset = Notification.objects.for_user(user).visible()
    if muted:
        queryset = queryset.exclude(event_key__in=muted)
    return queryset


def unread_count(user: User) -> int:
    """The bell's number, in one query.

    Read on every authenticated page render by the application shell, so the
    muted groups are a subquery rather than a separate round trip. The answer
    is the same one `muted_groups` would have produced — `event_key` is never
    null, so `NOT IN` cannot swallow a row — and it is one query instead of
    two on every page in the product.
    """
    muted = NotificationPreference.objects.filter(user=user, in_app=False).values("event_key")
    return Notification.objects.for_user(user).unread().exclude(event_key__in=muted).count()


@transaction.atomic
def mark_read(*, user: User, public_id: str) -> Notification | None:
    """Read one. Scoped to its owner, so a guessed id resolves to nothing."""
    if as_uuid(public_id) is None:
        return None
    notification = Notification.objects.for_user(user).filter(public_id=public_id).first()
    if notification is None or notification.read_at is not None:
        return notification

    notification.read_at = timezone.now()
    notification.save(update_fields=["read_at", "updated_at"])
    return notification


@transaction.atomic
def mark_all_read(*, user: User, kind: str = "") -> int:
    """Read everything this person can see — within the filter they are looking at.

    The button sits on a filtered list. It used to ignore the filter, so
    "Mark all read" on the interview notices also cleared every unread
    message and stage change the person had not seen yet.
    """
    rows = feed(user).filter(read_at__isnull=True)
    if kind:
        rows = rows.filter(kind=kind)
    return Notification.objects.filter(pk__in=rows.values("pk")).update(read_at=timezone.now())


@transaction.atomic
def dismiss(*, user: User, public_id: str) -> Notification | None:
    """Remove one from the feed, keeping it recoverable."""
    if as_uuid(public_id) is None:
        return None
    notification = Notification.objects.for_user(user).filter(public_id=public_id).first()
    if notification is None:
        return None

    notification.dismissed_at = timezone.now()
    notification.save(update_fields=["dismissed_at", "updated_at"])
    return notification


@transaction.atomic
def undismiss(*, user: User, public_id: str) -> Notification | None:
    """The undo. One click destroyed it; one click brings it back."""
    if as_uuid(public_id) is None:
        return None
    notification = (
        Notification.objects.for_user(user)
        .filter(public_id=public_id, dismissed_at__isnull=False)
        .first()
    )
    if notification is None:
        return None

    notification.dismissed_at = None
    notification.save(update_fields=["dismissed_at", "updated_at"])
    return notification


def last_dismissed(user: User) -> Notification | None:
    """What the undo button would restore."""
    return (
        Notification.objects.for_user(user)
        .filter(dismissed_at__isnull=False)
        .order_by("-dismissed_at")
        .first()
    )
