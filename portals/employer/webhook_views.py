"""
Webhook endpoints, per company (SRS §5.12).

The secret is shown after creation and after a rotation, and at no other time.
It is stored encrypted rather than hashed (ADR-A45, and see the model) because
this server signs with it on every delivery — but the screen still treats it as
something to copy once, because a page that prints a signing key on every load
is a page somebody eventually screenshots.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.common import restrictions
from apps.common.errors import DomainError
from apps.companies import policies as company_policies
from apps.companies.permissions import Grant
from apps.notifications import selectors as notification_selectors
from apps.notifications.events import registry
from apps.notifications.models import WebhookEndpoint
from apps.notifications.services import webhooks


@method_decorator(login_required, name="dispatch")
class WebhookView(View):
    """`/employer/<slug>/webhooks/`."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_section(request.membership, "company")
        endpoints = notification_selectors.endpoints_for(request.company)

        return render(
            request,
            "employer/webhooks.html",
            {
                "company": request.company,
                "membership": membership,
                "endpoints": endpoints,
                "event_types": sorted(registry()),
                "recent": notification_selectors.recent_deliveries(request.company),
                "can_manage": Grant.COMPANY_EDIT in membership.grants,
                # Shown once, immediately after it is created or rotated.
                "new_secret": request.session.pop("webhook_secret", None),
            },
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        back = reverse("employer:webhooks", args=[company_slug])
        action = request.POST.get("action") or "create"

        try:
            company_policies.require_can_edit_company(membership)
            message = self._run(request, action)
        except DomainError as exc:
            restrictions.flash(request, exc)
            return redirect(back)

        messages.success(request, message)
        return redirect(back)

    def _run(self, request: HttpRequest, action: str) -> str:
        if action == "create":
            _, secret = webhooks.create_endpoint(
                company=request.company,
                actor=request.user,
                url=request.POST.get("url", ""),
                event_types=request.POST.getlist("event_types"),
                description=request.POST.get("description", ""),
            )
            request.session["webhook_secret"] = secret
            return "Endpoint created. Copy the signing secret now."

        endpoint = self._endpoint(request)

        if action == "rotate":
            request.session["webhook_secret"] = webhooks.rotate_secret(
                endpoint=endpoint, actor=request.user
            )
            return "Secret rotated. The old one stops working immediately."

        if action in {"enable", "disable"}:
            webhooks.set_active(endpoint=endpoint, actor=request.user, active=action == "enable")
            return "Endpoint enabled." if action == "enable" else "Endpoint disabled."

        if action == "delete":
            webhooks.delete_endpoint(endpoint=endpoint, actor=request.user)
            return "Endpoint deleted."

        raise Http404("Unknown action.")

    def _endpoint(self, request: HttpRequest) -> WebhookEndpoint:
        """Scoped to this company: an id from another tenant resolves to nothing."""
        endpoint = notification_selectors.endpoint_for(
            request.company, request.POST.get("endpoint", "")
        )
        if endpoint is None:
            raise Http404("No such endpoint.")
        return endpoint
