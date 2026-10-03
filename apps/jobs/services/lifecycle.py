"""
The posting lifecycle (SRS §7.4, §7.5).

Every status change goes through the state machine below. That is the whole
point of having one: "can this job be published" is answered in one table
rather than in whichever view happens to be handling the click, and a
transition nobody declared is refused rather than silently allowed.

Publication is the transition with teeth. A job cannot be published without the
things a candidate needs to decide whether to apply — a title, a description,
and pay unless the employer has deliberately hidden it — because a posting that
says nothing wastes the time of everyone who opens it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import InvalidState, StaleWrite
from apps.common.state_machine import StateMachine, Transition
from apps.companies.permissions import Grant
from apps.notifications import events

from ..models import Job, JobStatus

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import CompanyMembership

logger = logging.getLogger(__name__)

#: The only legal moves (SRS §7.5). A closed job reopens by being republished,
#: which is deliberate: reopening is a decision worth making explicitly, and it
#: gives the posting a fresh publication date rather than a stale one.
LIFECYCLE = StateMachine(
    "status",
    transitions=[
        Transition(JobStatus.DRAFT, JobStatus.PUBLISHED, permission=Grant.JOB_PUBLISH),
        Transition(JobStatus.DRAFT, JobStatus.ARCHIVED),
        Transition(JobStatus.PUBLISHED, JobStatus.CLOSED),
        Transition(JobStatus.PUBLISHED, JobStatus.EXPIRED),
        Transition(JobStatus.PUBLISHED, JobStatus.DRAFT, permission=Grant.JOB_PUBLISH),
        Transition(JobStatus.PUBLISHED, JobStatus.PAUSED, permission=Grant.JOB_PUBLISH),
        Transition(JobStatus.PAUSED, JobStatus.PUBLISHED, permission=Grant.JOB_PUBLISH),
        Transition(JobStatus.PAUSED, JobStatus.CLOSED),
        Transition(JobStatus.CLOSED, JobStatus.PUBLISHED, permission=Grant.JOB_PUBLISH),
        Transition(JobStatus.CLOSED, JobStatus.ARCHIVED),
        Transition(JobStatus.EXPIRED, JobStatus.PUBLISHED, permission=Grant.JOB_PUBLISH),
        Transition(JobStatus.EXPIRED, JobStatus.ARCHIVED),
        Transition(JobStatus.ARCHIVED, JobStatus.DRAFT),
    ],
)


@transaction.atomic
def publish(
    *,
    job: Job,
    actor: User,
    membership: CompanyMembership,
    row_version: int | None = None,
) -> Job:
    """Make a posting visible to candidates."""
    _guard_version(job, row_version)
    _require_publishable(job)

    LIFECYCLE.require(job, JobStatus.PUBLISHED, actor_permissions=membership.grants)

    job.status = JobStatus.PUBLISHED
    job.published_at = job.published_at or timezone.now()
    job.closed_at = None
    job.closed_reason = ""
    job.archived_at = None
    _save(job, ["status", "published_at", "closed_at", "closed_reason", "archived_at"])

    events.emit(
        "job.published",
        job,
        payload={
            "job_public_id": str(job.public_id),
            "company_id": job.company_id,
            "title": job.title,
            # For the followers' notice (FOL-01): who posted it, and where it is.
            "company_name": job.company.name,
            "target_url": reverse("jobs:detail", args=[job.slug]),
        },
    )
    audit.record(
        verb="job.published",
        actor=actor,
        target=job,
        company=job.company,
        metadata={"title": job.title},
    )
    return job


@transaction.atomic
def unpublish(
    *, job: Job, actor: User, membership: CompanyMembership, row_version: int | None = None
) -> Job:
    """Back to draft. Used when a posting went out before it was ready."""
    _guard_version(job, row_version)
    LIFECYCLE.require(job, JobStatus.DRAFT, actor_permissions=membership.grants)

    job.status = JobStatus.DRAFT
    _save(job, ["status"])

    audit.record(verb="job.unpublished", actor=actor, target=job, company=job.company)
    return job


@transaction.atomic
def close(
    *,
    job: Job,
    actor: User,
    membership: CompanyMembership,
    reason: str = "",
    row_version: int | None = None,
) -> Job:
    """Stop accepting applications.

    The reason is recorded because "filled" and "cancelled" are different
    facts, and a candidate mid-pipeline is owed the difference.
    """
    _guard_version(job, row_version)
    LIFECYCLE.require(job, JobStatus.CLOSED, actor_permissions=membership.grants)

    job.status = JobStatus.CLOSED
    job.closed_at = timezone.now()
    job.closed_reason = (reason or "").strip()[:200]
    _save(job, ["status", "closed_at", "closed_reason"])

    events.emit(
        "job.closed",
        job,
        payload={
            "job_public_id": str(job.public_id),
            "company_id": job.company_id,
            "reason": job.closed_reason,
        },
    )
    audit.record(
        verb="job.closed",
        actor=actor,
        target=job,
        company=job.company,
        metadata={"reason": job.closed_reason},
    )
    return job


@transaction.atomic
def expire(*, job: Job, actor: User | None = None) -> Job:
    """Close a posting whose deadline passed.

    Separate from `close` and run by the scheduler: a posting nobody closed is
    not the same fact as one somebody decided to close, and a job board full of
    roles that quietly stayed open is the reason candidates stop trusting dates.
    """
    LIFECYCLE.require(job, JobStatus.EXPIRED)

    job.status = JobStatus.EXPIRED
    job.closed_at = timezone.now()
    job.closed_reason = job.closed_reason or "Closing date passed."
    _save(job, ["status", "closed_at", "closed_reason"])

    events.emit(
        "job.expired",
        job,
        payload={"job_public_id": str(job.public_id), "company_id": job.company_id},
    )
    audit.record(verb="job.expired", actor=actor, target=job, company=job.company)
    return job


@transaction.atomic
def archive(
    *, job: Job, actor: User, membership: CompanyMembership, row_version: int | None = None
) -> Job:
    """Out of the working list, still readable."""
    _guard_version(job, row_version)
    LIFECYCLE.require(job, JobStatus.ARCHIVED, actor_permissions=membership.grants)

    job.status = JobStatus.ARCHIVED
    job.archived_at = timezone.now()
    _save(job, ["status", "archived_at"])

    audit.record(verb="job.archived", actor=actor, target=job, company=job.company)
    return job


@transaction.atomic
def restore(
    *, job: Job, actor: User, membership: CompanyMembership, row_version: int | None = None
) -> Job:
    """Bring an archived posting back as a draft."""
    _guard_version(job, row_version)
    LIFECYCLE.require(job, JobStatus.DRAFT, actor_permissions=membership.grants)

    job.status = JobStatus.DRAFT
    job.archived_at = None
    _save(job, ["status", "archived_at"])

    audit.record(verb="job.restored", actor=actor, target=job, company=job.company)
    return job


def expire_due(*, limit: int = 500) -> int:
    """Expire every published posting whose closing date has passed.

    Row by row rather than one bulk `UPDATE`: each expiry emits an event and
    writes an audit row, and a bulk update that skipped both would leave the
    candidates watching that job with no notification and no trail.
    """
    expired = 0
    for job in Job.objects.expiring().select_related("company")[:limit]:
        try:
            expire(job=job)
            expired += 1
        except InvalidState:
            # Raced with someone closing it by hand. Their decision wins.
            logger.info("job %s was already out of published state", job.pk)
    return expired


# --- internals ------------------------------------------------------------


def _require_publishable(job: Job) -> None:
    """What a candidate needs before a posting is worth their time."""
    missing = []
    if not job.title.strip():
        missing.append("a title")
    if not job.description.strip():
        missing.append("a description")
    if job.salary_visible and job.salary_min is None and job.salary_max is None:
        missing.append("a salary range (or switch pay visibility off)")
    if job.apply_mode == "external" and not job.external_url:
        missing.append("the application URL")

    if missing:
        raise InvalidState(f"Before publishing, add {', '.join(missing)}.")

    if job.closes_at is not None and job.closes_at <= timezone.now():
        raise InvalidState("That closing date has already passed.")


def _guard_version(job: Job, expected: int | None) -> None:
    if expected is not None and expected != job.row_version:
        raise StaleWrite(
            "Someone else changed this posting while you were editing it. "
            "Reload to see their version."
        )


def _save(job: Job, fields: list[str]) -> None:
    job.row_version += 1
    job.save(update_fields=[*fields, "row_version", "updated_at"])


@transaction.atomic
def take_down(*, job: Job, actor: User, reason: str) -> Job:
    """A platform moderator unpublishes a posting (SRS §2.3, Platform Moderator).

    Not `unpublish`: that is the company's own action and is gated by the
    company's grants, which a moderator does not hold. The posting goes back to
    draft, the reason is recorded, and the company's editors are told why.
    """
    reason = (reason or "").strip()
    if not reason:
        raise InvalidState("Record why this posting is being taken down.")
    if job.status != JobStatus.PUBLISHED:
        raise InvalidState("Only a published posting can be taken down.")

    job.status = JobStatus.DRAFT
    _save(job, ["status"])
    audit.record(
        verb="job.taken_down",
        actor=actor,
        target=job,
        company=job.company,
        metadata={"reason": reason, "title": job.title},
    )
    events.emit(
        "job.taken_down",
        job,
        payload={
            "job_public_id": str(job.public_id),
            "company_id": job.company_id,
            "title": job.title,
            "reason": reason,
            # Straight to the posting, where the company can fix it and
            # publish again, rather than to the notification feed.
            "target_url": reverse("employer:job_detail", args=[job.company.slug, job.public_id]),
        },
    )
    return job


@transaction.atomic
def pause(
    *, job: Job, actor: User, membership: CompanyMembership, row_version: int | None = None
) -> Job:
    """Take a live posting off the site for now (FR-5.1).

    Candidates stop seeing it and cannot apply; applications already made carry
    on. It keeps its publication date, because it is coming back.
    """
    _guard_version(job, row_version)
    LIFECYCLE.require(job, JobStatus.PAUSED, actor_permissions=membership.grants)

    job.status = JobStatus.PAUSED
    _save(job, ["status"])
    audit.record(verb="job.paused", actor=actor, target=job, company=job.company)
    events.emit(
        "job.paused",
        job,
        payload={"job_public_id": str(job.public_id), "company_id": job.company_id},
    )
    return job


@transaction.atomic
def resume(
    *, job: Job, actor: User, membership: CompanyMembership, row_version: int | None = None
) -> Job:
    """Put a paused posting back on the site.

    Not `publish`: nothing new happened to the role, so `job.published` and the
    alert fan-out it drives are not repeated for the same posting.
    """
    if job.status != JobStatus.PAUSED:
        raise InvalidState("Only a paused posting can be resumed.")
    _guard_version(job, row_version)
    LIFECYCLE.require(job, JobStatus.PUBLISHED, actor_permissions=membership.grants)

    job.status = JobStatus.PUBLISHED
    _save(job, ["status"])
    audit.record(verb="job.resumed", actor=actor, target=job, company=job.company)
    events.emit(
        "job.resumed",
        job,
        payload={"job_public_id": str(job.public_id), "company_id": job.company_id},
    )
    return job
