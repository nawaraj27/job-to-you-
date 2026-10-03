"""
Who must use two-factor authentication, and from when (SRS §2.1, FR-1.5).

  * **Platform staff** — "MFA-mandatory" (§2.1). Anybody holding a `StaffRole`,
    and anybody Django lets into the admin site (`is_staff`).
  * **Company owners and company admins** (FR-1.5).

Each gets fourteen days from the grant before it is enforced, so being made an
admin on a Friday does not lock somebody out of the company on Saturday. After
that, the surfaces the role opens send them to set it up first.

A person who has turned it on must also *use* it: a session that signed in
before the device existed, or one that was never challenged, is sent through
the code prompt before a protected surface opens.

Policy only. The middleware in `accounts.middleware` applies it; the security
settings screen explains it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.utils import timezone

GRACE_DAYS = 14


@dataclass(frozen=True)
class Requirement:
    required: bool
    since: dt.datetime | None
    reasons: tuple[str, ...]

    @property
    def deadline(self) -> dt.datetime | None:
        return self.since + dt.timedelta(days=GRACE_DAYS) if self.since else None

    @property
    def overdue(self) -> bool:
        deadline = self.deadline
        return deadline is not None and timezone.now() >= deadline


def requirement_for(user) -> Requirement:
    """Whether this person must use two-factor authentication, and since when."""
    if user is None or not getattr(user, "is_authenticated", False):
        return Requirement(False, None, ())

    from apps.companies.models import CompanyMembership
    from apps.companies.permissions import Role

    moments: list[dt.datetime] = []
    reasons: list[str] = []

    role = getattr(user, "staff_role", None)
    if role is not None:
        moments.append(role.granted_at)
        reasons.append("platform staff")
    elif user.is_staff:
        moments.append(user.date_joined)
        reasons.append("platform staff")

    managing = (
        CompanyMembership.objects.active()
        .filter(user=user, role__in=(Role.OWNER, Role.ADMIN))
        .order_by("created_at")
        .select_related("company")
    )
    first = managing.first()
    if first is not None:
        moments.append(first.created_at)
        reasons.append(f"{first.get_role_display().lower()} of {first.company.name}")

    if not moments:
        return Requirement(False, None, ())
    return Requirement(True, min(moments), tuple(reasons))


def surface_requires(request) -> bool:
    """Whether the page being requested is one the requirement protects."""
    user = request.user
    path = request.path
    if path.startswith("/staff/") or path.startswith("/admin/"):
        return getattr(user, "staff_role", None) is not None or user.is_staff
    membership = getattr(request, "membership", None)
    if membership is not None and path.startswith("/employer/"):
        from apps.companies.permissions import Role

        return membership.role in (Role.OWNER, Role.ADMIN)
    return False
