"""
Granting and revoking platform staff roles (SRS §2.1, §2.3).

  * Only a superadmin proposes a grant, and only a *different* superadmin
    approves it ("granting a staff role requires approval by a second
    superadmin"). Nobody approves their own proposal, and nobody is proposed
    for a role by themselves.
  * Revoking is one superadmin's act, and immediate: removing authority must
    never wait for a second person to be awake.
  * The first superadmin on a new database has nobody to approve them. That
    one grant is `manage.py bootstrap_superadmin`, which refuses once any
    superadmin exists.

Every step is audited. MFA for staff is enforced by `accounts.mfa_policy`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotAllowed

from ..models import StaffRole, StaffRoleRequest

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User


def _require_superadmin(actor: User) -> None:
    from apps.accounts import staff

    if not staff.at_least(actor, "superadmin"):
        raise NotAllowed("Only a superadmin can change staff roles.")


@transaction.atomic
def propose(*, actor: User, user: User, level: str, reason: str) -> StaffRoleRequest:
    _require_superadmin(actor)
    reason = (reason or "").strip()
    if level not in StaffRole.Level.values:
        raise InvalidState("That is not a staff level.")
    if not reason:
        raise InvalidState("Record why this person needs the role.")
    if user.pk == actor.pk:
        raise NotAllowed("Somebody else has to propose a role for you.")
    if not user.is_active:
        raise InvalidState("That account is suspended.")
    current = getattr(user, "staff_role", None)
    if current is not None and current.level == level:
        raise InvalidState("They already hold that level.")

    try:
        with transaction.atomic():
            request = StaffRoleRequest.objects.create(
                user=user, level=level, reason=reason[:300], requested_by=actor
            )
    except IntegrityError:
        raise Conflict("There is already a proposal waiting for that person.") from None

    audit.record(
        verb="staff.role_proposed",
        actor=actor,
        target=user,
        metadata={"level": level, "reason": reason[:300]},
    )
    return request


@transaction.atomic
def decide(*, actor: User, request: StaffRoleRequest, approve: bool) -> StaffRoleRequest:
    _require_superadmin(actor)
    request = StaffRoleRequest.objects.select_for_update().get(pk=request.pk)
    if request.state != StaffRoleRequest.State.PENDING:
        raise InvalidState("That proposal has already been decided.")
    if request.requested_by_id == actor.pk:
        raise NotAllowed("A second superadmin has to approve a proposal you made.")
    if request.user_id == actor.pk:
        raise NotAllowed("You cannot decide a proposal about yourself.")

    now = timezone.now()
    request.state = StaffRoleRequest.State.APPROVED if approve else StaffRoleRequest.State.DECLINED
    request.decided_by = actor
    request.decided_at = now
    request.save(update_fields=["state", "decided_by", "decided_at", "updated_at"])

    if approve:
        StaffRole.objects.update_or_create(
            user=request.user,
            defaults={
                "level": request.level,
                "granted_by": request.requested_by,
                "granted_at": now,
            },
        )
    audit.record(
        verb="staff.role_granted" if approve else "staff.role_declined",
        actor=actor,
        target=request.user,
        metadata={"level": request.level, "proposed_by": request.requested_by_id},
    )
    return request


@transaction.atomic
def withdraw(*, actor: User, request: StaffRoleRequest) -> None:
    _require_superadmin(actor)
    if request.state != StaffRoleRequest.State.PENDING:
        raise InvalidState("That proposal has already been decided.")
    request.state = StaffRoleRequest.State.WITHDRAWN
    request.decided_by = actor
    request.decided_at = timezone.now()
    request.save(update_fields=["state", "decided_by", "decided_at", "updated_at"])
    audit.record(verb="staff.role_proposal_withdrawn", actor=actor, target=request.user)


@transaction.atomic
def revoke(*, actor: User, user: User, reason: str) -> None:
    _require_superadmin(actor)
    reason = (reason or "").strip()
    if not reason:
        raise InvalidState("Record why the role is being removed.")
    if user.pk == actor.pk:
        raise NotAllowed("Another superadmin has to remove your role.")
    role = StaffRole.objects.filter(user=user).first()
    if role is None:
        raise InvalidState("They hold no staff role.")
    level = role.level
    role.delete()

    from . import identity

    # Authority ends now, not when their session expires.
    identity.revoke_all_sessions(user=user, except_key=None)
    audit.record(
        verb="staff.role_revoked",
        actor=actor,
        target=user,
        metadata={"level": level, "reason": reason[:300]},
    )


@transaction.atomic
def bootstrap_superadmin(*, user: User) -> StaffRole:
    """The one grant with nobody to approve it: the first superadmin."""
    if StaffRole.objects.filter(level=StaffRole.Level.SUPERADMIN).exists():
        raise Conflict("A superadmin already exists; grants now need a second superadmin.")
    role, _ = StaffRole.objects.update_or_create(
        user=user, defaults={"level": StaffRole.Level.SUPERADMIN, "granted_at": timezone.now()}
    )
    audit.record(verb="staff.superadmin_bootstrapped", actor=None, target=user)
    return role
