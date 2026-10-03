"""
Applying, and withdrawing (SRS §5.8, §11.2).

The apply path is the most consequential write in the product, and almost all
of its length is freezing things that must not move afterwards:

  * **the CV.** An immutable `ResumeVersion` plus the SHA-256 of the exact
    rendered file (ADR-A8, ADR-A19). "The CV I sent you" has to mean one thing
    six months later, when the candidate's live CV has moved on.
  * **the posting.** `job_snapshot` copies the job by value, so an applicant
    can see what they applied to after the employer edits it (ADR-A23).
  * **the match score** — the column exists and stays empty until P13, because
    a score recomputed later against a changed profile is not the score the
    decision was made on.

Everything downstream subscribes to `application.submitted`. This service does
not know any of those subscribers exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common import ratelimit
from apps.common.errors import Conflict, InvalidState, NotAllowed, NotFound
from apps.matching.services import snapshots
from apps.notifications import events
from apps.resumes.services import renders as render_service

from ..models import (
    JOB_SNAPSHOT_SCHEMA,
    ActorKind,
    Application,
    ApplicationSource,
    CoverLetter,
    GrantBasis,
)
from . import letters, pipeline, visibility

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job
    from apps.resumes.models import ResumeVersion

MAX_ANSWER_LENGTH = 2000


def apply_to_job(
    *,
    job: Job,
    profile: CandidateProfile,
    actor: User,
    resume_version: ResumeVersion,
    cover_letter: CoverLetter | None = None,
    answers: dict[str, Any] | None = None,
    source: str = ApplicationSource.PLATFORM,
    cover_letter_text: str = "",
) -> Application:
    """Submit one application.

    `cover_letter_text` is a letter written on the apply screen. It is saved
    **inside** the application's transaction, after every guard has passed.
    The screen used to save it first, so each refused attempt — already
    applied, a missing answer, the daily limit — left another copy in the
    candidate's letter list (one account had ten identical "For Backend
    Engineer II" letters for a role it never applied to).

    Everything that can refuse runs **before** the daily limit is consumed and
    before the transaction opens, in that order. The limiter is a counter in
    Redis: a rollback does not give the count back, so an application refused
    for a missing answer would otherwise cost the candidate a slot out of a
    budget they never spent. Same shape as `messaging.send`.
    """
    _require_own_version(resume_version, profile)
    _require_open(job)
    _require_not_blocked(profile, job)
    _require_no_live_application(job, profile)
    _require_cooldown_elapsed(job, profile)
    cleaned_answers = _clean_answers(job, answers or {})
    letter_text = (cover_letter_text or "").strip()
    if letter_text and cover_letter is not None:
        raise InvalidState("Choose a saved letter or write a new one, not both.")
    if letter_text:
        letters.check_room(profile)

    # FR-6.13. Consumed here rather than in the view so a second entry point
    # cannot bypass it.
    ratelimit.check(ratelimit.APPLICATION_PER_USER, f"user:{actor.pk}")

    # Freeze the artefact (ADR-A8). Rendering here rather than later means the
    # hash on the application is of a file that provably exists. Outside the
    # transaction because it is CPU work holding no lock worth holding.
    render = render_service.render_version(version=resume_version, actor=actor)

    return _record_application(
        job=job,
        profile=profile,
        actor=actor,
        resume_version=resume_version,
        render_sha256=render.sha256,
        cover_letter=cover_letter,
        answers=cleaned_answers,
        source=source,
        letter_text=letter_text,
    )


@transaction.atomic
def _record_application(
    *,
    job: Job,
    profile: CandidateProfile,
    actor: User,
    resume_version: ResumeVersion,
    render_sha256: str,
    cover_letter: CoverLetter | None,
    answers: dict[str, Any],
    source: str,
    letter_text: str = "",
) -> Application:
    """The write. One transaction, nothing in it that can be undone by Redis."""
    # Re-checked inside the transaction: the guards above ran outside it, and
    # two simultaneous submissions would both have passed them. The re-check
    # narrows the window; the partial unique index closes it, and the
    # `IntegrityError` below is what turns the loser of a genuine race into the
    # sentence the candidate should read rather than a 500.
    #
    # This mattered in practice: a double-click on Submit is the normal way to
    # produce two simultaneous submissions, and until the P19 audit the second
    # one returned a server error.
    _require_no_live_application(job, profile)

    if letter_text:
        # Rolled back with everything else if the insert below loses a race.
        cover_letter = letters.save(
            profile=profile, actor=actor, content=letter_text, title=f"For {job.title}", job=job
        )

    application = _create_application(
        job=job,
        candidate=profile,
        resume_version=resume_version,
        submitted_render_sha256=render_sha256,
        cover_letter=cover_letter,
        stage=pipeline.default_stage_for(job),
        answers=answers,
        source=source if source in ApplicationSource.values else ApplicationSource.PLATFORM,
        job_snapshot=job.as_snapshot(),
        job_snapshot_schema_version=JOB_SNAPSHOT_SCHEMA,
        # Frozen here and never recomputed (FR-8.5). Six months from now the
        # candidate has different skills and the scorer has been retuned;
        # recomputing then would silently rewrite what the employer saw when
        # they made their decision. `{}` is still possible and still means an
        # absent score rather than a zero one — `snapshot_for` refuses to fail
        # a submission over a match calculation.
        match_snapshot=snapshots.snapshot_for(profile, job),
        applied_at=timezone.now(),
        last_activity_at=timezone.now(),
    )

    pipeline.record_event(
        application=application,
        actor=actor,
        actor_kind=ActorKind.CANDIDATE,
        type="application.submitted",
        to_stage=application.stage,
        visible_to_candidate=True,
    )

    # INV-4: the employer may now read this candidate, because of this.
    visibility.grant(
        candidate=profile,
        company=job.company,
        basis=GrantBasis.APPLICATION,
        application=application,
    )

    # Counted in the database rather than read-then-written: two applications
    # landing together must not lose one another.
    from django.db.models import F

    from apps.jobs.models import Job as JobModel

    JobModel.objects.filter(pk=job.pk).update(applications_count=F("applications_count") + 1)

    events.emit(
        "application.submitted",
        application,
        payload={
            "application_public_id": str(application.public_id),
            "job_public_id": str(job.public_id),
            "company_id": job.company_id,
            # Denormalised because the notification says it (SRS §13.1 rule 3).
            # A bare id would race: the posting can be renamed before the
            # consumer runs, and the recruiter should read the title they had.
            "job_title": job.title,
        },
    )
    audit.record(
        verb="application.submitted",
        actor=actor,
        target=application,
        company=job.company,
        metadata={"job": job.slug},
    )
    return application


@transaction.atomic
def withdraw(*, application: Application, actor: User, reason: str = "") -> Application:
    """The candidate pulls out (FR-6.5).

    Allowed at any point before an accepted offer, and never silent: the
    employer sees it on the same timeline the candidate does.
    """
    if application.withdrawn_at is not None:
        raise InvalidState("You have already withdrawn this application.")
    if _has_accepted_offer(application):
        raise InvalidState(
            "You accepted an offer for this role. Speak to the employer to unwind it."
        )

    application.withdrawn_at = timezone.now()
    application.last_activity_at = timezone.now()
    application.save(update_fields=["withdrawn_at", "last_activity_at", "updated_at"])

    pipeline.record_event(
        application=application,
        actor=actor,
        actor_kind=ActorKind.CANDIDATE,
        type="application.withdrawn",
        payload={"reason": reason.strip()[:200]},
        visible_to_candidate=True,
    )
    events.emit(
        "application.withdrawn",
        application,
        payload={
            "application_public_id": str(application.public_id),
            "company_id": application.job.company_id,
            # The team's notification read "They pulled out of a role you are
            # hiring for" — with several roles open, a sentence that says none.
            "job_title": application.job.title,
        },
    )
    audit.record(
        verb="application.withdrawn",
        actor=actor,
        target=application,
        company=application.job.company,
    )
    return application


def save_cover_letter(
    *,
    profile: CandidateProfile,
    actor: User,
    content: str,
    title: str = "",
    job: Job | None = None,
) -> CoverLetter:
    """Kept as the name the apply screen already calls (P8).

    The implementation moved to `letters.save` in P14, where the rest of the
    letter operations live. Two implementations of "store a cover letter" would
    have disagreed about the length cap and the count limit within a phase.
    """
    return letters.save(profile=profile, actor=actor, content=content, title=title, job=job)


# --- guards ---------------------------------------------------------------


def _create_application(**fields: Any) -> Application:
    """Insert, translating the partial unique index into a domain answer.

    The savepoint matters: an `IntegrityError` raised inside the caller's
    transaction poisons it, so every query after this point would fail too if
    the insert were not wrapped.
    """
    try:
        with transaction.atomic():
            return Application.objects.create(**fields)
    except IntegrityError:
        raise Conflict(
            "You have already applied to this role. Check your applications for its progress."
        ) from None


def _require_own_version(version: ResumeVersion, profile: CandidateProfile) -> None:
    if version.document.profile_id != profile.pk:
        raise NotAllowed("That CV does not belong to you.")
    if version.redacted_at is not None:
        raise InvalidState("That version was erased and can no longer be sent.")


def _require_open(job: Job) -> None:
    if not job.is_open:
        raise InvalidState("This role is no longer accepting applications.")
    if job.apply_mode != "platform":
        raise InvalidState("This employer takes applications on their own site.")


def _require_not_blocked(profile: CandidateProfile, job: Job) -> None:
    """A blocked company is blocked in both directions.

    Applying to a company you blocked would hand them exactly the data the
    block exists to withhold.
    """
    if profile.blocked_companies.filter(company_id=job.company_id).exists():
        raise InvalidState("You blocked this company. Unblock them to apply.")


def _require_no_live_application(job: Job, profile: CandidateProfile) -> None:
    if Application.objects.live().filter(job=job, candidate=profile).exists():
        raise Conflict("You already have a live application for this role.")


def _require_cooldown_elapsed(job: Job, profile: CandidateProfile) -> None:
    """FR-6.14: re-application after a rejection, once the cooldown passes.

    Zero days means no cooldown, which is the default: a company that wants a
    waiting period has to choose one.
    """
    days = job.application_cooldown_days or 0
    if days <= 0:
        return

    import datetime as dt

    since = timezone.now() - dt.timedelta(days=days)
    recent = (
        Application.objects.filter(job=job, candidate=profile, closed_at__isnull=False)
        .filter(closed_at__gt=since)
        .exists()
    )
    if recent:
        raise InvalidState(
            f"This employer asks candidates to wait {days} days before applying again."
        )


def _has_accepted_offer(application: Application) -> bool:
    from ..models import OfferState

    return application.offers.filter(state=OfferState.ACCEPTED).exists()


def _clean_answers(job: Job, answers: dict[str, Any]) -> dict[str, Any]:
    """Keep the answers to this job's questions, and refuse a missing required one.

    Keyed by question id and validated against the posting, so an answer to a
    question that was deleted does not travel with the application and a
    required question cannot be skipped by omitting the field.
    """
    questions = {str(question.pk): question for question in job.questions.all()}
    cleaned: dict[str, Any] = {}

    for key, question in questions.items():
        # A form posts string keys; a service caller may pass integers. Both
        # are normalised here rather than at every call site.
        raw = answers.get(key, "")
        if raw == "" and key.isdigit():
            raw = answers.get(int(key), "")  # type: ignore[call-overload]
        value = _clean_answer(raw, question)
        if question.is_required and value in ("", [], None):
            raise InvalidState(f"Answer: {question.text}")
        if value not in ("", [], None):
            cleaned[key] = value

    unknown = set(map(str, answers)) - set(questions)
    if unknown:
        raise NotFound("This posting's questions changed while you were answering them.")
    return cleaned


def _clean_answer(raw: Any, question: Any) -> Any:
    from apps.jobs.models import QuestionKind

    if question.kind == QuestionKind.BOOLEAN:
        return raw in (True, "on", "true", "True", "yes", "1")
    if question.kind == QuestionKind.NUMBER:
        try:
            return float(str(raw).strip()) if str(raw).strip() else ""
        except ValueError:
            raise InvalidState(f"{question.text} takes a number.") from None
    if question.kind == QuestionKind.MULTI_CHOICE:
        chosen = raw if isinstance(raw, list) else [raw]
        return [str(item) for item in chosen if str(item) in (question.options or [])]
    if question.kind == QuestionKind.SINGLE_CHOICE:
        return str(raw) if str(raw) in (question.options or []) else ""
    return str(raw or "").strip()[:MAX_ANSWER_LENGTH]
