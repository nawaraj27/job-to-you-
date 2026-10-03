"""
What a fragment endpoint says to a request that is not htmx.

The candidate screens swap regions: a button posts, the view renders one
partial, and htmx puts it in place. The same endpoints are also reached by
ordinary forms — "Use this template" on a template's page, "Start a CV from
this example", the hub's "Start writing" — and a plain form navigates to
whatever comes back. What came back was the partial on its own: no
stylesheet, no header, no navigation, a gallery of CVs drawn as bare lists
under an SVG tick the size of the window, or "Not included in your plan"
under a warning triangle 1,000 pixels tall.

The answer to a full-page request has to be a page. This module is the one
rule for that, so each fragment helper asks it instead of growing its own:

  * a success, or a refusal the reader can act on in place, is a flash
    message on the page they came from (post / redirect / get);
  * a plan refusal is the full restriction page, with the upgrade and the
    plan list as its actions, because the page they came from offered the
    thing they have just been refused.

Anything that is not a page navigation (`is_page_navigation`) gets None, and
the caller renders its partial as before.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from . import restrictions
from .errors import DomainError


def is_htmx(request: HttpRequest) -> bool:
    return bool(request.headers.get("HX-Request"))


def is_page_navigation(request: HttpRequest) -> bool:
    """Whether the browser will show this response as a whole page.

    A plain form post is a navigation: every current browser says so with
    `Sec-Fetch-Dest: document`, and asks for `text/html` in `Accept`. htmx
    never sends either (and says who it is with `HX-Request`), so a region
    request keeps getting its region — as do callers that are neither, such
    as a script or a test client posting to the endpoint directly.
    """
    if is_htmx(request):
        return False
    if request.headers.get("Sec-Fetch-Dest") == "document":
        return True
    return "text/html" in (request.headers.get("Accept") or "")


def safe_referer(request: HttpRequest) -> str:
    """The page the request came from, when it is on this site."""
    referer = request.headers.get("referer") or ""
    if referer and url_has_allowed_host_and_scheme(
        referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return referer
    return ""


def page_answer(
    request: HttpRequest,
    *,
    message: str | None = None,
    error: object = None,
    fallback_url: str,
    next_url: str = "",
) -> HttpResponse | None:
    """The full-page answer to a fragment endpoint, or None when it is not one.

    `next_url` is where a success goes when it has somewhere better to be than
    back (applying a template goes to the editor it now draws); `fallback_url`
    is where anything goes when the referer is missing or off-site.
    """
    if not is_page_navigation(request):
        return None

    back = safe_referer(request) or fallback_url

    if isinstance(error, DomainError):
        restriction = restrictions.describe(error, request)
        if restriction is not None and restriction.kind == "plan":
            response = render(
                request,
                "restriction.html",
                {"restriction": restriction, "back_url": back},
                status=restriction.status,
            )
            response["X-Restriction"] = restriction.kind
            return response
        restrictions.flash(request, error)
        return redirect(back)

    if error:
        messages.error(request, str(error))
        return redirect(back)

    if message:
        messages.success(request, message)
    return redirect(next_url or back)
