"""
`/settings/security/` — how somebody gets into their account (FR-1.2 to FR-1.5).

Everything here belongs to the account rather than to a candidate profile, so
it is open to every signed-in person — an employer-only account included — and
never creates a profile on the way.

Four sections, each backed by a service that already existed and had no screen:

  * **Email** — whether the sign-in address is confirmed, and a fresh link.
  * **Password** — change it knowing the current one. The account page's
    "Change password" button linked to the sign-in page, which sends a
    signed-in person straight back.
  * **Two-factor** — set up, recovery codes, turn off with a code. Mandatory
    for staff and for company owners and admins (`accounts.mfa_policy`).
  * **Where you're signed in** — every session, and ending them.

Full-page POSTs that redirect back with a message: these are rare, deliberate
acts and a reload must never repeat one.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from apps.accounts import mfa_policy
from apps.accounts.services import identity, mfa
from apps.common import restrictions
from apps.common.errors import DomainError

#: Session key holding freshly issued recovery codes until the next page load.
FRESH_CODES = "settings:fresh_recovery_codes"


def _group(secret: str) -> str:
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


@method_decorator(login_required, name="dispatch")
class SecurityView(View):
    template_name = "settings/security.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        from .views import _tabs

        user = request.user
        pending = mfa.totp_device(user, confirmed=False)
        address = user.email_addresses.filter(is_primary=True).first()
        current_key = request.session.session_key
        sessions = [
            {"row": row, "current": row.session_key == current_key}
            for row in identity.active_sessions(user)
        ]
        requirement = mfa_policy.requirement_for(user)
        return render(
            request,
            self.template_name,
            {
                "page_title": "Security",
                "page_text": "How you sign in, and where you are signed in now.",
                "settings_tabs": _tabs("security"),
                "address": address,
                "mfa_enabled": mfa.is_enabled(user),
                "mfa_pending": pending,
                # Grouped in fours for typing; authenticators ignore the spaces.
                "mfa_secret": _group(_base32(pending)) if pending else "",
                "mfa_uri": mfa.provisioning_uri(pending) if pending else "",
                "recovery_left": mfa.remaining_recovery_codes(user=user),
                "fresh_codes": request.session.pop(FRESH_CODES, None),
                "requirement": requirement,
                "sessions": sessions,
                "other_sessions": sum(1 for s in sessions if not s["current"]),
            },
        )


def _base32(device) -> str:
    import base64

    return base64.b32encode(device.bin_key).decode("ascii").rstrip("=")


@method_decorator(login_required, name="dispatch")
class SecurityCommandView(View):
    def post(self, request: HttpRequest) -> HttpResponse:
        command = request.POST.get("command", "")
        user = request.user
        handler = getattr(self, f"do_{command}", None)
        if handler is None:
            messages.error(request, "That is not something this page can do.")
            return redirect("settings:security")
        try:
            message = handler(request, user)
        except DomainError as exc:
            restrictions.flash(request, exc)
        else:
            messages.success(request, message)
        return redirect("settings:security")

    def do_password(self, request: HttpRequest, user) -> str:
        new = request.POST.get("new_password", "")
        if new != request.POST.get("confirm_password", ""):
            from apps.common.errors import InvalidState

            raise InvalidState("The two new passwords do not match.")
        ended = identity.change_password(
            user=user,
            current_password=request.POST.get("current_password", ""),
            new_password=new,
            keep_session_key=request.session.session_key,
        )
        # The hash in this session is of the old password; without this the
        # person who just changed it is signed out on their next click.
        update_session_auth_hash(request, user)
        if ended:
            return (
                f"Password changed. Signed out of {ended} other session{'s' if ended != 1 else ''}."
            )
        return "Password changed."

    def do_mfa_begin(self, request: HttpRequest, user) -> str:
        mfa.begin_enrolment(user=user)
        return "Add the key below to your authenticator app, then enter the code it shows."

    def do_mfa_confirm(self, request: HttpRequest, user) -> str:
        from django_otp import login as otp_login

        codes = mfa.confirm_enrolment(user=user, code=request.POST.get("code", ""))
        # This session has just proved the device; the protected pages should
        # not ask again straight away.
        device = mfa.totp_device(user, confirmed=True)
        if device is not None:
            otp_login(request, device)
        request.session[FRESH_CODES] = codes
        return "Two-factor authentication is on. Save your recovery codes now."

    def do_mfa_cancel(self, request: HttpRequest, user) -> str:
        from django_otp.plugins.otp_totp.models import TOTPDevice

        TOTPDevice.objects.filter(user=user, name=mfa.TOTP_DEVICE_NAME, confirmed=False).delete()
        return "Set-up cancelled. Nothing was turned on."

    def do_mfa_disable(self, request: HttpRequest, user) -> str:
        requirement = mfa_policy.requirement_for(user)
        if requirement.required:
            from apps.common.errors import NotAllowed

            raise NotAllowed(
                "Your role requires two-factor authentication "
                f"({', '.join(requirement.reasons)}), so it cannot be turned off."
            )
        mfa.disable(user=user, code=request.POST.get("code", ""))
        return "Two-factor authentication is off."

    def do_mfa_codes(self, request: HttpRequest, user) -> str:
        if not mfa.verify(user=user, code=request.POST.get("code", "")):
            from apps.common.errors import NotAllowed

            raise NotAllowed("That code is not correct.")
        request.session[FRESH_CODES] = mfa.regenerate_recovery_codes(user=user)
        return "New recovery codes issued. The old ones no longer work."

    def do_revoke_session(self, request: HttpRequest, user) -> str:
        # Identified by row id, never by session key: a session key is the
        # cookie itself, and printing the others into this page would put every
        # live credential on the account one XSS or screen share away.
        from apps.common.errors import InvalidState, NotAllowed
        from apps.common.ids import as_pk

        row = identity.active_sessions(user).filter(pk=as_pk(request.POST.get("session"))).first()
        if row is None:
            raise NotAllowed("That session does not exist.")
        if row.session_key == request.session.session_key:
            raise InvalidState("That is this session. Use Sign out instead.")
        identity.revoke_session(actor=user, session_key=row.session_key)
        return "That session is signed out."

    def do_revoke_others(self, request: HttpRequest, user) -> str:
        ended = identity.revoke_all_sessions(user=user, except_key=request.session.session_key)
        if not ended:
            return "There were no other sessions."
        return f"Signed out of {ended} other session{'s' if ended != 1 else ''}."
