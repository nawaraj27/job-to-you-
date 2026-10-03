"""
Suspending and reinstating an account (SRS §2.3, Platform Moderator).

`User.is_active` was reachable only through the Django admin change form, which
wrote it with no reason, no audit entry and no effect on sessions already
signed in — a suspended person stayed signed in until their session expired.

Two limits the admin form never had:

  * nobody suspends themselves, which is how a moderator locks the only
    moderator out;
  * platform staff are suspended only by a superadmin. A moderator able to
    suspend an admin is a privilege escalation made of legitimate clicks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.audit import services as audit
from apps.common.errors import InvalidState, NotAllowed

from . import identity

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User


def _require_can_act_on(*, actor: User, user: User) -> None:
    from apps.accounts import staff

    if actor.pk == user.pk:
        raise NotAllowed("You cannot suspend or reinstate your own account.")
    target_is_staff = staff.role_of(user) is not None or user.is_staff or user.is_superuser
    if target_is_staff and not staff.at_least(actor, "superadmin"):
        raise NotAllowed("Only a superadmin can suspend or reinstate platform staff.")


@transaction.atomic
def suspend_account(*, actor: User, user: User, reason: str) -> int:
    """Sign somebody out everywhere and stop them signing in. Returns sessions ended."""
    reason = (reason or "").strip()
    if not reason:
        raise InvalidState("Record why this account is being suspended.")
    _require_can_act_on(actor=actor, user=user)
    if not user.is_active:
        raise InvalidState("That account is already suspended.")

    user.is_active = False
    user.save(update_fields=["is_active"])
    ended = identity.revoke_all_sessions(user=user, except_key=None)
    audit.record(
        verb="account.suspended",
        actor=actor,
        target=user,
        metadata={"reason": reason, "sessions_ended": ended},
    )
    return ended


@transaction.atomic
def reinstate_account(*, actor: User, user: User, reason: str) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise InvalidState("Record why this account is being reinstated.")
    _require_can_act_on(actor=actor, user=user)
    if user.is_active:
        raise InvalidState("That account is not suspended.")

    user.is_active = True
    user.save(update_fields=["is_active"])
    audit.record(verb="account.reinstated", actor=actor, target=user, metadata={"reason": reason})
