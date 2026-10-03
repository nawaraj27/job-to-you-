"""
Who may see and change a plan (SRS §2.4, ADR-A1).

Two rules, and the second is the one that gets forgotten.

**A candidate's billing is their own.** No employer, no teammate, nobody else.
There is no shared-billing case on the candidate side, so there is no lookup
here that could accidentally admit one.

**Inside a company, seeing the plan and changing it are different rights.**
Decision D-11 (SRS §2.3): the Owner and Company Admin see the plan, its status,
usage and invoices (`companies.policies.SECTION_ROLES["billing"]`); only
`billing.manage` may change, cancel or resume it, and only the Owner preset
holds that. A company admin can do nearly everything else and deliberately
cannot move money — nor hand the grant to anybody (`require_can_hand_out`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.common.errors import NotAllowed, NotFound
from apps.companies.permissions import Grant, has, require

from .models import Subscription

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import CompanyMembership


def can_manage_company_billing(membership: CompanyMembership | None) -> bool:
    return has(membership, Grant.BILLING_MANAGE)


def require_company_billing(membership: CompanyMembership | None) -> None:
    """The write side of a company's plan."""
    if membership is None:
        raise NotAllowed("You are not a member of this company.")
    require(membership, Grant.BILLING_MANAGE)


def require_company_view(membership: CompanyMembership | None) -> None:
    """The read side. Which roles reach the screen is the section gate (D-11)."""
    if membership is None or not membership.is_active:
        raise NotAllowed("You are not a member of this company.")


def require_own_subscription(
    subscription: Subscription, profile: CandidateProfile | None
) -> Subscription:
    """A candidate's own subscription, or `NotFound`.

    `NotFound` rather than `NotAllowed` for the reason it is everywhere else in
    this codebase: a 403 confirms that the subscription exists, which is a fact
    about a stranger's account.
    """
    if profile is None or subscription.candidate_id != profile.pk:
        raise NotFound("That subscription is not yours.")
    return subscription


def invoice_payable_by(user, invoice) -> bool:  # type: ignore[no-untyped-def]
    """May this person pay this invoice online?

    A candidate pays their own. Inside a company, paying is moving money, so it
    takes `billing.manage` — the same right that changes the plan (D-11).
    """
    subscription = invoice.subscription
    if subscription.candidate_id is not None:
        profile = getattr(user, "candidate_profile", None)
        return profile is not None and subscription.candidate_id == profile.pk
    from apps.companies.selectors import membership_for

    membership = membership_for(user=user, company=subscription.company)
    return membership is not None and can_manage_company_billing(membership)


def require_invoice_payable(user, invoice) -> None:  # type: ignore[no-untyped-def]
    """`NotFound`, never `NotAllowed`: a 403 would confirm a stranger's invoice exists."""
    if not getattr(user, "is_authenticated", False) or not invoice_payable_by(user, invoice):
        raise NotFound("That invoice is not one you can pay.")


def require_attempt_visible(user, attempt) -> None:  # type: ignore[no-untyped-def]
    """The payment screen is for whoever may pay the invoice it belongs to."""
    if not getattr(user, "is_authenticated", False) or not invoice_payable_by(
        user, attempt.invoice
    ):
        raise NotFound("That payment is not one you can see.")
