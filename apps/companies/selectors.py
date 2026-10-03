"""
Company reads — all scoped (DEVELOPER.md §1.2).

Every function here takes an explicit `user` and, where relevant, an explicit
`company`. None of them takes a request. That is not style: a scheduled job has
an actor and no request, and a scoping primitive that requires HTTP is one that
background work will inevitably route around — which is how tenant data leaks.
"""

from __future__ import annotations

from django.db.models import QuerySet

from .models import (
    Company,
    CompanyInvite,
    CompanyMembership,
    CompanyVerification,
    CompanyVerificationDecision,
    CompanyVerificationDocument,
)


def companies_for(user) -> QuerySet[Company]:
    """Companies this user may act in. Backs the company switcher."""
    return Company.objects.for_actor(user).order_by("name")


def membership_for(*, user, company: Company) -> CompanyMembership | None:
    """The active membership binding this user to this company, or None.

    Returned rather than raised so callers can distinguish "not a member" from
    "a member who lacks a grant" — two different answers that deserve two
    different messages.
    """
    if user is None or not user.is_authenticated:
        return None
    return (
        CompanyMembership.objects.active()
        .select_related("company", "user")
        .filter(user=user, company=company)
        .first()
    )


def membership_by_slug(*, user, slug: str) -> CompanyMembership | None:
    """Resolve URL tenancy in one query (ADR-A18).

    Company state is part of the filter, not a later check. Suspension has to
    lock every surface at once; leaving it to each view means it holds until
    somebody writes a view that forgets.
    """
    from .models import CompanyState

    if user is None or not user.is_authenticated:
        return None
    return (
        CompanyMembership.objects.active()
        .select_related("company", "user")
        .filter(
            user=user,
            company__slug=slug,
            company__deleted_at__isnull=True,
            company__state=CompanyState.ACTIVE,
        )
        .first()
    )


def team_for(*, company: Company) -> QuerySet[CompanyMembership]:
    return (
        CompanyMembership.objects.active()
        .filter(company=company)
        .select_related("user")
        .order_by("role", "user__full_name")
    )


def open_invites_for(*, company: Company) -> QuerySet[CompanyInvite]:
    from django.utils import timezone

    return (
        CompanyInvite.objects.filter(
            company=company,
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .select_related("invited_by")
        .order_by("-created_at")
    )


def active_owner_count(*, company: Company) -> int:
    """Used by the last-owner guard. A company always has at least one owner."""
    return CompanyMembership.objects.owners().filter(company=company).count()


def verification_for(*, company: Company) -> CompanyVerification:
    """One company's verification with its documents and their files.

    The screen asks each document whether a reviewer could open it, so the
    attachments come with them: without this the checklist walked the
    documents one query at a time. The queue selector prefetches the same
    two relations, which is why the completeness helpers read
    `verification.documents.all()` rather than issuing their own query.
    """
    return (
        CompanyVerification.objects.prefetch_related("documents__attachment")
        .select_related("company")
        .get(company=company)
    )


def verification_queue() -> QuerySet[CompanyVerification]:
    """Submissions waiting for a reviewer, oldest first (SRS §19.5).

    Platform-wide rather than company-scoped, and that is the one place in this
    module where that is correct: the reader is a moderator working a queue, not
    a member of any of these companies.
    """
    from django.db.models import Prefetch

    from .models import CompanyDomain, VerificationState

    return (
        CompanyVerification.objects.filter(state=VerificationState.PENDING)
        .select_related("company", "claimed_by")
        .prefetch_related(
            "documents__attachment",
            # Earlier decisions on the same company, so a reviewer sees that
            # this is attempt 2 and why attempt 1 was rejected (D-02).
            "decisions__reviewed_by",
            # Domain proof (SRS §19.2) beside the documents, without a query per row.
            Prefetch(
                "company__domains",
                queryset=CompanyDomain.objects.filter(verified_at__isnull=False),
                to_attr="proven_domains",
            ),
        )
        .order_by("submitted_at")
    )


def verification_documents(*, company: Company):
    """The documents attached to one company's verification, oldest first."""
    return (
        CompanyVerificationDocument.objects.filter(verification__company=company)
        .select_related("attachment")
        .order_by("created_at", "pk")
    )


def verification_document(document_id) -> CompanyVerificationDocument | None:
    """One document, for the staff review screen. Platform-wide, like the queue."""
    return (
        CompanyVerificationDocument.objects.select_related("verification__company", "attachment")
        .filter(pk=document_id, attachment__deleted_at__isnull=True)
        .first()
    )


def recently_reviewed(*, limit: int = 10) -> list[CompanyVerificationDecision]:
    """The last decisions, so a reviewer can see what the queue has been doing.

    Read from the decision history rather than the current state, so a
    rejection is still listed after the company applies again (D-02).
    """
    return list(
        CompanyVerificationDecision.objects.select_related(
            "verification__company", "reviewed_by"
        ).order_by("-decided_at", "-id")[:limit]
    )


def verification_history(*, company: Company) -> list[CompanyVerificationDecision]:
    """Every decision on this company's verification, newest first (D-02)."""
    return list(
        CompanyVerificationDecision.objects.filter(verification__company=company)
        .select_related("reviewed_by")
        .order_by("-decided_at", "-id")
    )


def by_slug(slug: str) -> Company | None:
    """One company by slug, or `None`. For staff screens that take a form post."""
    return Company.objects.filter(slug=slug).first()


def public_company(slug: str) -> Company | None:
    """A company's public page, or `None` (SRS §3.7).

    Active and not deleted: a suspended or removed company has no public page,
    and a 404 is the only honest answer for one.
    """
    from .models import CompanyState

    return Company.objects.filter(
        slug=slug, state=CompanyState.ACTIVE, deleted_at__isnull=True
    ).first()


def member_of(*, company: Company, membership_id) -> CompanyMembership | None:
    """One active membership, scoped to a company.

    Scoped rather than looked up by primary key alone, and the scoping is the
    point: an unscoped lookup would let a member of one company act on a
    colleague of another by guessing an id (ADR-A18).
    """
    return (
        CompanyMembership.objects.active()
        .select_related("user", "company")
        .filter(pk=membership_id, company=company)
        .first()
    )
