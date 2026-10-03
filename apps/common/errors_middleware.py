"""
One place where a DomainError becomes an HTTP response (DEVELOPER.md §3.7).

Services raise; views map. Without a single mapping, every view grows its own
try/except and they drift — one returns 500 for a permission failure, another
leaks an internal message. Here the status comes from the exception class, and
the message is the one the service wrote for the person who will read it.

HTMX requests get the same status with a bare message, so an inline error
fragment can render it rather than the client swallowing an unexpected body.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string

from .errors import DomainError, NotFound


class DomainErrorMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_exception(self, request: HttpRequest, exception: Exception) -> HttpResponse | None:
        from django.core.exceptions import ValidationError

        if isinstance(exception, ValidationError):
            # Malformed input reached the ORM — a UUID column handed "abc" is
            # the usual one. The person sent something invalid; that is a 400
            # with a sentence, never a 500. Specific views guard their own ids
            # (`common.ids`) so they can say something more useful than this.
            exception = DomainError("That request had something in it we could not read.")

        if not isinstance(exception, DomainError):
            return None

        # NotFound is the platform's "you cannot see this" answer, and it must
        # be indistinguishable from a genuine 404 (SRS §2.2).
        if isinstance(exception, NotFound):
            raise Http404(exception.message) from exception

        from . import restrictions

        restriction = restrictions.describe(exception, request)

        if request.headers.get("HX-Request"):
            if restriction is None:
                # A real failure keeps the client's error treatment.
                return HttpResponse(
                    exception.message, status=exception.status_code, content_type="text/plain"
                )
            # An expected refusal is swapped in like any other answer. The
            # status is kept — it is the truth, and tests and logs read it —
            # and the header tells app.js this 4xx is content, not breakage.
            # Without it htmx discarded the body and the client painted
            # "The connection dropped or the server didn't answer" over a
            # plan limit.
            response = HttpResponse(
                render_to_string(
                    "components/restriction.html", {"restriction": restriction}, request=request
                ),
                status=exception.status_code,
            )
            response["X-Restriction"] = restriction.kind
            return response

        referer = request.headers.get("referer")
        if referer and request.method == "POST":
            from django.utils.http import url_has_allowed_host_and_scheme

            # Only back to this site. The header is client-supplied, and an
            # error redirect is not somewhere to be sent off-site from.
            if url_has_allowed_host_and_scheme(
                referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                # A restriction is a warning on the page it came from, a
                # failure an error (RES-04).
                restrictions.flash(request, exception)
                return redirect(referer)

        if restriction is None:
            return HttpResponse(
                exception.message, status=exception.status_code, content_type="text/plain"
            )
        # A page, with the site's own shell and a way on, instead of one line
        # of plain text that read as the site having failed.
        response = render(
            request,
            "restriction.html",
            {"restriction": restriction},
            status=exception.status_code,
        )
        response["X-Restriction"] = restriction.kind
        return response
