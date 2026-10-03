"""
The candidate's plan screen (SRS §5.14, P18).

Three things the page must do, in this order of importance:

  1. **Say what is left**, before it says what to buy. Somebody arriving here
     after a refusal wants to know when their allowance resets, not to read a
     sales page.
  2. **Never be the enforcement point.** Every button posts to a service that
     re-checks; the page only decides what to show.
  3. **Make cancelling as easy as subscribing.** The cancel button is on the
     same screen, not behind a support email.

Nothing here takes a payment itself. An open invoice can be paid online through
a sandbox gateway (eSewa, Khalti; ADR-A58) when one is configured, or by bank
transfer recorded by staff (ADR-A15). What this page does is decision D-08: moving to Pro
starts it at once, on a new period from today, and raises that period's full
invoice — access does not wait for a bank transfer to be settled by hand, and
the nightly dunning run ends it if the invoice is never paid. Moving back to
Free is scheduled for the end of the period; Pro works until then and its
invoice stays owed. An overdue invoice blocks any new or upgraded plan. There
is no proration.
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
from apps.billing.gateways import base as gateways
from apps.billing.money import format_money
from apps.billing.services import checkout, entitlements, plans, subscriptions
from apps.common import restrictions
from apps.common.errors import DomainError


@method_decorator(login_required, name="dispatch")
class PlanView(View):
    """`/dashboard/plan/` — what you are on, what you have used, what else there is."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = getattr(request.user, "candidate_profile", None)
        if profile is None:
            return redirect("dashboard:index")

        subscription = selectors.subscription_for(profile)
        from apps.billing.services import invoices

        current_code = entitlements.plan_for(profile).code
        cards = [
            {**card, "is_current": card["code"] == current_code}
            for card in selectors.plan_cards(audience="candidate")
        ]
        return render(
            request,
            "dashboard/plan.html",
            {
                # Decision D-08: an overdue invoice blocks any new or paid plan,
                # and the page says so before anybody presses a button.
                "overdue": invoices.overdue_for(profile),
                "plan": entitlements.plan_for(profile),
                "subscription": subscription,
                "usage": selectors.usage_summary(profile),
                "invoices": checkout.annotate(selectors.invoices_for(profile, limit=12)),
                # A paid plan chosen and not yet paid for (ADR-A59).
                "pending": _pending_checkout(profile),
                "show_pay": True,
                "online_gateways": gateways.enabled(),
                "payments_test_mode": gateways.environment() == "test",
                # Every plan side by side, the current one marked: a comparison
                # is only a comparison when both sides are on the page.
                "cards": cards,
                "current_card": next((card for card in cards if card["is_current"]), None),
                "options": [card for card in cards if not card["is_current"]],
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Cancel or resume. Both are the candidate's own decision to make."""
        profile = getattr(request.user, "candidate_profile", None)
        if profile is None:
            return redirect("dashboard:index")

        subscription = selectors.subscription_for(profile)
        if subscription is None and request.POST.get("action") != "abandon_checkout":
            messages.info(request, "You are on the free plan; there is nothing to cancel.")
            return redirect(reverse("dashboard:plan"))

        if subscription is not None:
            policies.require_own_subscription(subscription, profile)
        action = request.POST.get("action", "")

        try:
            if action == "cancel":
                subscriptions.cancel(subscription=subscription, actor=request.user)
                messages.success(
                    request,
                    f"Your plan ends on {subscription.cancel_at:%d %b %Y}. "
                    "Nothing changes until then.",
                )
            elif action == "resume":
                subscriptions.resume(subscription=subscription, actor=request.user)
                messages.success(request, "Your plan will keep running.")
            elif action == "abandon_checkout":
                checkout = subscriptions.pending_checkout(profile)
                if checkout is not None:
                    subscriptions.abandon_checkout(
                        subscription=checkout,
                        actor=request.user,
                        reason="cancelled by the candidate",
                    )
                    messages.success(
                        request, f"{checkout.plan.name} was not started. Nothing was charged."
                    )
                return redirect(reverse("dashboard:plan"))
            elif action == "keep_plan":
                subscriptions.keep_plan(subscription=subscription, actor=request.user)
                messages.success(request, f"You are staying on {subscription.plan.name}.")
            else:
                messages.error(request, "That is not something this page can do.")
        except DomainError as error:
            # Caught rather than left to the middleware so the person stays on
            # the page they were reading, with the reason attached to it.
            restrictions.flash(request, error)

        return redirect(reverse("dashboard:plan"))


@method_decorator(login_required, name="dispatch")
class StartPlanView(View):
    """`/dashboard/plan/start/`: choose a plan.

    A paid plan opens a checkout (ADR-A59): an invoice now, the plan when the
    invoice is verified paid, by eSewa/Khalti or a recorded bank transfer.
    Choosing it again returns the same checkout. Free, and moves back to Free,
    follow D-08 unchanged: a downgrade waits for the end of the paid period.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = getattr(request.user, "candidate_profile", None)
        if profile is None:
            return redirect("dashboard:index")

        existing = selectors.subscription_for(profile)
        try:
            plan = plans.by_code(request.POST.get("plan", ""))
            if (
                not plan.is_free
                and not plan.is_quoted
                and (existing is None or existing.plan_id != plan.pk)
            ):
                # ADR-A59: a paid plan starts when its invoice is paid. Choosing
                # it opens a checkout; the current plan carries on meanwhile.
                _pending, invoice = subscriptions.start_checkout(
                    owner=profile, plan=plan, actor=request.user
                )
                messages.success(
                    request,
                    f"{plan.name} is ready to pay for: invoice {invoice.number}, "
                    f"{format_money(invoice.total, invoice.currency)}. "
                    f"{plan.name} starts as soon as the payment is confirmed.",
                )
                return redirect(reverse("dashboard:plan") + "#checkout")
            # Choosing the current plan while a move away is scheduled keeps it.
            keeping = (
                existing is not None
                and existing.pending_plan_id is not None
                and existing.plan_id == plan.pk
            )
            if existing is not None:
                # An upgrade or a downgrade keeps the subscription: cancelling
                # and re-subscribing would reset the period, lose the provider
                # reference, and send a cancellation email for an upgrade.
                policies.require_own_subscription(existing, profile)
                subscription = subscriptions.change_plan(
                    subscription=existing, plan=plan, actor=request.user
                )
            else:
                subscription = subscriptions.subscribe(
                    owner=profile, plan=plan, actor=request.user, provider="manual"
                )
        except DomainError as error:
            restrictions.flash(request, error)
            return redirect(reverse("dashboard:plan"))

        if subscription.pending_plan_id is not None:
            # Paid -> Free waits for the period end (D-08).
            messages.success(
                request,
                f"You move to {subscription.pending_plan.name} on "
                f"{subscription.current_period_end:%d %b %Y}. {subscription.plan.name} keeps "
                "working until then, and its invoice is still due.",
            )
        elif keeping:
            # Nothing new to bill: the current period's invoice already stands.
            messages.success(request, f"You are staying on {subscription.plan.name}.")
        else:
            messages.success(request, f"You are on {plan.name}.")

        return redirect(reverse("dashboard:plan"))


def _pending_checkout(profile) -> dict | None:
    """The waiting plan, its open invoice and how it can be paid."""
    from apps.billing.models import InvoiceState

    pending = subscriptions.pending_checkout(profile)
    if pending is None:
        return None
    invoice = pending.invoices.filter(state=InvoiceState.OPEN).order_by("-issued_on").first()
    if invoice is not None:
        checkout.annotate([invoice])
    return {"subscription": pending, "invoice": invoice}
