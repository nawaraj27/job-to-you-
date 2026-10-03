"""
Content Security Policy (ADR-A31).

Enforced, not report-only. A policy that only reports is a policy that stops an
attack never.

What `script-src` allows, and why it is now short
------------------------------------------------
`'self'` and a per-request `'nonce-…'`. No `'unsafe-inline'`, so an injected
inline script is blocked — the stored-XSS vector that matters when the input is
a candidate's CV. And, since P19, **no `'unsafe-eval'`**.

The exception is gone because its cause is: the app vendors the `@alpinejs/csp`
build, which parses directive expressions into an AST and walks it rather than
handing them to `new Function`. ADR-A31 scheduled that swap and it has happened.

The obligation it leaves behind is on templates, and it is a real one: every
Alpine expression must be something the CSP build can *evaluate* — property
access, member access, calls with arguments, ternaries, comparisons, logical
operators, assignments, `++`. Anything statement-shaped — a semicolon, an `if`,
an arrow function, a regular expression, a template literal — throws at runtime
and must live in a component method instead (`static/js/app.js`). Two such
expressions existed when the swap was made; both became methods.

`style-src` keeps `'unsafe-inline'` for Alpine's `x-bind:style`. Tailwind is
build-time and contributes nothing here.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

ENFORCE_HEADER = "Content-Security-Policy"
REPORT_HEADER = "Content-Security-Policy-Report-Only"

#: Rendered with the per-request nonce substituted into script-src.
POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'"
)


class ContentSecurityPolicyMiddleware:
    """Generate a per-request nonce and attach the policy to the response."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # 16 bytes of urandom, base64. Must be unpredictable per request, or an
        # attacker who can guess it can authorise their own injected script.
        request.csp_nonce = base64.b64encode(os.urandom(16)).decode()  # type: ignore[attr-defined]

        response = self.get_response(request)

        # Never overwrite a policy a view set deliberately.
        if ENFORCE_HEADER in response or REPORT_HEADER in response:
            return response

        header = REPORT_HEADER if getattr(settings, "CSP_REPORT_ONLY", False) else ENFORCE_HEADER
        response[header] = POLICY.format(nonce=request.csp_nonce)  # type: ignore[attr-defined]
        return response


def csp_nonce(request: HttpRequest) -> dict[str, str]:
    """Context processor, so templates can write nonce="{{ csp_nonce }}"."""
    return {"csp_nonce": getattr(request, "csp_nonce", "")}
