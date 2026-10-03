"""
Online payment screens (ADR-A58).

Three views, and only one of them trusts the person in front of it:

  * **`PayView`** (POST, signed in) — the payer chooses a gateway for one of
    their invoices. Ownership is checked here; the attempt is written; the
    customer is handed to the gateway from a page whose own policy allows that
    one gateway's form target and nothing else.
  * **`ReturnView`** (GET, anyone) — the gateway sent the browser back. It is
    not signed in by assumption: a return is checked against the attempt and
    the gateway, never against the session. Afterwards it redirects to the
    attempt's page, so a refresh or the Back button re-reads a result rather
    than re-running a callback.
  * **`AttemptView`** (signed in) — what happened, and "check again" while the
    gateway is still deciding.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.common import ratelimit, restrictions
from apps.common.csp import ENFORCE_HEADER, POLICY
from apps.common.errors import DomainError

from . import policies
from .gateways import base as gateways
from .models import AttemptState, Invoice, PaymentAttempt
from .providers.base import PaymentUnverified
from .services import checkout

#: Returns are anonymous by design, so they are limited per address.
RETURN_PER_IP = ratelimit.Rule("billing.return", limit=60, window_seconds=60, fail_closed=False)
CHECK_PER_USER = ratelimit.Rule("billing.check", limit=20, window_seconds=60, fail_closed=False)


def back_url_for(invoice: Invoice) -> str:
    company = invoice.subscription.company
    if company is not None:
        return reverse("employer:billing", args=[company.slug])
    return reverse("dashboard:plan")


@method_decorator(login_required, name="dispatch")
class PayView(View):
    """`POST /billing/invoices/<number>/pay/` — start paying one invoice."""

    def post(self, request: HttpRequest, number: str) -> HttpResponse:
        invoice = (
            Invoice.objects.select_related("subscription__company").filter(number=number).first()
        )
        if invoice is None:
            raise Http404("That invoice is not one you can pay.")
        policies.require_invoice_payable(request.user, invoice)

        def urls_for(reference: str) -> gateways.ReturnUrls:
            key = request.POST.get("gateway", "")
            return gateways.ReturnUrls(
                success=request.build_absolute_uri(
                    reverse("billing:return", args=[key, reference])
                ),
                failure=request.build_absolute_uri(
                    reverse("billing:return_failed", args=[key, reference])
                ),
                website=request.build_absolute_uri("/"),
            )

        try:
            attempt, handoff = checkout.begin(
                invoice=invoice,
                gateway_key=request.POST.get("gateway", ""),
                actor=request.user,
                urls_for=urls_for,
            )
        except DomainError as error:
            restrictions.flash(request, error)
            return redirect(back_url_for(invoice))

        response = render(
            request,
            "billing/handoff.html",
            {"attempt": attempt, "checkout": handoff, "invoice": invoice},
        )
        # The page's own policy: the one gateway origin this form may post to.
        # Every other page keeps `form-action 'self'`.
        target = urlsplit(handoff.url)
        origin = f"{target.scheme}://{target.netloc}"
        response[ENFORCE_HEADER] = POLICY.format(nonce=request.csp_nonce).replace(  # type: ignore[attr-defined]
            "form-action 'self'", f"form-action 'self' {origin}"
        )
        response["Cache-Control"] = "no-store"
        return response


class ReturnView(View):
    """`GET /billing/return/<gateway>/<reference>/[failed/]` — the gateway sent the browser back."""

    failed = False

    def get(self, request: HttpRequest, gateway: str, reference: str) -> HttpResponse:
        ratelimit.check(RETURN_PER_IP, ratelimit.client_ip(request))
        try:
            attempt = checkout.handle_return(
                gateway_key=gateway,
                reference=reference,
                params={k: request.GET.get(k, "") for k in request.GET},
                failed=self.failed,
            )
        except (PaymentUnverified, gateways.GatewayNotConfigured) as error:
            return render(
                request,
                "billing/return_refused.html",
                {"message": error.message},
                status=400,
            )
        return redirect("billing:attempt", public_id=attempt.public_id)


@method_decorator(login_required, name="dispatch")
class AttemptView(View):
    """`/billing/payments/<public_id>/` — the outcome, and "check again"."""

    def _attempt(self, request: HttpRequest, public_id: str) -> PaymentAttempt:
        attempt = get_object_or_404(
            PaymentAttempt.objects.select_related(
                "invoice", "invoice__subscription", "invoice__subscription__company", "payment"
            ),
            public_id=public_id,
        )
        policies.require_attempt_visible(request.user, attempt)
        return attempt

    def get(self, request: HttpRequest, public_id: str) -> HttpResponse:
        attempt = self._attempt(request, public_id)
        response = render(
            request,
            "billing/attempt.html",
            {
                "attempt": attempt,
                "invoice": attempt.invoice,
                "back_url": back_url_for(attempt.invoice),
                "can_check": attempt.state in (AttemptState.INITIATED, AttemptState.PENDING),
                "can_retry": attempt.state
                in (AttemptState.CANCELLED, AttemptState.EXPIRED, AttemptState.FAILED)
                and bool(checkout.offered(attempt.invoice)),
            },
        )
        response["Cache-Control"] = "no-store"
        return response

    def post(self, request: HttpRequest, public_id: str) -> HttpResponse:
        attempt = self._attempt(request, public_id)
        ratelimit.check(CHECK_PER_USER, str(request.user.pk))
        if not attempt.is_final:
            attempt = checkout.reconcile(attempt)
            if attempt.state == AttemptState.PENDING:
                messages.info(request, "Still waiting for the payment service to confirm.")
        return redirect("billing:attempt", public_id=attempt.public_id)
