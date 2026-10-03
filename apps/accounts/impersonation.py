"""
Staff impersonation (SRS FR-1.10, §2.3 Platform Support).

"Staff impersonation with a recorded reason, a 60-minute expiry, no
staff-on-staff use, and an `on_behalf_of` stamp on every action performed."

How it works:

  * The staff member stays signed in as themselves. Their session carries an
    `impersonation` record naming the person, the reason and the expiry.
  * `ImpersonationMiddleware` swaps `request.user` for that person on each
    request, keeps the staff member on `request.impersonator`, and sets a
    context variable for the duration of the request.
  * `audit.record` reads that variable: during impersonation every entry is
    written with the staff member as `actor` and the person as `on_behalf_of`,
    whatever the calling service passed. No service has to know.
  * The account's own keys stay out of reach: security settings, data export
    and erasure, the staff screens and the admin refuse while impersonating.
    Seeing what somebody sees is support; changing their password is not.

The banner is in `templates/partials/impersonation_banner.html`.
"""

from __future__ import annotations

import contextvars
import datetime as dt
from collections.abc import Callable
from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.utils import timezone

SESSION_KEY = "accounts:impersonation"
DURATION = dt.timedelta(minutes=60)

#: Refused while impersonating: the account's keys, its data, and every
#: surface with authority of its own.
BLOCKED_PREFIXES = (
    "/settings/security/",
    "/settings/data/",
    "/accounts/password/",
    "/accounts/verify/",
    "/staff/",
    "/admin/",
)
#: Always reachable, so impersonation can be ended from any page.
STOP_PATH = "/staff/impersonation/stop/"

_impersonator: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "careerai_impersonator", default=None
)


def current_impersonator_id() -> int | None:
    return _impersonator.get()


def is_staff_account(user: Any) -> bool:
    from apps.accounts import staff

    return staff.role_of(user) is not None or bool(user.is_staff) or bool(user.is_superuser)


def start(request: HttpRequest, *, target: Any, reason: str) -> None:
    from apps.accounts import staff
    from apps.audit import services as audit
    from apps.common.errors import InvalidState, NotAllowed

    actor: Any = getattr(request, "impersonator", None) or request.user
    if not staff.at_least(actor, "support"):
        raise NotAllowed("Impersonation needs a staff role.")
    reason = (reason or "").strip()
    if not reason:
        raise InvalidState("Record why you need to see this account as its owner.")
    if target.pk == actor.pk:
        raise InvalidState("That is your own account.")
    if is_staff_account(target):
        raise NotAllowed("Staff accounts cannot be impersonated.")
    if not target.is_active:
        raise InvalidState("That account is suspended.")

    expires = timezone.now() + DURATION
    request.session[SESSION_KEY] = {
        "staff_id": actor.pk,
        "target_id": target.pk,
        "reason": reason[:300],
        "expires": expires.timestamp(),
    }
    audit.record(
        verb="account.impersonation_started",
        actor=actor,
        on_behalf_of=target,
        target=target,
        metadata={"reason": reason[:300], "expires": expires.isoformat()},
    )


def stop(request: HttpRequest, *, expired: bool = False) -> None:
    from apps.accounts.models import User
    from apps.audit import services as audit

    record = request.session.pop(SESSION_KEY, None)
    if not record:
        return
    staff_user = User.objects.filter(pk=record.get("staff_id")).first()
    target = User.objects.filter(pk=record.get("target_id")).first()
    token = _impersonator.set(None)
    try:
        audit.record(
            verb="account.impersonation_ended",
            actor=staff_user,
            on_behalf_of=target,
            target=target,
            metadata={"expired": expired},
        )
    finally:
        _impersonator.reset(token)


class ImpersonationMiddleware:
    """After authentication and OTP, before tenancy and everything that reads the user."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        record = request.session.get(SESSION_KEY) if hasattr(request, "session") else None
        user = getattr(request, "user", None)
        if not record or user is None or not user.is_authenticated:
            return self.get_response(request)

        from apps.accounts.models import User

        if record.get("staff_id") != user.pk:
            # A different person signed in on this browser: the record is not
            # theirs to use.
            request.session.pop(SESSION_KEY, None)
            return self.get_response(request)

        if timezone.now().timestamp() >= float(record.get("expires", 0)):
            stop(request, expired=True)
            request._impersonation_expired = True  # type: ignore[attr-defined]
            return self.get_response(request)

        target = User.objects.filter(pk=record.get("target_id"), is_active=True).first()
        if target is None or is_staff_account(target):
            stop(request)
            return self.get_response(request)

        staff_user = user
        # The staff member passed two-factor to get here; the session being
        # borrowed is theirs, so it counts as verified for the person too.
        target.is_verified = lambda: True  # type: ignore[attr-defined]
        request.impersonator = staff_user  # type: ignore[attr-defined]
        request.impersonation = {  # type: ignore[attr-defined]
            "target": target,
            "reason": record.get("reason", ""),
            "expires": dt.datetime.fromtimestamp(float(record["expires"]), tz=dt.UTC),
        }
        request.user = target
        token = _impersonator.set(staff_user.pk)
        try:
            return self.get_response(request)
        finally:
            _impersonator.reset(token)

    def process_view(self, request: HttpRequest, view_func, view_args, view_kwargs):
        """Refusals and notices, once the messages framework is set up."""
        if getattr(request, "_impersonation_expired", False):
            messages.info(request, "Impersonation ended after 60 minutes.")
        if (
            getattr(request, "impersonation", None)
            and request.path.startswith(BLOCKED_PREFIXES)
            and request.path != STOP_PATH
        ):
            messages.error(
                request, "Not while viewing another account. End the impersonation first."
            )
            return redirect("dashboard:index")
        return None


def banner(request: HttpRequest) -> dict[str, Any]:
    """Context processor: the banner on every page while impersonating."""
    return {"impersonation": getattr(request, "impersonation", None)}
