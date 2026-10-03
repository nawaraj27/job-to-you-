"""
Scheduled work for jobs (DEVELOPER.md §3.9).

Both tasks keep a promise the product makes on screen: a closing date means the
posting closes, and a daily alert arrives daily. A promise kept by a person
remembering is not kept.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="default")
def expire_due_jobs() -> int:
    """Close every published posting whose date has passed. Runs hourly.

    Hourly rather than daily: the window is the honest gap between the date an
    employer set and the moment the posting actually stops taking applications.
    """
    from .services import lifecycle

    expired = lifecycle.expire_due()
    if expired:
        logger.info("expired %s job(s)", expired)
    return expired


@shared_task(queue="default")
def run_job_alerts(frequency: str = "daily") -> dict[str, int]:
    """Match saved searches against jobs published since they last ran.

    Idempotent: matches are unique per (search, job), so a re-run after a
    failure tells nobody twice.
    """
    from .services import discovery

    return discovery.run_alerts(frequency=frequency)
