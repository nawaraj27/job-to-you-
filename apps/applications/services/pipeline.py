"""
The pipeline: stages, and moving an application between them (SRS §5.8).

There is no `StateMachine` here, and that is deliberate rather than an
omission. A company defines its own stages (FR-6.3), so the legal moves are
data, not a table somebody wrote at compile time. What is enforced instead:

  * a terminal stage closes the application, and reopening it is an explicit
    act rather than a side effect of moving back;
  * moving into a stage that reveals contact details unlocks them, once, with
    an event and an audit row (ADR-A34);
  * every move appends to the one timeline (FR-6.4).

The one thing a company cannot configure away is that `kind` means what it
says: rename "Hired" to anything you like, but a `terminal` stage closes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState, NotFound
from apps.companies.permissions import Grant, has
from apps.notifications import events

from ..models import (
    ActorKind,
    Application,
    ApplicationEvent,
    PipelineStage,
    StageKind,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import Company, CompanyMembership
    from apps.jobs.models import Job

#: The default pipeline (FR-6.3). Seeded once, shared by every company that has
#: not defined its own. Contact unlocks at Interview: the point at which an
#: employer has demonstrably decided to speak to the person (ADR-A34).
DEFAULT_STAGES: tuple[dict, ...] = (
    {"key": "new", "label": "New", "kind": StageKind.OPEN, "order": 10},
    {"key": "reviewing", "label": "Reviewing", "kind": StageKind.OPEN, "order": 20},
    {"key": "shortlisted", "label": "Shortlisted", "kind": StageKind.OPEN, "order": 30},
    {
        "key": "interview",
        "label": "Interview",
        "kind": StageKind.INTERVIEW,
        "order": 40,
        "reveals_contact": True,
    },
    {"key": "offer", "label": "Offer", "kind": StageKind.OFFER, "order": 50},
    {"key": "hired", "label": "Hired", "kind": StageKind.TERMINAL, "order": 60},
    {"key": "declined", "label": "Not proceeding", "kind": StageKind.TERMINAL, "order": 70},
)

MAX_STAGES = 20


def ensure_default_stages() -> int:
    """Create the platform default stage set if it is missing.

    Idempotent, and called from the apply path rather than only from a seed
    command: an application that cannot be created because nobody ran a
    management command is a failure the candidate experiences.
    """
    created = 0
    for spec in DEFAULT_STAGES:
        _, was_created = PipelineStage.objects.get_or_create(
            company=None, key=spec["key"], defaults={**spec}
        )
        created += int(was_created)
    return created


def stages_for(company: Company) -> list[PipelineStage]:
    ensure_default_stages()
    return list(PipelineStage.objects.for_company(company))


def default_stage_for(job: Job) -> PipelineStage:
    """Where a new application lands."""
    stages = stages_for(job.company)
    if not stages:  # pragma: no cover - ensure_default_stages just ran
        raise InvalidState("This company has no pipeline stages.")
    return stages[0]


def stage_by_key(company: Company, key: str) -> PipelineStage:
    for stage in stages_for(company):
        if stage.key == key:
            return stage
    raise NotFound("That stage is not part of this company's pipeline.")


@transaction.atomic
def move(
    *,
    application: Application,
    actor: User,
    membership: CompanyMembership,
    stage: PipelineStage,
    reason: str = "",
    message: str = "",
) -> Application:
    """Move one application to a stage.

    `reason` is required for a terminal move and recorded on the application;
    `message` is what the candidate is told, and rides in the event payload
    rather than in a column, because it belongs to this moment rather than to
    the application.
    """
    _require_same_pipeline(application, stage)

    # Two people moving the same applicant at once each loaded it at its old
    # stage, so both passed the "already at" check and the candidate was told
    # twice. The row lock serialises them, and the checks below then read what
    # the first move actually left behind.
    Application.objects.select_for_update().filter(pk=application.pk).values_list("pk").get()
    application.refresh_from_db(
        fields=["stage", "closed_at", "closed_reason", "withdrawn_at", "contact_revealed_at"]
    )

    if application.is_withdrawn:
        # Only the candidate can undo a withdrawal, and the way they do it is
        # by applying again. An employer reviving it would be the platform
        # putting somebody back in a process they left.
        raise InvalidState("The candidate withdrew this application.")
    if stage.pk == application.stage_id:
        raise InvalidState(f"This application is already at {stage.label}.")

    # Reopening: moving a closed application back to a live stage. It is the
    # same decision as closing it and needs the same grant — a rejection that
    # anyone could quietly reverse is not a decision anybody is accountable for.
    reopening = application.closed_at is not None and stage.kind != StageKind.TERMINAL
    if reopening and not has(membership, Grant.APPLICATION_DECIDE):
        from apps.common.errors import NotAllowed

        raise NotAllowed("Reopening a closed application needs the decide permission.")

    if stage.kind == StageKind.TERMINAL:
        if not has(membership, Grant.APPLICATION_DECIDE):
            from apps.common.errors import NotAllowed

            raise NotAllowed("Closing an application needs the decide permission.")
        if not reason.strip():
            # FR-6.7: a rejection carries a reason. Not for the paperwork —
            # a pipeline with no reasons cannot tell a company why it loses
            # people, which is the one report worth reading.
            raise InvalidState("Say why this application is being closed.")

    previous = application.stage
    application.stage = stage
    application.last_activity_at = timezone.now()

    fields = ["stage", "last_activity_at", "updated_at"]
    if stage.kind == StageKind.TERMINAL:
        application.closed_at = timezone.now()
        application.closed_reason = reason.strip()[:60]
        fields += ["closed_at", "closed_reason"]
    elif application.closed_at is not None:
        application.closed_at = None
        application.closed_reason = ""
        fields += ["closed_at", "closed_reason"]

    application.save(update_fields=fields)

    record_event(
        application=application,
        actor=actor,
        actor_kind=ActorKind.EMPLOYER,
        type="application.stage_changed",
        from_stage=previous,
        to_stage=stage,
        payload={"reason": application.closed_reason, "message": message.strip()[:2000]},
        # The candidate is told, always. A stage change they cannot see is how
        # "we'll be in touch" became the most disliked sentence in hiring.
        visible_to_candidate=True,
    )

    if stage.reveals_contact and application.contact_revealed_at is None:
        reveal_contact(application=application, actor=actor, membership=membership, basis="stage")

    events.emit(
        "application.stage_changed",
        application,
        payload={
            "application_public_id": str(application.public_id),
            "company_id": application.job.company_id,
            "from_stage": previous.key,
            "to_stage": stage.key,
            # The words the candidate's notification is written from. Not in
            # `required`, because events already stored do not have them and a
            # missing key renders blank rather than failing — but without them
            # the notification read "There is an update on one of your
            # applications", which is a sentence that tells somebody to go and
            # look through all of them.
            "job_title": application.job.title,
            "company_name": application.job.company.name,
            # What the candidate may be told the stage is called (FR-6.10).
            # This carried `stage.label`, so a stage a company marked private
            # reached the candidate by email as "You are now at the Hiring
            # manager doubts stage" while every screen correctly said "Under
            # review".
            "stage_label": stage.public_label,
            # Appended to the candidate's notification when an application
            # closes, so the reason arrives in the one notification they get.
            "reason_line": (
                f" {application.closed_reason}"
                if stage.kind == StageKind.TERMINAL and application.closed_reason
                else ""
            ),
        },
    )
    if stage.kind == StageKind.TERMINAL:
        events.emit(
            "application.closed",
            application,
            payload={
                "application_public_id": str(application.public_id),
                "company_id": application.job.company_id,
                "reason": application.closed_reason,
                "job_title": application.job.title,
                "company_name": application.job.company.name,
            },
        )

    audit.record(
        verb="application.stage_changed",
        actor=actor,
        target=application,
        company=application.job.company,
        metadata={"from": previous.key, "to": stage.key, "reason": application.closed_reason},
    )
    return application


def move_many(
    *,
    applications: list[Application],
    actor: User,
    membership: CompanyMembership,
    stage: PipelineStage,
    reason: str = "",
    message: str = "",
) -> dict[str, Any]:
    """Bulk move (FR-6.6).

    One transaction per application rather than one for the batch: moving
    forty applications where the thirty-ninth is already at that stage must
    move the other thirty-nine, not fail the lot. The count of refusals comes
    back so the screen can say what happened.
    """
    if stage.kind == StageKind.TERMINAL and not reason.strip():
        # Checked once for the batch. Per application, the missing reason was
        # an `InvalidState` counted as "skipped", so closing four applicants
        # with no reason reported "Moved 0 to Not proceeding. 4 were already
        # there." — nothing had moved and nothing was already there.
        raise InvalidState("Say why these applications are being closed.")

    moved = skipped = 0
    refused: list[str] = []
    for application in applications:
        if application.stage_id == stage.pk:
            skipped += 1
            continue
        try:
            move(
                application=application,
                actor=actor,
                membership=membership,
                stage=stage,
                reason=reason,
                message=message,
            )
            moved += 1
        except InvalidState as exc:
            refused.append(exc.message)
    return {"moved": moved, "skipped": skipped, "refused": len(refused), "reasons": refused}


@transaction.atomic
def reveal_contact(
    *,
    application: Application,
    actor: User,
    membership: CompanyMembership,
    basis: str = "manual",
    reason: str = "",
) -> Application:
    """Unlock the candidate's contact details (ADR-A34).

    Recorded three ways — on the application, in the timeline, and in the audit
    log — because "who saw my phone number and when" is a question a candidate
    is entitled to a real answer to.
    """
    if application.contact_revealed_at is not None:
        return application
    if basis == "manual" and not reason.strip():
        raise InvalidState("Give a reason for unlocking contact details early.")

    application.contact_revealed_at = timezone.now()
    application.save(update_fields=["contact_revealed_at", "updated_at"])

    record_event(
        application=application,
        actor=actor,
        actor_kind=ActorKind.EMPLOYER,
        type="application.contact_revealed",
        payload={"basis": basis, "reason": reason.strip()[:200]},
        # The candidate sees this. It is their contact information.
        visible_to_candidate=True,
    )
    events.emit(
        "application.contact_revealed",
        application,
        payload={
            "application_public_id": str(application.public_id),
            "company_id": application.job.company_id,
            "basis": basis,
        },
    )
    audit.record(
        verb="application.contact_revealed",
        actor=actor,
        target=application,
        company=application.job.company,
        metadata={"basis": basis, "reason": reason.strip()[:200]},
    )
    return application


def record_event(
    *,
    application: Application,
    actor: User | None,
    actor_kind: str,
    type: str,
    from_stage: PipelineStage | None = None,
    to_stage: PipelineStage | None = None,
    payload: dict | None = None,
    visible_to_candidate: bool = False,
) -> ApplicationEvent:
    """Append to the one timeline. The only way a row gets in."""
    return ApplicationEvent.objects.create(
        application=application,
        actor=actor,
        actor_kind=actor_kind,
        type=type,
        from_stage=from_stage,
        to_stage=to_stage,
        payload=payload or {},
        visible_to_candidate=visible_to_candidate,
    )


# --- company pipeline configuration ---------------------------------------


@transaction.atomic
def configure_stages(*, company: Company, actor: User, specs: list[dict]) -> list[PipelineStage]:
    """Replace a company's stage set.

    Replaced rather than edited in place because the input is a whole ordered
    list from one screen. Stages still carrying applications are kept and
    updated instead of deleted — `PROTECT` on `Application.stage` would refuse
    the delete anyway, and a screen that fails with a database error after
    someone reordered seven rows is a screen nobody trusts.
    """
    if not specs:
        raise InvalidState("A pipeline needs at least one stage.")
    if len(specs) > MAX_STAGES:
        raise InvalidState(f"A pipeline holds up to {MAX_STAGES} stages.")
    if not any(spec.get("kind") == StageKind.TERMINAL for spec in specs):
        raise InvalidState("A pipeline needs somewhere for an application to end.")

    existing = {stage.key: stage for stage in PipelineStage.objects.filter(company=company)}
    kept: list[PipelineStage] = []

    for index, spec in enumerate(specs):
        key = str(spec.get("key") or "").strip()[:40]
        label = str(spec.get("label") or "").strip()[:60]
        if not key or not label:
            raise InvalidState("Every stage needs a name.")
        kind = spec.get("kind", StageKind.OPEN)
        if kind not in StageKind.values:
            raise InvalidState(f"{kind!r} is not a kind of stage.")

        stage = existing.pop(key, None) or PipelineStage(company=company, key=key)
        stage.label = label
        stage.order = (index + 1) * 10
        stage.kind = kind
        stage.is_visible_to_candidate = bool(spec.get("is_visible_to_candidate", True))
        stage.candidate_label = str(spec.get("candidate_label") or "").strip()[:60]
        stage.reveals_contact = bool(spec.get("reveals_contact", kind == StageKind.INTERVIEW))
        stage.save()
        kept.append(stage)

    # Whatever the screen dropped. In use means it stays: PROTECT would refuse
    # the delete, and losing a stage would strand every application on it.
    for stage in existing.values():
        if stage.applications.exists():
            raise InvalidState(
                f"{stage.label} still has applications on it. Move them before removing it."
            )
        stage.delete()

    audit.record(
        verb="pipeline.configured",
        actor=actor,
        company=company,
        metadata={"stages": [stage.key for stage in kept]},
    )
    return kept


def _require_same_pipeline(application: Application, stage: PipelineStage) -> None:
    """A stage from another company is not a stage this application can reach."""
    company_id = application.job.company_id
    if stage.company_id not in (None, company_id):
        raise NotFound("That stage is not part of this company's pipeline.")
