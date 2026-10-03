"""
Team membership: invite, accept, change role, remove, transfer ownership.

Three rules here exist because their absence is a real incident rather than a
theoretical one: an invitation must bind to its email address, a company must
never lose its last owner, and revocation must take effect in the same request.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotAllowed, NotFound
from apps.notifications import events

from .. import policies, selectors
from ..models import Company, CompanyInvite, CompanyMembership, OwnershipTransfer
from ..permissions import Role, validate_grants

INVITE_TTL = timedelta(days=14)


def _hash(token: str) -> str:
    """Invites are stored hashed (SRS §15.1). A leaked database row must not be
    a working invitation."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@transaction.atomic
def invite(
    *,
    actor: User,
    actor_membership: CompanyMembership,
    company: Company,
    email: str,
    role: str,
    permissions: list[str] | None = None,
) -> tuple[CompanyInvite, str]:
    """Invite someone by email. Returns the invite and its single-use token.

    The raw token is returned once, to be emailed, and never stored.
    """
    policies.require_can_invite(actor_membership)
    _require_known_role(role)
    policies.require_can_assign_role(actor_membership, role)
    policies.require_company_active(company)

    email = (email or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        # The form field is `type="email"`, but a POST is not a form. A blank
        # or malformed address made an invitation nobody could ever receive,
        # and it held the open-invite slot for that "address".
        raise InvalidState("Enter the email address to send the invitation to.") from None
    validate_grants(permissions or [])
    policies.require_can_hand_out(actor_membership, permissions)

    if (
        CompanyMembership.objects.active()
        .filter(company=company, user__email__iexact=email)
        .exists()
    ):
        raise Conflict("That person is already on the team.")

    # Re-inviting replaces the outstanding invitation rather than colliding with
    # the partial unique index, which is what an admin actually means by it.
    CompanyInvite.objects.filter(
        company=company, email=email, accepted_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())

    token = secrets.token_urlsafe(32)
    record = CompanyInvite.objects.create(
        company=company,
        email=email,
        role=role,
        permissions=permissions or [],
        token_hash=_hash(token),
        expires_at=timezone.now() + INVITE_TTL,
        invited_by=actor,
    )

    audit.record(
        verb="team.invited",
        actor=actor,
        target=company,
        metadata={"email": email, "role": role},
    )
    events.emit(
        "team.invited",
        company,
        payload={
            "company_id": company.pk,
            "email": email,
            "role": role,
            "invited_by_id": actor.pk,
        },
    )
    return record, token


def find_invite(token: str, *, lock: bool = False) -> CompanyInvite:
    """Look an invite up by its raw token."""
    rows = CompanyInvite.objects.select_related("company").filter(token_hash=_hash(token))
    if lock:
        rows = rows.select_for_update(of=("self",))
    record = rows.first()
    if record is None:
        raise NotFound("That invitation link is not valid.")
    return record


@transaction.atomic
def revoke_invite(
    *, actor: User, actor_membership: CompanyMembership, invite_id: object
) -> CompanyInvite:
    """Withdraw an open invitation (SRS §2.6, "revocation is immediate").

    The link stops working at once, because acceptance checks `revoked_at`.
    Scoped to the actor's company, so an id from another company is a 404.
    """
    policies.require_can_invite(actor_membership)
    try:
        pk = int(str(invite_id))
    except (TypeError, ValueError):
        raise NotFound("That invitation does not exist, or you cannot see it.") from None

    record = (
        CompanyInvite.objects.select_for_update()
        .filter(pk=pk, company_id=actor_membership.company_id)
        .first()
    )
    if record is None:
        raise NotFound("That invitation does not exist, or you cannot see it.")
    if record.role == Role.OWNER and actor_membership.role != Role.OWNER:
        raise NotAllowed("Only an owner can withdraw an invitation to become an owner.")
    if record.accepted_at is not None:
        raise InvalidState("That invitation was already accepted. Remove the member instead.")
    if record.revoked_at is not None:
        raise InvalidState("That invitation was already withdrawn.")

    record.revoked_at = timezone.now()
    record.save(update_fields=["revoked_at", "updated_at"])

    audit.record(
        verb="team.invite_revoked",
        actor=actor,
        target=actor_membership.company,
        company=actor_membership.company,
        metadata={"email": record.email, "role": record.role},
    )
    return record


@transaction.atomic
def accept_invite(*, user: User, token: str) -> CompanyMembership:
    """Accept an invitation, binding it to the invited address only.

    The invitation row is locked, so two tabs accepting (or a revoke landing
    during acceptance) settle one after the other rather than both passing
    the "still open" check.
    """
    invitation = find_invite(token, lock=True)
    policies.require_can_accept(invite=invitation, user=user)
    policies.require_company_active(invitation.company)

    existing = (
        CompanyMembership.objects.filter(company=invitation.company, user=user)
        .select_for_update()
        .first()
    )
    if existing is not None and existing.deleted_at is None:
        raise Conflict("You are already on this team.")

    if existing is not None:
        # Rejoining after removal reuses the row, so the partial unique index
        # holds and the person keeps one identity in this company's history.
        existing.deleted_at = None
        existing.role = invitation.role
        existing.permissions = invitation.permissions
        existing.status = CompanyMembership.Status.ACTIVE
        existing.joined_at = timezone.now()
        existing.invited_by = invitation.invited_by
        existing.save()
        membership = existing
    else:
        membership = CompanyMembership.objects.create(
            company=invitation.company,
            user=user,
            role=invitation.role,
            permissions=invitation.permissions,
            invited_by=invitation.invited_by,
        )

    invitation.accepted_at = timezone.now()
    invitation.accepted_by = user
    invitation.save(update_fields=["accepted_at", "accepted_by", "updated_at"])

    audit.record(verb="company.member_joined", actor=user, target=invitation.company)
    # The inviter, while they are still on this team and are not the joiner.
    inviter_on_team = (
        invitation.invited_by_id
        and invitation.invited_by_id != user.pk
        and CompanyMembership.objects.active()
        .filter(company_id=invitation.company_id, user_id=invitation.invited_by_id)
        .exists()
    )
    events.emit(
        "company.member_joined",
        invitation.company,
        payload={
            "company_id": invitation.company_id,
            "company_name": invitation.company.name,
            "user_id": user.pk,
            "member_name": user.get_full_name(),
            "role": membership.role,
            "role_label": str(Role(membership.role).label),
            "recipient_ids": [invitation.invited_by_id] if inviter_on_team else [],
            "target_url": reverse("employer:team", args=[invitation.company.slug]),
        },
    )
    return membership


@transaction.atomic
def change_role(
    *,
    actor: User,
    actor_membership: CompanyMembership,
    target: CompanyMembership,
    role: str,
    permissions: list[str] | None = None,
) -> CompanyMembership:
    """Change a member's role, protecting the last owner."""
    _require_known_role(role)
    actor_membership, target = _lock_team(actor_membership=actor_membership, target=target)
    policies.require_can_assign_role(actor_membership, role)
    policies.require_can_remove(actor_membership=actor_membership, target=target)

    if target.role == role and permissions is None:
        return target

    if target.role == Role.OWNER and role != Role.OWNER:
        _require_another_owner(company=target.company, excluding=target)

    previous = target.role
    target.role = role
    if permissions is not None:
        target.permissions = validate_grants(permissions)
        policies.require_can_hand_out(actor_membership, target.permissions)
    target.save()

    audit.record(
        verb="company.member_role_changed",
        actor=actor,
        target=target.company,
        metadata={"user_id": target.user_id, "from": previous, "to": role},
    )
    return target


@transaction.atomic
def remove_member(
    *, actor: User, actor_membership: CompanyMembership, target: CompanyMembership
) -> None:
    """Revoke a membership.

    Access disappears in the same request cycle, because every scoped queryset
    derives from an active membership. Historical notes and events stay
    attributed to the person: the audit trail must not develop holes.
    """
    actor_membership, target = _lock_team(actor_membership=actor_membership, target=target)
    policies.require_can_remove(actor_membership=actor_membership, target=target)

    if target.role == Role.OWNER:
        _require_another_owner(company=target.company, excluding=target)

    target.deleted_at = timezone.now()
    target.save(update_fields=["deleted_at", "updated_at"])
    # A waiting nomination to or from somebody who has left can never be
    # answered, and would block every new one.
    from django.db.models import Q

    OwnershipTransfer.objects.filter(state=OwnershipTransfer.State.PENDING).filter(
        Q(nominee=target) | Q(nominated_by=target)
    ).update(state=OwnershipTransfer.State.CANCELLED, decided_at=timezone.now())

    audit.record(
        verb="company.member_removed",
        actor=actor,
        target=target.company,
        metadata={"user_id": target.user_id, "role": target.role},
    )
    events.emit(
        "company.member_removed",
        target.company,
        payload={
            "company_id": target.company_id,
            "company_name": target.company.name,
            "user_id": target.user_id,
            "removed_by_id": actor.pk,
            "removed_by_name": actor.get_full_name(),
            # Only the person removed. No link: they can no longer open anything
            # at this company.
            "recipient_ids": [target.user_id] if target.user_id != actor.pk else [],
        },
    )


def pending_nomination(company: Company) -> OwnershipTransfer | None:
    return (
        OwnershipTransfer.objects.filter(company=company, state=OwnershipTransfer.State.PENDING)
        .select_related("nominee__user", "nominated_by__user")
        .first()
    )


@transaction.atomic
def nominate_owner(
    *, actor: User, actor_membership: CompanyMembership, target: CompanyMembership
) -> OwnershipTransfer:
    """Step one of SRS §2.6: the owner nominates; nothing changes until they accept."""
    if target.company_id != actor_membership.company_id:
        raise NotFound("That member does not exist, or you cannot see them.")
    actor_membership, target = _lock_team(actor_membership=actor_membership, target=target)
    if actor_membership.role != Role.OWNER:
        raise InvalidState("Only an owner can nominate a new owner.")
    if target.pk == actor_membership.pk or target.role == Role.OWNER:
        raise InvalidState("That person already owns this company.")
    if pending_nomination(actor_membership.company) is not None:
        raise Conflict("There is already a nomination waiting. Cancel it first.")

    transfer = OwnershipTransfer.objects.create(
        company=actor_membership.company, nominated_by=actor_membership, nominee=target
    )
    audit.record(
        verb="company.ownership_nominated",
        actor=actor,
        target=actor_membership.company,
        metadata={"from_user_id": actor_membership.user_id, "to_user_id": target.user_id},
    )
    _emit_nomination(transfer, "company.ownership_nominated", recipients=[target.user_id])
    return transfer


@transaction.atomic
def respond_to_nomination(
    *, actor: User, actor_membership: CompanyMembership, accept: bool
) -> OwnershipTransfer:
    """Step two: only the nominee answers. Accepting completes the transfer."""
    transfer = (
        OwnershipTransfer.objects.select_for_update()
        .filter(company_id=actor_membership.company_id, state=OwnershipTransfer.State.PENDING)
        .first()
    )
    if transfer is None or transfer.nominee_id != actor_membership.pk:
        raise NotFound("There is no nomination waiting for you.")

    if accept:
        nominator = transfer.nominated_by
        if not nominator.is_active or nominator.role != Role.OWNER:
            transfer.state = OwnershipTransfer.State.CANCELLED
            transfer.decided_at = timezone.now()
            transfer.save(update_fields=["state", "decided_at", "updated_at"])
            raise InvalidState(
                "The person who nominated you is no longer an owner, so the nomination has lapsed."
            )
        transfer_ownership(
            actor=nominator.user, actor_membership=nominator, target=actor_membership
        )
        transfer.state = OwnershipTransfer.State.ACCEPTED
    else:
        transfer.state = OwnershipTransfer.State.DECLINED
    transfer.decided_at = timezone.now()
    transfer.save(update_fields=["state", "decided_at", "updated_at"])

    if not accept:
        audit.record(
            verb="company.ownership_declined", actor=actor, target=actor_membership.company
        )
        _emit_nomination(
            transfer,
            "company.ownership_nomination_closed",
            recipients=[transfer.nominated_by.user_id],
            outcome=f"{actor.get_full_name()} declined to become the owner",
        )
    return transfer


@transaction.atomic
def cancel_nomination(*, actor: User, actor_membership: CompanyMembership) -> OwnershipTransfer:
    """The nominating owner (or any owner) withdraws a waiting nomination."""
    if actor_membership.role != Role.OWNER:
        raise InvalidState("Only an owner can cancel a nomination.")
    transfer = (
        OwnershipTransfer.objects.select_for_update()
        .filter(company_id=actor_membership.company_id, state=OwnershipTransfer.State.PENDING)
        .first()
    )
    if transfer is None:
        raise NotFound("There is no nomination waiting.")
    transfer.state = OwnershipTransfer.State.CANCELLED
    transfer.decided_at = timezone.now()
    transfer.save(update_fields=["state", "decided_at", "updated_at"])
    audit.record(verb="company.ownership_cancelled", actor=actor, target=actor_membership.company)
    _emit_nomination(
        transfer,
        "company.ownership_nomination_closed",
        recipients=[transfer.nominee.user_id],
        outcome="The ownership nomination was withdrawn",
    )
    return transfer


def _emit_nomination(
    transfer: OwnershipTransfer, event_type: str, *, recipients: list[int], outcome: str = ""
) -> None:
    company = transfer.company
    events.emit(
        event_type,
        company,
        payload={
            "company_id": company.pk,
            "company_name": company.name,
            "from_name": transfer.nominated_by.user.get_full_name(),
            "to_name": transfer.nominee.user.get_full_name(),
            "outcome": outcome,
            "recipient_ids": recipients,
            "target_url": reverse("employer:team", args=[company.slug]),
        },
    )


@transaction.atomic
def transfer_ownership(
    *, actor: User, actor_membership: CompanyMembership, target: CompanyMembership
) -> CompanyMembership:
    """Hand ownership to another active member.

    The outgoing owner becomes an admin rather than losing access outright: a
    transfer that locks the previous owner out is indistinguishable from a
    hostile takeover, and is unrecoverable if it was a mistake.
    """
    if target.company_id != actor_membership.company_id:
        raise NotFound("That member does not exist, or you cannot see them.")
    actor_membership, target = _lock_team(actor_membership=actor_membership, target=target)
    if actor_membership.role != Role.OWNER:
        raise InvalidState("Only an owner can transfer ownership.")
    if target.company_id != actor_membership.company_id:
        raise NotFound("That member does not exist, or you cannot see them.")
    if not target.is_active:
        raise InvalidState("That member is not active.")
    if target.pk == actor_membership.pk:
        raise InvalidState("You already own this company.")

    target.role = Role.OWNER
    target.save(update_fields=["role", "updated_at"])

    actor_membership.role = Role.ADMIN
    actor_membership.save(update_fields=["role", "updated_at"])

    audit.record(
        verb="company.ownership_transferred",
        actor=actor,
        target=target.company,
        metadata={"from_user_id": actor_membership.user_id, "to_user_id": target.user_id},
    )
    events.emit(
        "company.ownership_transferred",
        target.company,
        payload={
            "company_id": target.company_id,
            "company_name": target.company.name,
            "from_user_id": actor_membership.user_id,
            "from_name": actor_membership.user.get_full_name(),
            "to_user_id": target.user_id,
            "to_name": target.user.get_full_name(),
            # SRS §2.6: "both are notified".
            "recipient_ids": [actor_membership.user_id, target.user_id],
            "target_url": reverse("employer:team", args=[target.company.slug]),
        },
    )
    return target


def _require_known_role(role: str) -> None:
    """A role outside the enum saved happily and carried no grants at all."""
    if role not in Role.values:
        raise InvalidState("Choose a role for this person.")


def _lock_team(
    *, actor_membership: CompanyMembership, target: CompanyMembership
) -> tuple[CompanyMembership, CompanyMembership]:
    """Serialise membership changes inside one company, then re-read both sides.

    The last-owner rule is a count followed by a write. Two owners removing
    each other at the same moment both counted one other owner, both wrote,
    and the company was left with none. Locking the company row makes the
    second change wait for the first and count again; re-reading the two
    memberships means it decides on what is true now, not on what the view
    loaded before the wait.
    """
    Company.objects.select_for_update().only("pk").get(pk=actor_membership.company_id)

    rows = CompanyMembership.objects.active().select_related("company", "user")
    fresh_actor = rows.filter(pk=actor_membership.pk).first()
    if fresh_actor is None:
        raise NotFound("That company does not exist, or you cannot see it.")
    fresh_target = rows.filter(pk=target.pk).first()
    if fresh_target is None:
        raise NotFound("That member does not exist, or you cannot see them.")
    return fresh_actor, fresh_target


def _require_another_owner(*, company: Company, excluding: CompanyMembership) -> None:
    """A company always has at least one active owner.

    Without this, an owner can remove or demote themselves and leave a company
    nobody can administer — no invitations, no verification, no billing — and
    the only repair is a staff intervention in the database.
    """
    remaining = (
        CompanyMembership.objects.owners().filter(company=company).exclude(pk=excluding.pk).count()
    )
    if remaining == 0:
        raise InvalidState(
            "A company must always have an owner. Transfer ownership first, "
            "or promote another member to owner."
        )


__all__ = [
    "accept_invite",
    "change_role",
    "find_invite",
    "invite",
    "remove_member",
    "revoke_invite",
    "selectors",
    "transfer_ownership",
]
