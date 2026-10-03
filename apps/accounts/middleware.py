"""
Enforce the two-factor requirement on the surfaces that need it (FR-1.5).

After `CompanyContextMiddleware`, because whether an employer page is protected
depends on the role the URL's company gives this person.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

from . import mfa_policy

#: Never intercepted: the pages that satisfy the requirement, and signing out.
EXEMPT_PREFIXES = ("/accounts/", "/settings/security/", "/static/", "/healthz", "/readyz")


class MfaRequirementMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(self, request: HttpRequest, view_func, view_args, view_kwargs):
        """After URL resolution, so `request.membership` is the URL's company.

        `CompanyContextMiddleware` binds the membership in its own
        `process_view`; this runs after it because it is later in MIDDLEWARE.
        """
        user = getattr(request, "user", None)
        if (
            user is None
            or not user.is_authenticated
            or request.path.startswith(EXEMPT_PREFIXES)
            or not mfa_policy.surface_requires(request)
        ):
            return None

        from .services import mfa

        if mfa.is_enabled(user):
            if not user.is_verified():
                # Signed in without a code: before the device existed, or by a
                # path that never asked. The protected page waits for one.
                query = urlencode({"next": request.get_full_path()})
                return redirect(f"{reverse('accounts:mfa_challenge')}?{query}")
            return None

        requirement = mfa_policy.requirement_for(user)
        if not requirement.required:
            return None

        if requirement.overdue:
            messages.warning(
                request,
                "Two-factor authentication is required for your role. Set it up to continue.",
            )
            return redirect("settings:security")

        if not request.session.get("mfa_grace_notice"):
            request.session["mfa_grace_notice"] = True
            messages.info(
                request,
                "Your role needs two-factor authentication. Set it up in Settings, Security, "
                f"before {requirement.deadline:%d %b %Y}.",
            )
        return None
