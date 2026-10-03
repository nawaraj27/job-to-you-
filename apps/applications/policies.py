"""
Authorization for applications (SRS §2.3, §2.5, INV-4).

Four rules, and the third is the one this phase exists to get right:

  * **an employer read is company-scoped** — `Application.objects.for_company`;
  * **a candidate read is owner-scoped** — their own applications, nobody's else;
  * **a candidate read *by* an employer needs a live visibility grant** (INV-4),
    which is a row, not an inference from "they applied somewhere";
  * **contact details are not part of an application** until the stage reveals
    them (ADR-A34), so the check lives at the boundary that serialises them.

Everything refuses with NotFound rather than NotAllowed across a tenant
boundary: a 403 confirms that an application exists inside a company the actor
cannot see, and in a two-sided marketplace that is a disclosure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.common.errors import InvalidState, NotAllowed, NotFound
from apps.companies import policies as company_policies
from apps.companies.permissions import Grant, has

from .models import Application

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import CompanyMembership


#: How far one member's application reads reach (SRS §2.3).
SCOPE_COMPANY = "company"
SCOPE_ASSIGNED = "assigned"
SCOPE_INTERVIEWS = "interviews"

#: How long an Interviewer keeps an application after the interview (§2.3,
#: "from scheduling until 30 days after the interview").
INTERVIEWER_DAYS_AFTER = 30


def read_scope(membership: CompanyMembership) -> str:
    """Which applications one member may read, as a rule rather than a query.

    From the Applications and Candidates columns of SRS §2.3:

      * Owner and Company Admin read company-wide;
      * an Interviewer reads the applications they are on an interview panel
        for, and nothing else;
      * a Recruiter or Hiring Manager reads applicants to the jobs they are
        explicitly assigned to (decision D-13 — recruiters used to read
        company-wide by default);
      * a Viewer reads the lists company-wide, unless the company switched on
        `strict_assignment_scoping`, which narrows them to assignments too.

    `application.read_any` held *explicitly* on the membership widens any of
    these back to company-wide: an owner deciding to give one person that
    reach is not a silent default.
    """
    from apps.companies.permissions import Role

    explicit = Grant.APPLICATION_READ_ANY in set(membership.permissions or [])
    if explicit or membership.role in (Role.OWNER, Role.ADMIN):
        return SCOPE_COMPANY
    if membership.role == Role.INTERVIEWER:
        return SCOPE_INTERVIEWS
    if membership.role in (Role.RECRUITER, Role.HIRING_MANAGER):
        return SCOPE_ASSIGNED
    if membership.company.strict_assignment_scoping:
        return SCOPE_ASSIGNED
    return SCOPE_COMPANY


def can_read(application: Application, membership: CompanyMembership | None) -> bool:
    """Whether one team member may open one application."""
    if membership is None or not membership.is_active:
        return False
    if membership.company_id != application.job.company_id:
        return False
    return Application.objects.visible_to(membership).filter(pk=application.pk).exists()


def reader_user_ids(application: Application, *, detail: bool = True) -> list[int]:
    """The people on the team who may read this application (D-13).

    For fan-out — a notification about an applicant goes to the people who can
    open it, not to everybody in the company. `detail` leaves out Viewers, who
    read lists but cannot open the application a notification links to.
    """
    from apps.companies.models import CompanyMembership
    from apps.companies.permissions import Role

    members = (
        CompanyMembership.objects.active()
        .filter(company_id=application.job.company_id, user__is_active=True)
        .select_related("company")
    )
    assigned = set(application.job.assignments.values_list("membership_id", flat=True))
    readers = []
    for member in members:
        if detail and not can_open_detail(member):
            continue
        scope = read_scope(member)
        if scope == SCOPE_COMPANY:
            readers.append(member.user_id)
        elif scope == SCOPE_ASSIGNED:
            if member.pk in assigned:
                readers.append(member.user_id)
        elif member.role == Role.INTERVIEWER and can_read(application, member):
            readers.append(member.user_id)
    return readers


def require_can_read(application: Application, membership: CompanyMembership | None) -> Application:
    if membership is None or membership.company_id != application.job.company_id:
        raise NotFound("That application does not exist, or you cannot see it.")
    if not can_read(application, membership):
        raise NotFound("That application does not exist, or you cannot see it.")
    return application


def can_open_detail(membership: CompanyMembership | None) -> bool:
    """A Viewer reads "lists and aggregates" and has no Candidates access (§2.3).

    The applicant detail screen is the candidate: their CV, answers, notes and
    ratings. The list with names and stages is what a Viewer is given.
    """
    from apps.companies.permissions import Role

    return membership is not None and membership.role != Role.VIEWER


def require_can_open_detail(membership: CompanyMembership | None) -> None:
    if not can_open_detail(membership):
        raise NotAllowed("Your role can see the applicant list but not individual applications.")


#: Kinds of change to an application, from the Applications column of §2.3.
WRITE_COMMENT = "comment"  # notes and ratings
WRITE_ORGANISE = "organise"  # stage moves, tags, interview scheduling


def can_write(
    application: Application, membership: CompanyMembership | None, kind: str = WRITE_ORGANISE
) -> bool:
    """Whether one member may change one application (SRS §2.3).

      * Owner and Company Admin: anything their grants allow.
      * Recruiter: full on assigned jobs (D-13) — writes need an assignment to
        the job, or `job.write_any` together with a read that reaches it.
      * Hiring Manager: "Rate and comment on assigned jobs; stage moves and
        rejections only if granted" — comments on assigned jobs, and moves only
        with `application.decide`.
      * Interviewer and Viewer: read-only.

    Decisions and offers are also gated by their own grants at the call site;
    this answers only whether the application is theirs to touch at all.
    """
    from apps.companies.permissions import Role

    if not can_read(application, membership):
        return False
    assert membership is not None  # narrowed by can_read
    if membership.role in (Role.OWNER, Role.ADMIN):
        return True
    if membership.role not in (Role.RECRUITER, Role.HIRING_MANAGER):
        return False

    assigned = application.job.assignments.filter(membership=membership).exists() or has(
        membership, Grant.JOB_WRITE_ANY
    )
    if not assigned:
        return False
    if membership.role == Role.RECRUITER:
        return True
    return kind == WRITE_COMMENT or has(membership, Grant.APPLICATION_DECIDE)


def require_can_write(
    application: Application, membership: CompanyMembership | None, kind: str = WRITE_ORGANISE
) -> Application:
    if not can_write(application, membership, kind):
        raise NotAllowed(
            "You can read this application but not change it. Changes are made by "
            "the people assigned to the job."
        )
    return application


def require_can_decide(membership: CompanyMembership | None) -> None:
    """Closing an application, or rejecting it (FR-6.7)."""
    company_policies.require_grant(membership, Grant.APPLICATION_DECIDE)


def require_can_offer(membership: CompanyMembership | None) -> None:
    company_policies.require_grant(membership, Grant.OFFER_SEND)


def require_owner(application: Application, profile: CandidateProfile | None) -> Application:
    """The candidate's own application, and nobody else's."""
    if profile is None or application.candidate_id != profile.pk:
        raise NotFound("That application does not exist, or you cannot see it.")
    return application


def require_live(application: Application) -> Application:
    if application.withdrawn_at is not None:
        raise InvalidState("You withdrew this application.")
    if application.closed_at is not None:
        raise InvalidState("This application has been closed.")
    return application


def require_contact_revealed(application: Application) -> None:
    """The gate ADR-A34 exists for.

    Called at the point where contact details would be rendered, not at the
    point they are fetched: a template that could reach a phone number is one
    `{{ }}` away from showing it.
    """
    if application.contact_revealed_at is None:
        raise NotAllowed(
            "Contact details unlock when this application reaches the interview stage."
        )


def can_see_contact(application: Application) -> bool:
    return application.contact_revealed_at is not None
