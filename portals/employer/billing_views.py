"""
The company's plan screen (SRS §5.14, §2.4).

Read-heavy on purpose. An employer plan is priced by agreement — the page says
"talk to us" — so there is no checkout here, and inventing one would be
advertising a purchase nobody can make (INV-6). What the screen does is answer
the two questions a team actually has: which plan are we on, and what have we
been invoiced.

Decision D-11: the Owner and Company Admin read it; only `billing.manage` may
act on it, which is the Owner alone — a company admin can do nearly everything
else and deliberately cannot move money. The control is hidden from the admin
and the POST refuses them, so the screen and the server say the same thing.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.billing import policies, selectors
from apps.billing.services import checkout, entitlements, subscriptions
from apps.common import restrictions
from apps.common.errors import DomainError
from apps.companies import policies as company_policies


@method_decorator(login_required, name="dispatch")
class BillingView(View):
    """`/employer/<slug>/billing/` — the plan, the usage, the invoices."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_section(request.membership, "billing")
        policies.require_company_view(membership)

        company = request.company
        return render(
            request,
            "employer/billing.html",
            {
                "company": company,
                "membership": membership,
                "plan": entitlements.plan_for(company),
                "subscription": selectors.subscription_for(company),
                "usage": selectors.usage_summary(company),
                "invoices": checkout.annotate(selectors.invoices_for(company, limit=12)),
                "can_manage": policies.can_manage_company_billing(membership),
                # Paying moves money, so only `billing.manage` sees the buttons (D-11).
                "show_pay": policies.can_manage_company_billing(membership),
            },
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        """Cancel or resume. The only writes a company can make to its own plan."""
        membership = company_policies.require_section(request.membership, "billing")
        policies.require_company_billing(membership)

        subscription = selectors.subscription_for(request.company)
        url = reverse("employer:billing", args=[company_slug])
        if subscription is None:
            messages.info(request, "This company has no subscription to change.")
            return redirect(url)

        action = request.POST.get("action", "")
        try:
            if action == "cancel":
                subscriptions.cancel(subscription=subscription, actor=request.user)
                messages.success(request, f"The plan ends on {subscription.cancel_at:%d %b %Y}.")
            elif action == "resume":
                subscriptions.resume(subscription=subscription, actor=request.user)
                messages.success(request, "The plan will keep running.")
            else:
                # Anything else used to fall through to "resume", so a stray or
                # malformed post quietly undid an owner's cancellation.
                messages.error(request, "That is not something this page can do.")
        except DomainError as error:
            restrictions.flash(request, error)

        return redirect(url)
