"""
Platform authority (SRS §2.3, ADR-A5).

`StaffRole` has existed since P1 and nothing read it until P19, because there
were no staff surfaces to guard. This is the module that reads it.

**Levels are ordered, and the order is the whole model.** Support can read,
moderator can act on companies and abuse, admin can change platform
configuration, superadmin can grant roles. Each includes the ones below it, so
a check is `at_least(user, MODERATOR)` rather than a set membership that
someone will forget to extend when a level is added.

**`is_staff` is not authority.** Django's flag governs the admin site and
nothing else here; a person with `is_staff` and no `StaffRole` reaches no staff
screen. Two systems answering "is this an operator" would eventually disagree,
and the one that mattered would be whichever the view happened to ask.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.common.errors import NotAllowed, NotFound

if TYPE_CHECKING:  # pragma: no cover
    from .models import StaffRole, User

#: Weakest to strongest. A check is "at least this", never "one of these".
ORDER: tuple[str, ...] = ("support", "moderator", "admin", "superadmin")


def role_of(user: User | None) -> StaffRole | None:
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "staff_role", None)


def level_of(user: User | None) -> str:
    role = role_of(user)
    return role.level if role is not None else ""


def at_least(user: User | None, level: str) -> bool:
    """Whether this person holds `level` or something above it."""
    if level not in ORDER:
        raise ValueError(f"{level!r} is not a staff level.")

    held = level_of(user)
    if not held:
        return False
    return ORDER.index(held) >= ORDER.index(level)


def require(user: User | None, level: str) -> StaffRole:
    """Raise unless this person holds `level` or above.

    `NotFound` for somebody with no staff role at all, `NotAllowed` for a
    member of staff who is not senior enough. The distinction is deliberate: a
    staff screen should not exist as far as a candidate is concerned, and a
    404 is the only answer that says so. A support agent who reaches a
    moderator screen has an authorisation problem, not a mystery.
    """
    role = role_of(user)
    if role is None:
        raise NotFound("That page does not exist.")
    if not at_least(user, level):
        raise NotAllowed(f"That action needs {level} access.")
    return role
