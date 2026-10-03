"""
Background interview work (DEVELOPER.md §3.9).

Evaluation runs on the `ai` queue for the reason every AI task does: a provider
having a bad afternoon must not consume the capacity ordinary background work
needs. Reminders and the hold sweep run on `default`, because neither calls a
model.
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

logger = logging.getLogger(__name__)

#: Scoring makes one model call per competency, in turn. The global 540-second
#: soft limit was sized for ordinary work; on the local model a single call has
#: taken over six minutes, so a four-competency interview could not fit.
SCORING_SOFT_LIMIT_SECONDS = 25 * 60
#: One kit is one long generation (up to ~8.5 minutes measured locally).
PLAN_SOFT_LIMIT_SECONDS = 15 * 60

#: A finished interview with no score this long after it ended is asked for
#: again: its task was lost, or the model was down when it ran.
STALLED_SCORING_MINUTES = 20
#: How long an unscored interview keeps being retried, and how often.
SCORING_RETRY_HOURS = 24
MAX_SCORING_ATTEMPTS = 3

#: When a reminder goes out (FR-10.6). Two, because one is not enough — a day
#: ahead is when somebody can still rearrange their afternoon, and an hour ahead
#: is when they actually need the link.
REMINDER_WINDOWS: tuple[tuple[str, int], ...] = (("24h", 24), ("1h", 1))

#: How wide a window each reminder sweep covers. The sweep runs hourly, so an
#: hour of tolerance means a session is reminded exactly once.
REMINDER_TOLERANCE_MINUTES = 30


@shared_task(
    queue="ai",
    soft_time_limit=SCORING_SOFT_LIMIT_SECONDS,
    time_limit=SCORING_SOFT_LIMIT_SECONDS + 60,
)
def evaluate_session(session_id: int) -> str:
    """Score one finished interview.

    Never raises into the queue for a scoring failure: an interview nobody could
    read is not an interview somebody failed, and a retry storm against a
    provider that is down would spend the company's budget to reach the same
    place.
    """
    from apps.common.errors import DomainError

    from .models import InterviewSession
    from .services import evaluation

    session = (
        InterviewSession.objects.select_related("template", "candidate__user", "application__job")
        .filter(pk=session_id)
        .first()
    )
    if session is None:
        return "gone"

    try:
        written = evaluation.evaluate(session=session)
    except DomainError as exc:
        logger.info("evaluation refused for session %s: %s", session_id, exc.message)
        return "refused"
    except SoftTimeLimitExceeded:
        # What was scored is kept and summarised; the rest is not guessed.
        logger.warning("evaluation of session %s ran out of time", session_id)
        if session.evaluations.filter(submitted_at__isnull=False).exists():
            evaluation.conclude(session=session)
        return "timed-out"

    return f"scored:{len(written)}" if written else "unavailable"


@shared_task(queue="default")
def send_reminders() -> dict[str, int]:
    """Remind both sides about interviews that are close (FR-10.6).

    Idempotent through the notification delivery ledger (ADR-A25): the sweep may
    run twice and the second run tells nobody twice, because the delivery's
    unique constraint refuses the duplicate rather than this task tracking what
    it has already sent.
    """
    from django.db import transaction
    from django.utils import timezone

    from apps.notifications import events

    from .models import InterviewSession, SessionState
    from .services import scheduling

    sent = 0
    for label, hours in REMINDER_WINDOWS:
        target = timezone.now() + dt.timedelta(hours=hours)
        window = dt.timedelta(minutes=REMINDER_TOLERANCE_MINUTES)

        sessions = InterviewSession.objects.filter(
            state=SessionState.SCHEDULED,
            scheduled_at__gte=target - window,
            scheduled_at__lt=target + window,
        ).select_related("candidate", "application__job__company")

        for session in sessions:
            # One transaction per reminder, not one for the sweep.
            #
            # `events.emit` refuses to run without a transaction, and a Celery
            # task has none — so this loop raised on the first session it found
            # and no interview reminder was ever delivered. Per session rather
            # than around the loop, so a sweep that fails halfway keeps the
            # reminders it already wrote.
            with transaction.atomic():
                events.emit(
                    "interview.scheduled",
                    session,
                    payload={
                        "session_public_id": str(session.public_id),
                        "candidate_public_id": str(session.candidate.public_id),
                        "kind": session.kind,
                        "when": scheduling.when_text(session.scheduled_at),
                        "job": (
                            session.application.job.title if session.application is not None else ""
                        ),
                        "company": (
                            session.application.job.company.name
                            if session.application is not None
                            else ""
                        ),
                        # Part of the dedupe key, so the 24-hour and 1-hour
                        # reminders are different notifications rather than one
                        # the ledger refuses to send twice.
                        "reminder": label,
                    },
                )
            sent += 1

    return {"reminded": sent}


#: How long a posting waits before its kit is attempted again after one was
#: started. Longer than a generation takes on the local model.
PLAN_COOLDOWN_SECONDS = 30 * 60


@shared_task(
    queue="ai", soft_time_limit=PLAN_SOFT_LIMIT_SECONDS, time_limit=PLAN_SOFT_LIMIT_SECONDS + 60
)
def plan_job_interview(job_id: int, replace: bool = False) -> str:
    """Write one posting's interview kit, off the request (FR-10.1).

    Queued the first time anybody interviews or practises against a posting
    that has no kit yet. That session runs on the general pool; this is what
    makes the next one specific to the role.

    Returns a word rather than raising, because none of the failure modes are
    worth a retry storm: a posting that has since been deleted, a model that is
    switched off, a budget that is spent. The next session queues it again.
    """
    from apps.jobs.models import Job

    from .services import planning

    job = Job.objects.filter(pk=job_id, deleted_at__isnull=True).select_related("company").first()
    if job is None:
        return "gone"

    if replace:
        # A kit that fails the anchoring rule. At most one rewrite a day per
        # posting, so a model that keeps writing generic questions cannot turn
        # every practice session into a paid regeneration.
        from django.core.cache import cache

        if not cache.add(f"interviews:replan:{job.pk}", 1, timeout=24 * 60 * 60):
            return "replan-throttled"
        return "rewritten" if planning.plan_job(job=job) is not None else "unavailable"

    if planning.cached_pools(job):
        # Two sessions started together and both queued this. The second one
        # has nothing to do.
        return "cached"

    # Every practice started while a posting has no kit queues this. With a
    # model that keeps failing — or is down — each of those sessions used to
    # cost two more generations of several minutes, stacked on the one AI
    # worker. One attempt per posting per half hour; the sessions in between
    # run on the job-aware fallback, as they would anyway.
    from django.core.cache import cache

    if not cache.add(f"interviews:plan:{job.pk}", 1, timeout=PLAN_COOLDOWN_SECONDS):
        return "plan-throttled"
    return "written" if planning.plan_job(job=job) is not None else "unavailable"


@shared_task(queue="default")
def sweep_holds() -> int:
    """Return expired slot holds to the market."""
    from .services import scheduling

    return scheduling.sweep_holds()


@shared_task(queue="default")
def close_stale_sessions(hours: int = 6) -> int:
    """Close interviews nobody is coming back to, into the state that is true.

    **A self-paced interview** left in progress for six hours is a closed tab:
    it is abandoned, which produces no outcome and blocks nothing.

    **A live interview is different.** It was being marked abandoned too, which
    is false — both people were in the room, the conversation happened, and an
    abandoned session cannot be scored. The panel then had no way to record the
    outcome of an interview they had just conducted. A live interview whose room
    has closed is completed instead, so its scorecard stays open.
    """
    from django.db import transaction
    from django.db.models import Q
    from django.utils import timezone

    from . import policies
    from .models import InterviewKind, InterviewSession, SessionState
    from .services import scheduling

    now = timezone.now()
    cutoff = now - dt.timedelta(hours=hours)
    closed = (
        InterviewSession.objects.filter(state=SessionState.IN_PROGRESS, started_at__lt=cutoff)
        .filter(~Q(kind=InterviewKind.LIVE))
        .update(
            state=SessionState.ABANDONED,
            ended_at=now,
            cancelled_reason="Left unfinished",
            updated_at=now,
        )
    )

    live = InterviewSession.objects.filter(
        kind=InterviewKind.LIVE, state=SessionState.IN_PROGRESS
    ).select_related("slot")
    for session in live:
        window = policies.join_window(session)
        if window is None or window[1] > now:
            continue
        try:
            with transaction.atomic():
                scheduling.complete_live(session=session, actor=None)
            closed += 1
        except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
            logger.warning("could not complete live session %s", session.pk, exc_info=True)

    return closed


@shared_task(queue="default")
def purge_old_transcripts(days: int | None = None) -> int:
    """Delete interview answers past their retention (SRS §15.3).

    Twelve months by default. The session, its state, its scores and its
    outcome all survive — an employer keeps the record of what was decided —
    and what goes is the transcript itself: the question text and the words the
    candidate typed. A recording of somebody answering badly should not follow
    them for years, and nothing downstream reads a turn after the evaluation
    that produced the scores.
    """
    from django.utils import timezone

    from apps.common.retention import INTERVIEW_TRANSCRIPT_DAYS

    from .models import InterviewTurn

    cutoff = timezone.now() - dt.timedelta(
        days=days if days is not None else INTERVIEW_TRANSCRIPT_DAYS
    )
    deleted, _ = InterviewTurn.objects.filter(session__ended_at__lt=cutoff).delete()

    if deleted:
        logger.info("purged interview transcript turns", extra={"count": deleted})
    return deleted


@shared_task(queue="default")
def recover_unscored_sessions() -> dict[str, int]:
    """Ask again for interviews that finished but were never scored.

    Scoring is queued once, when the interview ends. If the model was down, the
    worker died or the broker lost the message, nothing asked again, and the
    candidate's result page said "your answers are being read" for ever. The CV
    pipeline has had this sweep since STALL-01; interviews had not.

    Bounded: three attempts, for a day. After that the result page says the
    answers could not be read rather than that they are being read.
    """
    from django.core.cache import cache
    from django.utils import timezone

    from .models import InterviewKind, InterviewSession, SessionState

    now = timezone.now()
    stalled = list(
        InterviewSession.objects.filter(
            kind__in=InterviewKind.self_paced(),
            state=SessionState.COMPLETED,
            ended_at__lt=now - dt.timedelta(minutes=STALLED_SCORING_MINUTES),
            ended_at__gte=now - dt.timedelta(hours=SCORING_RETRY_HOURS),
            evaluations__isnull=True,
        )
        .values_list("pk", flat=True)
        .distinct()[:200]
    )
    requeued = 0
    for session_id in stalled:
        key = f"interviews:scoring-attempts:{session_id}"
        cache.add(key, 0, timeout=SCORING_RETRY_HOURS * 60 * 60)
        if cache.incr(key) > MAX_SCORING_ATTEMPTS:
            continue
        evaluate_session.delay(session_id)
        requeued += 1
    return {"stalled": len(stalled), "requeued": requeued}
