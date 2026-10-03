"""
The role x capability matrix (SRS §2.4, ADR-A1).

This module is the single authority for what a company member may do. The table
in the SRS is generated from it, so documentation cannot drift from
enforcement — a capability that exists here and nowhere else still cannot be
used, because `require` refuses an unknown key.

Grants are a fixed enum, never free-form strings. `CompanyMembership.permissions`
holds extra keys layered on top of the role preset, validated on write, so an
unusual org chart never needs a code change and never accumulates undocumented
values either.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.errors import NotAllowed


class Role(models.TextChoices):
    OWNER = "owner", _("Owner")
    ADMIN = "admin", _("Company admin")
    RECRUITER = "recruiter", _("Recruiter")
    HIRING_MANAGER = "hiring_manager", _("Hiring manager")
    INTERVIEWER = "interviewer", _("Interviewer")
    VIEWER = "viewer", _("Viewer")


class Grant(models.TextChoices):
    """Every capability the platform can grant inside a company.

    Adding one here is the only way to add one at all: `require()` rejects a key
    that is not a member of this enum, so a typo fails loudly instead of
    silently granting nothing (or, worse, being treated as satisfied).
    """

    JOB_PUBLISH = "job.publish", _("Publish jobs")
    JOB_WRITE_ANY = "job.write_any", _("Edit any job, not only assigned ones")
    APPLICATION_READ_ANY = "application.read_any", _("Read applications for any job")
    APPLICATION_DECIDE = "application.decide", _("Move to terminal stages and reject")
    OFFER_SEND = "offer.send", _("Send offers")
    CANDIDATE_SEARCH = "candidate.search", _("Search consenting candidates")
    CANDIDATE_EXPORT = "candidate.export", _("Bulk export candidate data")
    TEAM_INVITE = "team.invite", _("Invite and remove team members")
    COMPANY_EDIT = "company.edit", _("Edit company profile and verification")
    BILLING_MANAGE = "billing.manage", _("Manage plan and billing")


#: Role presets. A membership's effective grants are its role's preset plus any
#: extra keys stored on the membership itself.
#:
#: Decision D-13 (SRS §2.3): recruiters read and change applicants only on the
#: jobs they are assigned to. The preset therefore carries no
#: `application.read_any`; company-wide applicant access belongs to Owner and
#: Company Admin, or to a member given the grant explicitly. Hiring managers get
#: neither publish nor decide by default; a company that wants that grants it
#: explicitly.
#:
#: Decision D-11: `billing.manage` stays with the Owner. A Company Admin sees the
#: plan and cannot change it.
ROLE_GRANTS: dict[str, frozenset[str]] = {
    Role.OWNER: frozenset(Grant.values),
    Role.ADMIN: frozenset(Grant.values) - {Grant.BILLING_MANAGE},
    Role.RECRUITER: frozenset(
        {
            Grant.JOB_PUBLISH,
            Grant.APPLICATION_DECIDE,
            Grant.CANDIDATE_SEARCH,
        }
    ),
    # No `application.read_any`: a Hiring Manager sees "only applicants to
    # assigned jobs" (SRS §2.3). The preset used to grant it, which opened every
    # applicant in the company to a manager hiring for one role.
    Role.HIRING_MANAGER: frozenset(),
    Role.INTERVIEWER: frozenset(),
    Role.VIEWER: frozenset({Grant.APPLICATION_READ_ANY}),
}


def validate_grants(keys: list[str] | tuple[str, ...] | set[str]) -> list[str]:
    """Reject anything that is not a declared grant. Called on every write."""
    known = set(Grant.values)
    unknown = sorted(set(keys) - known)
    if unknown:
        raise ValueError(f"Unknown permission keys: {unknown}")
    return sorted(set(keys))


def grants_for(role: str, extra: list[str] | None = None) -> frozenset[str]:
    """Effective grants for a role plus any per-membership additions."""
    preset = ROLE_GRANTS.get(role, frozenset())
    if not extra:
        return preset
    return preset | frozenset(validate_grants(extra))


def has(membership, grant: str) -> bool:
    """True when this membership carries the grant.

    A membership that is not active carries nothing. Suspension and removal are
    therefore enforced here rather than remembered at each call site.
    """
    if membership is None or not membership.is_active:
        return False
    if grant not in set(Grant.values):
        raise ValueError(f"Unknown permission key: {grant!r}")
    return grant in membership.grants


def require(membership, grant: str) -> None:
    """Raise unless this membership carries the grant."""
    if not has(membership, grant):
        raise NotAllowed(
            "You do not have permission to do that.",
            details={"required": grant},
        )


def matrix() -> list[dict[str, object]]:
    """The role x capability table, for documentation and for the test that
    asserts every capability is exercised by at least one negative test."""
    return [
        {
            "role": role,
            "label": Role(role).label,
            "grants": sorted(ROLE_GRANTS[role]),
        }
        for role in Role.values
    ]
