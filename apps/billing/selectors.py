"""
Scoped reads for the billing screens (SRS §7.1).

Everything here takes the owner as an argument and filters on it. There is no
`all_invoices()` and no unscoped queryset: an invoice names what somebody pays
and how much, and the one query that forgot its `WHERE` clause would put one
company's spend on another company's page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import Invoice, Payment, Subscription
from .money import format_money
from .services import entitlements, usage

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company


def subscription_for(owner: Any) -> Subscription | None:
    return entitlements.live_subscription(owner)


def history_for(owner: Any) -> list[Subscription]:
    """Every subscription this owner has had, newest first."""
    candidate, company = _columns(owner)
    if candidate is None and company is None:
        return []
    query = Subscription.objects.select_related("plan")
    query = query.filter(candidate=candidate) if candidate else query.filter(company=company)
    return list(query)


def invoices_for(owner: Any, *, limit: int = 24) -> list[Invoice]:
    candidate, company = _columns(owner)
    if candidate is None and company is None:
        return []
    query = Invoice.objects.select_related("subscription", "subscription__plan")
    query = (
        query.filter(subscription__candidate=candidate)
        if candidate
        else query.filter(subscription__company=company)
    )
    return list(query[:limit])


def invoice_for(owner: Any, *, number: str) -> Invoice | None:
    """One invoice, scoped. Returns `None` rather than raising: the caller
    decides whether a missing invoice is a 404 or an empty panel."""
    return next((row for row in invoices_for(owner, limit=200) if row.number == number), None)


def payments_for(invoice: Invoice) -> list[Payment]:
    return list(invoice.payments.all())


def usage_summary(owner: Any) -> list[dict[str, Any]]:
    """What this owner has used of each metered allowance, this period.

    Built for a screen: a metric with no limit still appears, showing the
    count, because "unlimited" is a useful thing to be told and a blank row is
    not.
    """
    from . import limits as limit_registry

    plan = entitlements.plan_for(owner)
    audience = entitlements.audience_of(owner)
    rows: list[dict[str, Any]] = []

    for key in sorted(limit_registry.METERED):
        limit = limit_registry.get(key)
        if limit.audience and limit.audience != audience:
            continue
        allowed = entitlements.value_of(owner, key)
        try:
            used = usage.used(
                owner=entitlements.meter_owner(owner), metric=key, period=limit.period
            )
        except ValueError:
            # An account with nothing to meter — an employer-only user reading
            # a candidate screen — has used nothing rather than being an error.
            used = 0
        rows.append(
            {
                "key": key,
                "label": limit.label,
                "used": used,
                "allowed": allowed,
                "remaining": entitlements.remaining(owner, key),
                "is_unlimited": allowed is None,
                "plan": plan.code,
                "period": limit.period,
                "resets_on": entitlements.resets_on(key),
            }
        )
    return rows


def plan_cards(audience: str | None = None) -> list[dict[str, Any]]:
    """The plans, described in the words of the limits they actually enforce.

    The pricing page is built from this rather than from a hand-written list,
    which is the whole point of P18: a page that advertises a limit the code
    does not enforce is the same defect as a feature that is linked to and not
    built, pointing the other way. Generating the bullets from the plan
    document makes the two impossible to separate.
    """
    from .models import Plan
    from .services import entitlements

    rows = Plan.objects.filter(is_active=True)
    if audience is not None:
        rows = rows.filter(audience=audience)

    cards = []
    for plan in rows:
        limits = entitlements.read_limits(plan.limits)
        cards.append(
            {
                "code": plan.code,
                "name": plan.name,
                "audience": plan.audience,
                "price": "Talk to us"
                if plan.is_quoted
                else format_money(plan.price, plan.currency),
                "cadence": "per company" if plan.is_quoted else f"per {plan.interval}",
                "summary": plan.summary,
                "is_quoted": plan.is_quoted,
                "is_free": plan.is_free,
                "features": _bullets(plan.audience, limits),
                "excluded": _excluded(plan.audience, limits),
            }
        )
    return cards


def _excluded(audience: str, limits: dict[str, Any]) -> list[str]:
    """What this plan does not include, for the pages that compare plans.

    Listed separately from `features` so a page can draw it as not-included.
    It used to be one of the features — "Downloads are part of Pro" — and the
    pricing page drew every feature with a tick, so the free plan appeared to
    include the one thing it withholds.
    """
    if audience != "candidate":
        return []
    lines = []
    if not limits.get("pro_templates"):
        lines.append("The Pro templates")
    if not list(limits.get("export_formats") or []):
        lines.append("Downloading your CV as PDF or Word")
    return lines


def _bullets(audience: str, limits: dict[str, Any]) -> list[str]:
    """One line per limit, saying exactly what the code will do."""
    lines: list[str] = []

    if audience == "candidate":
        analyses = limits.get("resume_analyses")
        lines.append(
            "Unlimited CV analyses"
            if analyses is None
            else f"{analyses} CV analysis per month"
            if analyses == 1
            else f"{analyses} CV analyses per month"
        )
        lines.append(
            "Every CV template" if limits.get("pro_templates") else "The free CV templates"
        )
        formats = list(limits.get("export_formats") or [])
        if formats:
            lines.append(f"Download as {' and '.join(fmt.upper() for fmt in formats)}")
        lines.extend(_allowance_lines(limits))
    else:
        jobs = limits.get("active_jobs")
        lines.append("Unlimited job postings" if jobs is None else f"Up to {jobs} open postings")
        if limits.get("candidate_search"):
            lines.append("Talent search and shortlisting")
        lines.append("Applicant pipeline with skill matching")

    # `ai_cost_monthly` is deliberately not a bullet. It is a ceiling on what
    # the platform spends, in dollars of provider cost — a number that means
    # nothing to a candidate and would read as a promise about tokens.
    return lines


def _allowance_lines(limits: dict[str, Any]) -> list[str]:
    """The per-feature AI allowances (ADR-A59), with the period each resets on.

    Read from the same registry `entitlements.claim` enforces, so a plan that
    allows 2 practice interviews a month says exactly that.
    """
    from .limits import ASSISTANT_REPLIES, COVER_LETTERS, CV_IMPROVEMENTS, INTERVIEW_PRACTICE
    from .models import UsagePeriod

    lines = []
    for limit in (ASSISTANT_REPLIES, CV_IMPROVEMENTS, INTERVIEW_PRACTICE, COVER_LETTERS):
        if limit.key not in limits:
            continue
        allowed = limits[limit.key]
        per = "a day" if limit.period == UsagePeriod.DAY else "a month"
        # "Assisted CV replies" reads as "assisted CV replies" mid-sentence;
        # "CV improvement drafts" keeps its capitals.
        label = (
            limit.label if limit.label[1:2].isupper() else limit.label[0].lower() + limit.label[1:]
        )
        lines.append(f"Unlimited {label}" if allowed is None else f"{allowed} {label} {per}")
    return lines


def _columns(owner: Any) -> tuple[CandidateProfile | None, Company | None]:
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import Company

    if isinstance(owner, Company):
        return None, owner
    if isinstance(owner, CandidateProfile):
        return owner, None
    return getattr(owner, "candidate_profile", None), None
