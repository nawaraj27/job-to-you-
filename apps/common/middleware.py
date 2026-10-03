"""Request correlation context."""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .logging import bind, get_context, reset
from .uuid7 import uuid7

HEADER = "X-Request-ID"


class RequestContextMiddleware:
    """Bind the correlation envelope for the life of the request.

    Runs first in the chain so that session, CSRF and authentication all log
    with an id — authentication failures especially, which is where correlation
    is most useful and where the previous ordering left `request_id` unset.

    An inbound X-Request-ID is honoured so a proxy or load balancer can
    correlate its logs with ours, but it is length- and charset-capped: the
    value is written into every log line, and an unbounded header is a
    log-injection vector.

    `actor_id` and `company_id` are bound later by the authentication and
    tenancy layers (P1). They are never read from headers.
    """

    MAX_LENGTH = 64

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get(HEADER, "")
        request_id = self._clean(incoming) or str(uuid7())

        # Django's HttpRequest has no slot for this; attaching it is the
        # documented way to carry per-request context to views.
        request.request_id = request_id  # type: ignore[attr-defined]
        token = bind(request_id=request_id)
        try:
            response = self.get_response(request)
        finally:
            reset(token)

        response[HEADER] = request_id
        return response

    def _clean(self, value: str) -> str:
        value = value.strip()[: self.MAX_LENGTH]
        return value if value.isascii() and value.isprintable() else ""


def current_request_id() -> str | None:
    """Convenience for code that needs the id without a request in hand."""
    return get_context().request_id
