"""
Account background work (SRS §19.4).

One task that matters: the erasure sweep. It runs the erasures whose grace
period has expired, one account at a time, and never in a single transaction —
`erasure.erase` explains why.

**Failures are per account.** An erasure that raises must not stop the others:
the person whose job crashed still needs theirs run, and the ones behind them
in the queue have waited fourteen days already. The failure is logged loudly
and the account keeps `erasure_started_at` set, so the next sweep resumes it
rather than starting over — every step is idempotent for exactly this reason.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="default")
def run_due_erasures(limit: int = 25) -> dict[str, int]:
    """Erase the accounts whose fourteen days are up.

    Bounded per run: an erasure walks a dozen tables and purges object storage,
    and a hundred of them in one sweep is a maintenance window nobody planned.
    """
    from .services import erasure

    done = 0
    failed = 0

    for user in erasure.due_now()[:limit]:
        try:
            erasure.erase(user=user)
            done += 1
        except Exception:  # noqa: BLE001 - one failure must not stop the rest
            failed += 1
            logger.exception("erasure failed", extra={"user_id": user.pk})

    if done or failed:
        logger.info("erasure sweep", extra={"erased": done, "failed": failed})
    return {"erased": done, "failed": failed}


@shared_task(queue="default")
def resume_interrupted_erasures(limit: int = 25) -> int:
    """Finish erasures that began and did not end.

    A worker killed mid-erasure leaves an account with `erasure_started_at`
    set and `erased_at` null — the gate is closed, so nothing can write to it,
    but the walk is incomplete. Without this, that account sits half-erased
    until somebody notices, which is the worst of both states.
    """
    from .models import User
    from .services import erasure

    stuck = User.objects.filter(erasure_started_at__isnull=False, erased_at__isnull=True)[:limit]

    finished = 0
    for user in stuck:
        try:
            erasure.erase(user=user)
            finished += 1
        except Exception:  # noqa: BLE001
            logger.exception("erasure resume failed", extra={"user_id": user.pk})

    return finished
