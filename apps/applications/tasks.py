"""
Scheduled work for applications (DEVELOPER.md §3.9).

Both tasks keep a promise the product makes on screen: an offer with an expiry
date expires, and a company that never answers is told so.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="default")
def expire_due_offers() -> int:
    """Expire every sent offer whose date has passed. Runs hourly.

    An offer that says "expires Friday" and is still acceptable on Monday has
    taught the candidate that the date was decoration.
    """
    from .services import offers

    expired = offers.expire_due()
    if expired:
        logger.info("expired %s offer(s)", expired)
    return expired


@shared_task(queue="default")
def sweep_stale_applications(days: int = 30) -> int:
    """Count applications nobody has touched in `days`.

    Counts and logs; it does not close anything. Auto-closing an application
    because a recruiter was on holiday would be the platform rejecting somebody
    on the employer's behalf, which is not a thing it may do (FR-6.7). The
    number feeds the employer's response-time nudge (FR-6.15).
    """
    import datetime as dt

    from django.utils import timezone

    from .models import Application

    cutoff = timezone.now() - dt.timedelta(days=days)
    stale = Application.objects.live().filter(last_activity_at__lt=cutoff).count()
    if stale:
        logger.info("%s application(s) untouched for %s days", stale, days)
    return stale


@shared_task(queue="default")
def purge_closed_applications(days: int | None = None) -> dict[str, int]:
    """Remove the personal content of long-closed applications (SRS §15.3).

    Twenty-four months **from closure**, not from creation: an application that
    is still open is current data however old the posting is.

    Content rather than rows. The application, its stage history and its
    outcome survive, because an employer may be obliged to keep them and
    because the funnel a company reads is built from them. What goes is what
    the candidate wrote — screening answers — and what the company wrote about
    them: notes and ratings. That is the confidential class in §15.3, and it is
    the half that has no reason to exist two years after a decision.
    """
    import datetime as dt

    from django.utils import timezone

    from apps.common.retention import CLOSED_APPLICATION_DAYS

    from .models import Application, ApplicationNote

    cutoff = timezone.now() - dt.timedelta(
        days=days if days is not None else CLOSED_APPLICATION_DAYS
    )
    old = Application.objects.filter(closed_at__lt=cutoff)

    answers = old.exclude(answers={}).update(answers={})
    notes, _ = ApplicationNote.objects.filter(application__in=old).delete()

    if answers or notes:
        logger.info("purged closed applications", extra={"answers": answers, "notes": notes})
    return {"answers": answers, "notes": notes}
