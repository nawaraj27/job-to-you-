"""
CV improvement work that runs in the background, with a state a screen can read.

Two kinds of work, both of which wait on a model:

  * **turn** — an answer typed into the assistant conversation (or into an
    improvement question on the guided screen). The answer is written to the
    transcript the moment it is submitted, so "your answer was received" is a
    fact on the page before any model is asked; the reply and the drafted
    suggestions follow.
  * **prepare** — the safe rewordings an improvement plan lists
    (`resumes.improvement`), drafted one after another as suggestions.

Neither writes the CV. Everything they produce is a `ResumeSuggestion`, and the
candidate applies or rejects it (INV-3).

**The state is the row** (`ImprovementJob`), not the request. A candidate can
refresh, go to another page and come back, or close the tab: the screen reads
the row and shows the same stage. One active job per CV is enforced by the
database, so a double click or a second tab returns the job already running
instead of starting another; the form's one-time token makes a resubmitted
form return the job it already created.

**Stages, not a percentage.** The work is a model call whose progress nobody can
measure; the stages below are the steps the code actually passes through, and
the screen shows which one it is on. A "prepare" job also says which draft of
how many it is writing, because that count is real.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.ai.models import RunStatus
from apps.common.errors import DomainError, InvalidState

from ..models import (
    BuilderMode,
    ImprovementJob,
    ImprovementJobKind,
    ImprovementJobState,
    ResumeDocument,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

logger = logging.getLogger(__name__)

#: An active job untouched for this long is not coming back: its task was lost
#: or its worker died. It is marked failed, which offers the retry.
STALL_AFTER = dt.timedelta(minutes=10)

#: The steps each kind passes through, in order, as (key, what the screen says).
STAGES: dict[str, tuple[tuple[str, str], ...]] = {
    ImprovementJobKind.TURN: (
        ("received", "Your answer was received"),
        ("checking", "Checking what your CV already says"),
        ("drafting", "Preparing a suggested improvement"),
        ("reviewing", "Checking the suggestion against what you said"),
    ),
    ImprovementJobKind.PREPARE: (
        ("received", "Request received"),
        ("checking", "Reading your CV and what the analysis found"),
        ("drafting", "Drafting safe rewordings"),
        ("reviewing", "Checking each draft against your CV"),
    ),
}

#: Said when a job fails. Never the exception: a stack trace or a provider's
#: error text is not something a candidate can act on.
FAILED_AI = "The AI could not be reached. Nothing on your CV has changed; please try again."
FAILED_STALLED = (
    "This took far longer than it should and was stopped. Nothing on your CV has changed."
)
FAILED_UNEXPECTED = (
    "Something went wrong on our side. Nothing on your CV has changed; please try again."
)

#: Model outcomes that mean "no answer came back", as opposed to a deliberate
#: non-call (`NOT_ASKED`), which is a complete answer of its own.
_UNANSWERED = frozenset(
    {
        RunStatus.PROVIDER_ERROR,
        RunStatus.TIMEOUT,
        RunStatus.SCHEMA_ERROR,
        RunStatus.QUOTA_EXCEEDED,
        RunStatus.PENDING,
    }
)


# --- reading ----------------------------------------------------------------


def current_for(document: ResumeDocument) -> ImprovementJob | None:
    """The job a screen should show: the active one, else the latest.

    A job that has sat active past `STALL_AFTER` is failed here, on read, as
    well as by the periodic sweep — a screen must never show "processing" for
    work nobody is doing.
    """
    job = document.latest_improvement_job if document.latest_improvement_job_id else None
    if job is not None and job.is_active and job.updated_at < timezone.now() - STALL_AFTER:
        _fail(job, FAILED_STALLED)
    return job


#: How long a finished job's result stays on the screen.
SHOW_FINISHED_FOR = dt.timedelta(minutes=15)


def is_worth_showing(job: ImprovementJob | None) -> bool:
    """Whether the job panel should show this job at all."""
    if job is None:
        return False
    if job.is_active or job.is_failed:
        return True
    return bool(job.finished_at and job.finished_at > timezone.now() - SHOW_FINISHED_FOR)


def stages(job: ImprovementJob) -> list[dict[str, str]]:
    """Every stage of this job with its status: done, current, todo or failed."""
    steps = STAGES.get(job.kind, ())
    keys = [key for key, _label in steps]
    finished = job.state == ImprovementJobState.COMPLETED
    at = keys.index(job.stage) if job.stage in keys else 0
    rows = []
    for index, (key, label) in enumerate(steps):
        if finished or index < at:
            status = "done"
        elif index == at:
            status = "failed" if job.is_failed else "current"
        else:
            status = "todo"
        text = label
        if key == job.stage and job.stage_detail and not finished:
            text = f"{label} — {job.stage_detail}"
        rows.append({"key": key, "label": text, "status": status})
    return rows


# --- submitting ---------------------------------------------------------------


def submit(
    *,
    document: ResumeDocument,
    actor: User,
    kind: str,
    payload: dict[str, Any],
    token: str = "",
) -> tuple[ImprovementJob, bool]:
    """Queue one piece of work. Returns (job, created).

    `created` is False when the same form was submitted again (`token`) or
    another job is already running on this CV — the caller shows that job
    rather than a second one.
    """
    from . import builders as builder_service

    token = (token or "")[:64]
    with transaction.atomic():
        # Serialise submissions per CV: the unique constraint is the guarantee,
        # the lock keeps two racing requests from both writing a turn first.
        locked = (
            ResumeDocument.objects.select_for_update(of=("self",))
            .select_related("latest_improvement_job")
            .filter(pk=document.pk)
            .first()
        )
        if locked is not None:
            document.latest_improvement_job_id = locked.latest_improvement_job_id
            document.latest_improvement_job = locked.latest_improvement_job
        if token:
            existing = ImprovementJob.objects.filter(document=document, request_token=token).first()
            if existing is not None:
                return existing, False
        running = current_for(document)
        if running is not None and running.is_active:
            return running, False

        if kind == ImprovementJobKind.TURN:
            text = str(payload.get("text", "") or "").strip()
            if not text:
                raise InvalidState("Write something first: an answer, in your own words.")
            payload = {**payload, "text": text[:4000]}
            if payload.get("question"):
                builder_service.record_turn(
                    document=document, role="assistant", text=payload["question"], actor=None
                )
            # Written now, so "received" is true before any model is asked and
            # nothing the candidate typed can be lost to a failure after this.
            builder_service.record_turn(document=document, role="candidate", text=text, actor=actor)

        if kind == ImprovementJobKind.PREPARE:
            # One CV improvement draft from the plan's allowance (ADR-A59),
            # claimed only for a job that is really being created: a repeated
            # form (`token`) and a job already running returned above, so a
            # refresh or a second tab cannot spend twice. Refunded in `_fail`.
            from apps.billing.services import entitlements

            entitlements.claim(document.profile, "cv_improvements")

        try:
            with transaction.atomic():
                job = ImprovementJob.objects.create(
                    document=document,
                    kind=kind,
                    state=ImprovementJobState.QUEUED,
                    stage="received",
                    request_token=token,
                    payload=payload,
                    created_by=actor,
                )
        except IntegrityError:
            running = ImprovementJob.objects.filter(
                document=document, state__in=ImprovementJob.ACTIVE_STATES
            ).first()
            if running is None:  # pragma: no cover - the constraint names one
                raise
            return running, False

        ResumeDocument.objects.filter(pk=document.pk).update(latest_improvement_job=job)
        document.latest_improvement_job = job
        transaction.on_commit(lambda: _enqueue(job.pk))
    return job, True


def retry(*, job: ImprovementJob, actor: User) -> ImprovementJob:
    """Run a failed job again, from the start, with what it was given."""
    with transaction.atomic():
        job = ImprovementJob.objects.select_for_update().get(pk=job.pk)
        if not job.is_failed:
            raise InvalidState("Only something that failed can be tried again.")
        running = (
            ImprovementJob.objects.filter(
                document_id=job.document_id, state__in=ImprovementJob.ACTIVE_STATES
            )
            .exclude(pk=job.pk)
            .first()
        )
        if running is not None:
            return running
        if job.kind == ImprovementJobKind.PREPARE:
            # The failure refunded the first attempt; trying again spends one.
            from apps.billing.services import entitlements

            entitlements.claim(job.document.profile, "cv_improvements")
        job.state = ImprovementJobState.QUEUED
        job.stage = "received"
        job.stage_detail = ""
        job.result = {}
        job.finished_at = None
        job.save(
            update_fields=["state", "stage", "stage_detail", "result", "finished_at", "updated_at"]
        )
        transaction.on_commit(lambda: _enqueue(job.pk))
    return job


def _enqueue(job_id: int) -> None:
    from ..tasks import run_improvement_job

    run_improvement_job.delay(job_id)


# --- running ------------------------------------------------------------------


def run(job_id: int) -> str:
    """Do the work. Called by the task; safe to call twice for one job."""
    with transaction.atomic():
        job = (
            ImprovementJob.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("document", "created_by")
            .filter(pk=job_id)
            .first()
        )
        if job is None:
            return "gone"
        if job.state != ImprovementJobState.QUEUED:
            # Already running or finished: a duplicate delivery of the task.
            return f"skipped:{job.state}"
        job.state = ImprovementJobState.PROCESSING
        job.stage = "checking"
        job.stage_detail = ""
        job.attempts += 1
        job.started_at = timezone.now()
        job.save(
            update_fields=["state", "stage", "stage_detail", "attempts", "started_at", "updated_at"]
        )

    try:
        if job.kind == ImprovementJobKind.TURN:
            return _run_turn(job)
        if job.kind == ImprovementJobKind.PREPARE:
            return _run_prepare(job)
        _fail(job, FAILED_UNEXPECTED)
        return "unknown-kind"
    except Exception:  # noqa: BLE001 - recorded and told plainly, never shown raw
        logger.exception("improvement job %s failed", job.pk)
        _fail(job, FAILED_UNEXPECTED)
        return "failed"


def _run_turn(job: ImprovementJob) -> str:
    from . import assistant as assistant_service

    document = job.document
    actor = job.created_by
    if actor is None:
        # The account that asked has gone; nobody is left to answer for.
        _fail(job, FAILED_UNEXPECTED)
        return "no-actor"
    _stage(job, "drafting", state=ImprovementJobState.WAITING_AI)
    reply = assistant_service.respond(
        document=document,
        actor=actor,
        text=job.payload.get("text", ""),
        already_recorded=True,
        focus_entry=job.payload.get("focus_entry") or None,
    )
    if reply.run.status in _UNANSWERED:
        _fail(job, FAILED_AI, extra={"reply": reply.text})
        return "unanswered"

    _stage(job, "reviewing", state=ImprovementJobState.PROCESSING)
    _complete(
        job,
        {
            "reply": reply.text,
            "question": reply.question,
            "suggestions": len(reply.suggestions),
        },
    )
    return "completed"


def _run_prepare(job: ImprovementJob) -> str:
    from .. import improvement
    from . import analysis as analysis_service

    document = job.document
    plan = improvement.plan_for(document)
    wanted = set(job.payload.get("keys") or [])
    items = [item for item in plan.draftable if not wanted or item.key in wanted]
    if not items:
        _complete(
            job, {"drafted": 0, "skipped": [], "note": "Nothing left that can be reworded safely."}
        )
        return "nothing"

    drafted: list[int] = []
    skipped: list[str] = []
    target_role = plan.analysis.target_role if plan.analysis is not None else ""
    for index, item in enumerate(items, start=1):
        _stage(
            job,
            "drafting",
            state=ImprovementJobState.WAITING_AI,
            detail=f"{index} of {len(items)}: {item.affects}"[:120],
        )
        finding = _finding_row(item)
        try:
            suggestion = analysis_service.draft_rewrite(
                document=document,
                section_ref=item.section_ref,
                problem=item.title,
                actor=job.created_by,
                target_role=target_role,
                finding=finding,
            )
        except DomainError:
            skipped.append(item.title)
            continue
        drafted.append(suggestion.pk)

    if not drafted and skipped and len(skipped) == len(items):
        _fail(
            job,
            "Nothing could be drafted just now: the AI did not return a usable rewording. "
            "Nothing on your CV has changed; try again, or make the change yourself.",
            extra={"skipped": skipped},
        )
        return "none-drafted"

    _stage(job, "reviewing", state=ImprovementJobState.PROCESSING)
    _complete(job, {"drafted": len(drafted), "skipped": skipped})
    return "completed"


def _finding_row(item: Any) -> Any:
    if not item.finding_id:
        return None
    from ..models import ResumeFinding

    return ResumeFinding.objects.filter(pk=item.finding_id).first()


def _stage(job: ImprovementJob, stage: str, *, state: str, detail: str = "") -> None:
    job.stage = stage
    job.state = state
    job.stage_detail = detail
    ImprovementJob.objects.filter(pk=job.pk).update(
        stage=stage, state=state, stage_detail=detail, updated_at=timezone.now()
    )


def _complete(job: ImprovementJob, result: dict[str, Any]) -> None:
    now = timezone.now()
    job.state = ImprovementJobState.COMPLETED
    job.result = result
    job.finished_at = now
    ImprovementJob.objects.filter(pk=job.pk).update(
        state=ImprovementJobState.COMPLETED,
        result=result,
        stage_detail="",
        finished_at=now,
        updated_at=now,
    )


def _fail(job: ImprovementJob, message: str, *, extra: dict[str, Any] | None = None) -> None:
    now = timezone.now()
    if job.kind == ImprovementJobKind.PREPARE and job.state != ImprovementJobState.FAILED:
        # A draft that never arrived is the platform's failure, not use of
        # the candidate's allowance (ADR-A59).
        from apps.billing.services import entitlements

        entitlements.release(job.document.profile, "cv_improvements")
    result = {**(extra or {}), "error": message}
    job.state = ImprovementJobState.FAILED
    job.result = result
    job.finished_at = now
    ImprovementJob.objects.filter(pk=job.pk).update(
        state=ImprovementJobState.FAILED, result=result, finished_at=now, updated_at=now
    )


def fail_stalled() -> int:
    """Fail every active job nobody has touched in `STALL_AFTER`. For the sweep."""
    cutoff = timezone.now() - STALL_AFTER
    now = timezone.now()
    stalled = ImprovementJob.objects.filter(
        state__in=ImprovementJob.ACTIVE_STATES, updated_at__lt=cutoff
    )
    from apps.billing.services import entitlements

    for job in stalled.filter(kind=ImprovementJobKind.PREPARE).select_related("document__profile"):
        # A lost job is the platform's failure: its draft is refunded.
        entitlements.release(job.document.profile, "cv_improvements")
    return stalled.update(
        state=ImprovementJobState.FAILED,
        result={"error": FAILED_STALLED},
        finished_at=now,
        updated_at=now,
    )


# --- deciding on what was drafted -------------------------------------------------


def decide_all(
    *, document: ResumeDocument, actor: User, ids: list[int], accept: bool
) -> dict[str, int]:
    """Apply or reject the drafted changes the candidate was shown, together.

    Only the ids posted — the ones on the screen — so "Apply these
    improvements" can never apply something that arrived after the page was
    drawn. A change whose target moved since it was drafted is refused by the
    accept path and counted, not forced.
    """
    from ..models import SuggestionState
    from . import suggestions as suggestion_service

    done = refused = 0
    rows = document.draft.suggestions.filter(pk__in=ids, state=SuggestionState.OFFERED)
    for suggestion in rows:
        try:
            if accept:
                suggestion_service.accept(suggestion=suggestion, actor=actor)
            else:
                suggestion_service.reject(suggestion=suggestion, actor=actor)
            done += 1
        except DomainError:
            refused += 1
    return {"done": done, "refused": refused}


def ask(*, document: ResumeDocument, question: str, focus_entry: str = "") -> None:
    """Put one improvement question to the candidate in the conversation.

    Recorded as the assistant's turn, and the entry it is about remembered, so
    the next answer drafts into that entry rather than the most recent one.
    """
    from . import builders as builder_service

    session = builder_service.session_for(document=document, mode=BuilderMode.ASSISTANT)
    state = dict(session.step_state or {})
    state[FOCUS_KEY] = focus_entry
    session.step_state = state
    session.save(update_fields=["step_state", "updated_at"])
    builder_service.record_turn(document=document, role="assistant", text=question, actor=None)


#: Where `ask` leaves the entry the pending question is about.
FOCUS_KEY = "improvement_focus_entry"


def take_focus(document: ResumeDocument) -> str:
    """The entry the last improvement question was about, once."""
    from . import builders as builder_service

    session = builder_service.session_for(document=document, mode=BuilderMode.ASSISTANT)
    state = dict(session.step_state or {})
    focus = state.pop(FOCUS_KEY, "") or ""
    if focus:
        session.step_state = state
        session.save(update_fields=["step_state", "updated_at"])
    return focus
