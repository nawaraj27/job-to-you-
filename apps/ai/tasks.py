"""
Background AI work (DEVELOPER.md §3.9).

Everything here runs on the `ai` queue, which exists so a slow or rate-limited
provider cannot consume the capacity ordinary background work needs (SRS
§17.5). A provider having a bad afternoon must not stop applications being
counted.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="ai")
def run_task(
    *,
    task_key: str,
    inputs: dict,
    user_id: int | None = None,
    company_id: int | None = None,
    force: bool = False,
) -> str:
    """Run one AI task off the request path.

    Ids rather than objects, because a Celery argument is JSON and a serialised
    model is a stale copy of a row that may have changed by the time the worker
    picks it up.

    Returns the run's public id so a caller can look up what happened.
    """
    from apps.accounts.models import User
    from apps.companies.models import Company

    from .services import runner

    run_row = runner.run(
        task_key=task_key,
        inputs=inputs,
        user=User.objects.filter(pk=user_id).first() if user_id else None,
        company=Company.objects.filter(pk=company_id).first() if company_id else None,
        force=force,
    )
    return str(run_row.public_id)


@shared_task(queue="default")
def purge_run_snapshots(days: int = 400) -> int:
    """Clear input snapshots past their retention (ADR-A11).

    The snapshot carries personal data and inherits the retention of the record
    it came from. The run row itself stays: cost history and the evaluation
    corpus outlive the text that produced them, and a deleted row would take
    both with it.
    """
    import datetime as dt

    from django.utils import timezone

    from .models import AIRun

    cutoff = timezone.now() - dt.timedelta(days=days)
    cleared = (
        AIRun.objects.filter(created_at__lt=cutoff)
        .exclude(input_snapshot={})
        .update(input_snapshot={}, raw_output="")
    )
    if cleared:
        logger.info("cleared %s AI run snapshot(s)", cleared)
    return cleared
