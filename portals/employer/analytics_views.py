"""
The employer analytics screen (SRS §16.2).

Everything on this page comes from a rollup. Nothing aggregates raw events and
nothing aggregates applications on the request path — the day a posting goes
viral is the day somebody opens this, and it has to still work.

**The response nudge is on the page, not in an email.** §16.3.3 makes employer
response rate a steering measurement and later a public badge; the first step is
telling the company what their own number is, in a sentence, where they are
already looking.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View

from apps.analytics.services import reports
from apps.companies import policies as company_policies

#: Windows the screen offers. Bounded so a hand-typed query string cannot ask
#: for five years of rollups on one page.
WINDOWS = (7, 30, 90)


@method_decorator(login_required, name="dispatch")
class AnalyticsView(View):
    """`/employer/<slug>/analytics/` — the funnel, the postings, the numbers."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        # SRS §2.3: an Interviewer has no company-wide Jobs or Applications
        # access, and this page is those aggregated. The roles are in
        # `SECTION_ROLES["analytics"]`.
        membership = company_policies.require_section(request.membership, "analytics")
        days = _window(request.GET.get("days"))

        return render(
            request,
            "employer/analytics.html",
            {
                "company": request.company,
                "membership": membership,
                "days": days,
                "windows": WINDOWS,
                "overview": reports.company_overview(request.company, days=days),
                "funnel": reports.company_funnel(request.company),
                "postings": reports.job_performance(request.company, days=days)[:20],
                "time_to_hire": reports.time_to_hire(request.company),
                # The three §16.2 employer measures the screen did not have.
                "sources": reports.source_effectiveness(request.company, days=days),
                "quality": reports.quality_distribution(request.company, days=days),
                "workload": reports.recruiter_workload(request.company, days=days),
            },
        )


def _window(value: str | None) -> int:
    try:
        days = int(value or 30)
    except (TypeError, ValueError):
        return 30
    return days if days in WINDOWS else 30
