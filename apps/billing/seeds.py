"""
The plans the platform launches with.

Data rather than migration content, for the same reason the resume templates
are: commercial terms are edited by staff, and a migration that inserts rows
makes every later price change look like schema drift.

**These numbers are the pricing page's numbers.** That is the whole point —
INV-6 says nothing is advertised before it works, and the inverse holds too: a
page that advertises a limit the code does not enforce is the same defect
pointing the other way. The page said one analysis a month on the free plan, so
the free plan meters one analysis a month.

Two consequences worth stating plainly rather than discovering later:

  * **A free candidate cannot download their CV.** The pricing FAQ says paying
    unlocks "the export formats", so `export_formats` is empty on free. The
    render that `submit_application` freezes is *not* an export — it is the
    platform keeping its own evidentiary record (ADR-A8) — and is unaffected.
  * **There is one company plan, and it gates nothing a company would notice.**
    The page offers employers "talk to us" and lists no limits, so none are
    invented here. The plan exists because every account must have one, and it
    carries the AI budget that P11 already enforced.
"""

from __future__ import annotations

from decimal import Decimal

from .models import BillingInterval, PlanAudience

#: Prices are in Nepali rupees: the wallets the platform takes payment
#: through (eSewa, Khalti) settle in NPR only, and a price shown in one
#: currency and charged in another is a price nobody can check. Pro was USD 12;
#: at the stored USD/NPR rate of 141.5 that is NPR 1,698, rounded to NPR 1,700
#: (docs/billing/Pricing-and-Quotas.md). Change it here and run `seed_plans`.
PRO_PRICE_NPR = Decimal("1700")

#: (code, name, audience, price, currency, interval, is_quoted, is_default,
#:  order, summary, limits)
PLANS: list[tuple] = [
    (
        "free",
        "Free",
        PlanAudience.CANDIDATE,
        Decimal("0"),
        "NPR",
        BillingInterval.MONTH,
        False,
        True,
        0,
        "Enough to see where your CV stands and start improving it.",
        {
            "resume_analyses": 1,
            # AI-backed allowances (ADR-A59, docs/billing/Pricing-and-Quotas.md).
            # Sized to let somebody try each feature properly on the local
            # model without one account occupying the AI worker all day.
            "assistant_replies": 20,  # a day
            "cv_improvements": 5,  # a day
            "interview_practice": 2,  # a month
            "cover_letters": 2,  # a month
            "pro_templates": False,
            "export_formats": [],
            # The ceiling P11 has always applied by default. Named here so the
            # plan is the single answer to "what is my AI budget" rather than
            # one answer in code and another in the database.
            "ai_cost_monthly": "2.00",
        },
    ),
    (
        "pro",
        "Pro",
        PlanAudience.CANDIDATE,
        PRO_PRICE_NPR,
        "NPR",
        BillingInterval.MONTH,
        False,
        False,
        1,
        "For an active job search, when every application counts.",
        {
            "resume_analyses": None,  # Unlimited.
            # Generous, but still a number: a paid account is a person, and a
            # fair-use ceiling stops a script from occupying the AI worker.
            "assistant_replies": 300,  # a day
            "cv_improvements": 100,  # a day
            "interview_practice": 40,  # a month
            "cover_letters": 50,  # a month
            "pro_templates": True,
            "export_formats": ["pdf", "docx"],
            "ai_cost_monthly": "20.00",
        },
    ),
    (
        "employer",
        "Employer",
        PlanAudience.COMPANY,
        Decimal("0"),
        "NPR",
        BillingInterval.MONTH,
        True,  # Priced by agreement: "talk to us", so no checkout is offered.
        True,
        0,
        "Post roles, screen candidates, run structured interviews.",
        {
            "active_jobs": None,
            "candidate_search": True,
            "pro_templates": True,
            "export_formats": ["pdf", "docx"],
            "ai_cost_monthly": "50.00",
        },
    ),
]
