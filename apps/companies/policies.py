"""
Object-level authorization for companies (SRS §2.5, layer 3).

Layer 1 is the route guard, layer 2 is queryset scoping — the actual security
boundary — and this is layer 3: decisions and mutations about a specific object
by a specific actor.

Every rule here has a **passing negative test**. A permission whose failing case
is untested does not count as implemented (DEVELOPER.md §8.1).
"""

from __future__ import annotations

from apps.common.errors import NotAllowed, NotFound
from apps.common.types import Actor

from . import permissions
from .models import Company, CompanyInvite, CompanyMembership
from .permissions import Grant, Role


def require_membership(membership: CompanyMembership | None) -> CompanyMembership:
    """The actor must be an active member of this company.

    Raises NotFound, never NotAllowed: a 403 confirms the company exists to
    someone with no right to know that (SRS §2.2).
    """
    if membership is None or not membership.is_active:
        raise NotFound("That company does not exist, or you cannot see it.")
    return membership


def require_grant(membership: CompanyMembership | None, grant: str) -> None:
    require_membership(membership)
    permissions.require(membership, grant)


def can_manage_team(membership: CompanyMembership | None) -> bool:
    return permissions.has(membership, Grant.TEAM_INVITE)


def require_can_invite(membership: CompanyMembership | None) -> None:
    require_grant(membership, Grant.TEAM_INVITE)


def require_can_edit_company(membership: CompanyMembership | None) -> None:
    require_grant(membership, Grant.COMPANY_EDIT)


def require_can_assign_role(actor_membership: CompanyMembership, role: str) -> None:
    """Only an owner may create another owner.

    Without this an admin could promote themselves to owner and then remove the
    owner who invited them — privilege escalation using only legitimate calls.
    """
    require_grant(actor_membership, Grant.TEAM_INVITE)
    if role == Role.OWNER and actor_membership.role != Role.OWNER:
        raise NotAllowed("Only an owner can grant the owner role.")


def require_can_hand_out(actor_membership: CompanyMembership, keys: list[str] | None) -> None:
    """Nobody gives away a grant they do not hold themselves.

    Decision D-11 keeps `billing.manage` with the Owner. Extra keys on an
    invitation or a role change were validated as grants and nothing more, so a
    Company Admin could hand plan management to an invitee — or to themselves.
    """
    missing = sorted(set(keys or []) - set(actor_membership.grants))
    if missing:
        raise NotAllowed(
            "You cannot give somebody a permission you do not hold.",
            details={"missing": missing},
        )


def require_can_remove(*, actor_membership: CompanyMembership, target: CompanyMembership) -> None:
    """Removal rules, in the order they matter."""
    require_grant(actor_membership, Grant.TEAM_INVITE)

    if target.company_id != actor_membership.company_id:
        # Not a permission question: the objects belong to different tenants.
        raise NotFound("That member does not exist, or you cannot see them.")

    if target.role == Role.OWNER and actor_membership.role != Role.OWNER:
        raise NotAllowed("Only an owner can remove another owner.")


def invite_matches(*, invite: CompanyInvite, user: Actor) -> bool:
    """Whether this account holds the address the invitation was sent to.

    The main address, or a secondary address the account has already
    verified. Nothing else counts (decision D-03): the invitation is never
    bound to whichever account happens to be signed in.
    """
    if user is None or not user.is_authenticated:
        return False
    addresses = {user.email.lower()}
    addresses.update(
        address.lower()
        for address in user.email_addresses.filter(verified_at__isnull=False).values_list(
            "email", flat=True
        )
    )
    return invite.email.lower() in addresses


def require_can_accept(*, invite: CompanyInvite, user: Actor) -> None:
    """An invite binds to its email address, never to whoever is signed in.

    Decision D-03: there is no "bind to the current account" choice. A person
    signed in under another address either signs in as the invited address or
    asks for the invitation to be sent again to the address they use (SRS
    §2.6). Otherwise a forwarded link would put anyone who opened it on the
    team, and a membership would appear that nobody in the company can account
    for.
    """
    if not invite.is_open:
        raise NotAllowed("This invitation has expired or has already been used.")

    if user is None or not user.is_authenticated:
        raise NotAllowed("Sign in to accept this invitation.")

    if not invite_matches(invite=invite, user=user):
        raise NotAllowed(
            "This invitation was sent to a different email address. "
            "Sign in as that address, or ask for the invitation to be sent again "
            "to the address you use.",
            code="invite_email_mismatch",
        )


def require_company_active(company: Company) -> None:
    if not company.is_active:
        raise NotAllowed("This company is suspended.")


# --- portal sections (SRS §2.3) ---------------------------------------------

#: Which roles may open each employer-portal section at all. Taken from the
#: role catalogue in SRS §2.3: an Interviewer's Jobs, Team and Company columns
#: are "None", and Billing is "None" for everybody below Company Admin.
#:
#: This is the read gate for a whole screen. What a member may *do* inside one
#: is still each action's own grant and object policy — a Recruiter opens the
#: team screen and cannot invite from it.
SECTION_ROLES: dict[str, frozenset[str]] = {
    "jobs": frozenset({Role.OWNER, Role.ADMIN, Role.RECRUITER, Role.HIRING_MANAGER, Role.VIEWER}),
    "team": frozenset({Role.OWNER, Role.ADMIN, Role.RECRUITER, Role.HIRING_MANAGER, Role.VIEWER}),
    "company": frozenset(
        {Role.OWNER, Role.ADMIN, Role.RECRUITER, Role.HIRING_MANAGER, Role.VIEWER}
    ),
    # Decision D-11: Owner and Company Admin see the plan; only `billing.manage`
    # (the Owner) changes it — `apps.billing.policies`.
    "billing": frozenset({Role.OWNER, Role.ADMIN}),
    # Aggregates only — counts, rates, the team's workload — never an applicant.
    # The roles that held `application.read_any` before decision D-13 took it off
    # the Recruiter preset; the grant itself, given explicitly, still opens it.
    "analytics": frozenset({Role.OWNER, Role.ADMIN, Role.RECRUITER, Role.VIEWER}),
}

#: Sections whose gate is a grant rather than a role. Messaging a candidate sits
#: with searching for and pooling them in the Recruiter row of §2.3 ("Search,
#: save to pools, message"), so it is the same grant the pools screen uses.
SECTION_GRANTS: dict[str, str] = {
    "messages": Grant.CANDIDATE_SEARCH,
    "talent": Grant.CANDIDATE_SEARCH,
    "pools": Grant.CANDIDATE_SEARCH,
}


def can_open_section(membership: CompanyMembership | None, section: str) -> bool:
    if membership is None or not membership.is_active:
        return False
    if section in SECTION_GRANTS:
        return permissions.has(membership, SECTION_GRANTS[section])
    if section not in SECTION_ROLES:
        raise ValueError(f"{section!r} is not an employer portal section.")
    if section == "analytics" and Grant.APPLICATION_READ_ANY in (membership.permissions or []):
        return True
    return membership.role in SECTION_ROLES[section]


def require_section(membership: CompanyMembership | None, section: str) -> CompanyMembership:
    """Open one section of the portal, or refuse.

    NotAllowed rather than NotFound: the person is a member of this company, so
    the company's existence is not the secret. What they are told is that their
    role does not include this part of it.
    """
    member = require_membership(membership)
    if not can_open_section(member, section):
        raise NotAllowed("Your role in this company does not include this page.")
    return member
