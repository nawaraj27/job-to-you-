"""
URL-scoped tenancy (ADR-A18).

Tenancy lives in the URL because a session cannot hold two values at once. A
recruiter working at two companies in two browser tabs shares one session; with
session-scoped tenancy the tab that loses the race writes its note, its
rejection or its job edit into the wrong company. Putting the company in the
path makes that structurally impossible.

Resolution happens in `process_view`, not `__call__`, so the company is taken
from the **resolved URL keyword** rather than by parsing the path. Parsing would
mean this module and `urls.py` both deciding what counts as a slug, and they
would disagree the first time a non-company route was added under the prefix —
`/employer/new/` would be looked up as a company and 404 for everyone.

A missing or inactive membership is a 404, never a 403: a 403 confirms the
company exists to someone with no right to know.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import Http404, HttpRequest, HttpResponse

from apps.common import logging as log

from . import selectors

URL_KWARG = "company_slug"
SESSION_HINT_KEY = "last_company_slug"

#: Slugs that would shadow a route under /employer/. Refused at creation.
RESERVED_SLUGS = frozenset({"new", "invites", "go", "admin", "api", "static", "media"})


class CompanyContextMiddleware:
    """Bind `request.company` and `request.membership` from the URL."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Always present, so a view never has to ask whether the attribute
        # exists before asking whether there is a tenant.
        request.company = None  # type: ignore[attr-defined]
        request.membership = None  # type: ignore[attr-defined]
        return self.get_response(request)

    def process_view(self, request: HttpRequest, view_func, view_args, view_kwargs):
        slug = view_kwargs.get(URL_KWARG)
        if not slug:
            return None

        if not request.user.is_authenticated:
            # Let the view's login_required send them to sign in. A 404 here
            # would be a worse experience and protects nothing: an anonymous
            # visitor learns nothing from a redirect to a login page.
            return None

        membership = selectors.membership_by_slug(user=request.user, slug=slug)
        if membership is None:
            # Covers all of: no such company, not a member, membership revoked,
            # company suspended or deleted. One answer, on purpose.
            raise Http404("No such company.")

        request.company = membership.company  # type: ignore[attr-defined]
        request.membership = membership  # type: ignore[attr-defined]

        # A hint only — used to redirect a bare /employer/ somewhere sensible.
        # Never an authority; the URL decides, on every request.
        request.session[SESSION_HINT_KEY] = slug

        request._company_log_token = log.bind(company_id=membership.company_id)  # type: ignore[attr-defined]
        return None

    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        token = getattr(request, "_company_log_token", None)
        if token is not None:
            log.reset(token)
        return response
