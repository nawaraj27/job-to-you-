"""
Who may read an attachment (DEVELOPER.md §6.2).

The token proves a link was issued; the policy decides whether this actor may
read this file *now*. Both are required: a token that outlived a permission
change must not still work.

Scope note for this phase: candidate profiles (P3) and company memberships (P1)
do not exist yet, so the only private files are those an actor uploaded, and
company-visibility files have no possible reader. Those are the correct answers
for the current data model, not stubs — each rule gains a clause in the phase
that introduces its owner, and the rule that grants company access is part of
P1's permission work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.common.errors import NotAllowed, NotFound
from apps.common.types import Actor

from .constants import Visibility
from .models import Attachment

if TYPE_CHECKING:  # pragma: no cover
    from apps.companies.models import CompanyMembership


def can_access(actor: Actor, attachment: Attachment) -> bool:
    if not attachment.is_servable:
        return False

    if attachment.visibility == Visibility.PUBLIC:
        return True

    if actor is None or not actor.is_authenticated:
        return False

    # No `is_staff` shortcut. Django's flag governs the admin site only
    # (`apps/accounts/staff.py`); it used to open every private CV, message
    # attachment and verification document to anyone holding a token. Staff
    # read files through purpose-built, StaffRole-gated and audited screens —
    # the verification review serves through `views.serve` — never through
    # this general rule.

    if attachment.visibility == Visibility.PRIVATE:
        if attachment.uploaded_by_id == actor.id:
            return True
        # Somebody the owning domain shared it with — a message attachment,
        # for instance. A row, not a branch per domain (ADR-A52).
        return _has_live_grant(attachment, actor)

    if attachment.visibility == Visibility.COMPANY:
        # Readable by any active member of the owning company. A file with no
        # owning company has no company that could read it.
        if attachment.owner_company_id is None:
            return False
        from apps.companies.models import CompanyMembership

        member = (
            CompanyMembership.objects.active()
            .filter(company_id=attachment.owner_company_id, user=actor)
            .select_related("company")
            .first()
        )
        is_member = member is not None and _member_reaches_offer(attachment, member)
        # Or somebody the company shared it with by a grant row — the offer
        # letter a candidate receives when the offer is sent (ADR-A52).
        return is_member or _has_live_grant(attachment, actor)

    return False


def _member_reaches_offer(attachment: Attachment, member: CompanyMembership) -> bool:
    """A company file that is an offer letter belongs to one applicant.

    Decision D-13: the letter is read by the members whose application read
    reaches that applicant, not by every member of the company. Any other
    company file (verification documents are served by their own screen) keeps
    the company-wide rule.
    """
    from apps.applications import policies as application_policies
    from apps.applications.models import Offer

    offers = Offer.objects.filter(attachment=attachment).select_related("application__job")
    if not offers.exists():
        return True
    return any(application_policies.can_read(offer.application, member) for offer in offers)


def _has_live_grant(attachment: Attachment, actor: Actor) -> bool:
    """Is there a row saying this person may read this file (ADR-A52)?

    `actor` is already known to be an authenticated user here — the caller
    checked — but the parameter type is the broad one every policy takes, so
    the narrowing is done again rather than assumed.
    """
    from django.db.models import Q
    from django.utils import timezone

    from .models import AttachmentGrant

    actor_id = getattr(actor, "pk", None)
    if actor_id is None:
        return False

    return (
        AttachmentGrant.objects.filter(
            attachment=attachment, user_id=actor_id, revoked_at__isnull=True
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .exists()
    )


def require_can_access(actor: Actor, attachment: Attachment) -> None:
    """Raise if this actor may not read this file.

    A file that exists but is not readable raises NotFound rather than
    NotAllowed: a 403 confirms the file exists, which is itself a disclosure
    (SRS §15.2).
    """
    if not can_access(actor, attachment):
        raise NotFound("That file does not exist, or you cannot open it.")


def require_uploadable(actor: Actor) -> None:
    if actor is None or not actor.is_authenticated:
        raise NotAllowed("Sign in to upload a file.")
