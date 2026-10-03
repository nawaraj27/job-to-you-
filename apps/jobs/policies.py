"""
Authorization for jobs (SRS §2.3, §2.5, layer 3).

Two rules, and the second is the one that makes a small team usable:

  * **Reads are company-scoped.** Every employer-side queryset starts from
    `Job.objects.for_company(company)`, so the policy layer never has to
    compensate for a query that could have returned another tenant's row.
  * **Write access is a grant *or* an assignment.** A recruiter does not hold
    `job.write_any` by default, and requiring it would mean a five-person
    company granting everybody everything. Being assigned to a posting is what
    lets them edit it (§2.3).

Every rule here has a passing negative test (DEVELOPER.md §8.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import QuerySet

from apps.common.errors import InvalidState, NotFound
from apps.companies import policies as company_policies
from apps.companies.permissions import Grant, has

from .models import Job, JobAssignment

if TYPE_CHECKING:  # pragma: no cover
    from apps.companies.models import CompanyMembership


def is_assigned(job: Job, membership: CompanyMembership | None) -> bool:
    if membership is None:
        return False
    return JobAssignment.objects.filter(job=job, membership=membership).exists()


def can_read(job: Job, membership: CompanyMembership | None) -> bool:
    """The Jobs column of SRS §2.3, for reading.

    An Interviewer's is "None"; a Hiring Manager's is "View assigned jobs";
    everybody else in the company reads every posting.
    """
    from apps.companies.permissions import Role

    if membership is None or not membership.is_active or membership.company_id != job.company_id:
        return False
    if not company_policies.can_open_section(membership, "jobs"):
        return False
    if membership.role == Role.HIRING_MANAGER:
        return has(membership, Grant.JOB_WRITE_ANY) or is_assigned(job, membership)
    return True


def readable_jobs(queryset: QuerySet[Job], membership: CompanyMembership) -> QuerySet[Job]:
    """The same rule as `can_read`, over a queryset of one company's postings."""
    from apps.companies.permissions import Role

    if not company_policies.can_open_section(membership, "jobs"):
        return queryset.none()
    if membership.role == Role.HIRING_MANAGER and not has(membership, Grant.JOB_WRITE_ANY):
        return queryset.filter(assignments__membership=membership).distinct()
    return queryset


def can_write(job: Job, membership: CompanyMembership | None) -> bool:
    """Either a company-wide grant, or this posting in particular."""
    if membership is None or not membership.is_active:
        return False
    if membership.company_id != job.company_id:
        return False
    return has(membership, Grant.JOB_WRITE_ANY) or is_assigned(job, membership)


def can_manage_assignments(membership: CompanyMembership | None) -> bool:
    """Who may put people on a posting or take them off (decision D-13).

    Assignment is what opens a job's applicants to a recruiter or hiring
    manager, so handing it out is a grant of access. It belongs to members with
    company-wide job access — Owner and Company Admin, or `job.write_any` given
    explicitly — not to every recruiter who can edit the posting.
    """
    return has(membership, Grant.JOB_WRITE_ANY)


def require_can_write(job: Job, membership: CompanyMembership | None) -> Job:
    """NotFound rather than NotAllowed for a foreign company's posting.

    A 403 confirms that a job exists inside a company the actor cannot see,
    which is a disclosure in a two-sided marketplace (SRS §2.2).
    """
    if membership is None or membership.company_id != job.company_id:
        raise NotFound("That job does not exist, or you cannot see it.")
    if not can_write(job, membership):
        from apps.common.errors import NotAllowed

        raise NotAllowed(
            "You can edit the postings you are assigned to. Ask an admin for access to this one."
        )
    return job


def require_can_publish(job: Job, membership: CompanyMembership | None) -> Job:
    """Publishing is a company-wide decision, never an assignment-scoped one.

    An assigned recruiter may write a posting; making it visible to the market
    is a separate grant, because a live job is the company speaking.
    """
    require_can_write(job, membership)
    company_policies.require_grant(membership, Grant.JOB_PUBLISH)
    return job


#: Roles whose Jobs column in SRS §2.3 includes creating a posting: Owner and
#: Company Admin ("Full") and Recruiter ("Create and edit own jobs"). Hiring
#: Manager ("View assigned jobs"), Interviewer ("None") and Viewer ("Read") do
#: not.
_CREATING_ROLES = frozenset({"owner", "admin", "recruiter"})


def can_create_jobs(membership: CompanyMembership | None) -> bool:
    """Whether this member may start a new posting."""
    if membership is None or not membership.is_active:
        return False
    return membership.role in _CREATING_ROLES or has(membership, Grant.JOB_WRITE_ANY)


def require_can_manage_jobs(membership: CompanyMembership | None) -> None:
    """Creating a posting at all.

    Membership alone used to be enough, so a Viewer or an Interviewer could
    create draft postings by posting the form.
    """
    company_policies.require_membership(membership)
    if not can_create_jobs(membership):
        from apps.common.errors import NotAllowed

        raise NotAllowed("Your role can read postings but not create them. Ask an owner or admin.")


def require_editable(job: Job) -> Job:
    if job.deleted_at is not None:
        raise InvalidState("That posting has been deleted.")
    if not job.is_editable:
        raise InvalidState("A closed or archived posting cannot be edited. Reopen it first.")
    return job


def require_visible(job: Job | None) -> Job:
    """What a candidate may open.

    Draft, closed, expired and archived postings are all NotFound to the
    public: an unpublished job is the company's own information, and a URL that
    behaves differently for a draft leaks that one exists.
    """
    if job is None or not job.is_published:
        raise NotFound("That job is no longer listed.")
    return job
