"""
Talent search screens (FR-9.1 to FR-9.4).

Three screens and one download, all behind `candidate.search` — except the
download, which needs `candidate.export`, because taking a list of people off
the platform is a different act from looking at them one at a time.

**Two things these views deliberately do not do.** They do not filter by
visibility: `services.talent` owns that, and a view that also scoped would be a
second place for the rule to live. And they do not write the access log: the
service writes it in the same transaction as the grant, so there is no path
through a view that reads a candidate without recording it.
"""

from __future__ import annotations

import csv
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View

from apps.candidates.models import Seeking, ViewSource
from apps.candidates.search import PAGE_SIZE, TalentQuery
from apps.candidates.services import talent as talent_service
from apps.common import restrictions
from apps.common.errors import InvalidState, NotFound
from apps.common.pagination import page_of
from apps.companies import policies as company_policies
from apps.companies.permissions import Grant
from apps.taxonomy import selectors as taxonomy_selectors


@method_decorator(login_required, name="dispatch")
class TalentSearchView(View):
    """`/employer/<slug>/talent/` — find candidates who allow it."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        # The empty form was open to every member and refused only on submit,
        # so a Viewer who typed a query got a 403 for using the page they had
        # just been shown.
        membership = company_policies.require_section(request.membership, "talent")
        query = TalentQuery.from_mapping(request.GET)

        result = None
        error = None
        if not query.is_empty:
            # `InvalidState` only — a query this screen cannot run is an inline
            # message. `NotAllowed` deliberately propagates: somebody without
            # `candidate.search` gets a 403 from the error middleware, not a
            # 200 page with an apology on it, because "you may not do this" and
            # "you asked for something invalid" are different answers and the
            # first must not be recoverable by editing the form.
            try:
                result = talent_service.search(
                    membership=membership, actor=request.user, query=query
                )
            except InvalidState as exc:
                error = exc.message

        rows = [_row(profile) for profile in (result.profiles if result else [])]
        page = page_of(rows, number=request.GET.get("page"), per_page=PAGE_SIZE)

        context = {
            "company": request.company,
            "membership": membership,
            "query": query,
            "page": page,
            "total": result.total if result else 0,
            "searched": not query.is_empty,
            "error": error,
            "seeking_choices": Seeking.choices,
            "locations": taxonomy_selectors.locations_in("NP"),
            "can_export": Grant.CANDIDATE_EXPORT in membership.grants,
            "export_limit": talent_service.EXPORT_LIMIT,
        }

        if request.headers.get("HX-Request"):
            return render(request, "employer/talent/_results.html", context)
        return render(request, "employer/talent/search.html", context)


@method_decorator(login_required, name="dispatch")
class TalentProfileView(View):
    """`/employer/<slug>/talent/<public_id>/` — one candidate.

    Opening this page is the act that creates the grant and writes the view.
    That is why it is a GET that changes state, which is otherwise a thing to
    avoid: the read *is* the event, and there is no honest way to record a
    profile read without the profile being read.
    """

    def get(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)

        try:
            profile = talent_service.view_profile(
                membership=membership,
                actor=request.user,
                public_id=public_id,
                source=ViewSource.SEARCH,
            )
        except NotFound as exc:
            raise Http404(exc.message) from exc

        return render(
            request,
            "employer/talent/profile.html",
            {
                "company": request.company,
                "membership": membership,
                "candidate": profile,
                "skills": profile.skills.select_related("skill").all(),
                "experiences": profile.experiences.all()[:10],
                "education": profile.education.all()[:5],
                # ADR-A34: contact details are revealed at a stage that requires
                # an application, and a search result is not that stage.
                "back_url": reverse("employer:talent", args=[company_slug]),
            },
        )


@method_decorator(login_required, name="dispatch")
class TalentExportView(View):
    """`/employer/<slug>/talent/export/` — a chosen shortlist as CSV (FR-9.4).

    A POST, because it creates records: every exported row is a logged view, and
    a GET that a refresh repeats would write them twice and tell the candidate
    they were read twice.
    """

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        back = reverse("employer:talent", args=[company_slug])

        # Same split as the search screen: an empty or oversized selection is a
        # message and a redirect; a missing `candidate.export` grant is a 403.
        try:
            rows = talent_service.export(
                membership=membership,
                actor=request.user,
                public_ids=request.POST.getlist("candidate"),
            )
        except InvalidState as exc:
            restrictions.flash(request, exc)
            return redirect(back)

        return _csv_response(rows, company_slug=company_slug)


def _csv_response(rows: list[dict[str, Any]], *, company_slug: str) -> HttpResponse:
    """The export, as a file.

    Columns come from the service's allow-list rather than from the first row,
    so an empty export still produces a valid file with a header — and so a
    field added to the row shape cannot appear in the download without being
    added to the allow-list first.
    """
    stamp = timezone.localdate().isoformat()
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="{company_slug}-candidates-{stamp}.csv"'
    )

    writer = csv.DictWriter(response, fieldnames=list(talent_service.EXPORT_FIELDS))
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(value) for key, value in row.items()})
    return response


#: Characters a spreadsheet reads as the start of a formula.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> Any:
    """A value a spreadsheet shows rather than runs (CSV injection).

    Headlines and skills are typed by candidates. A headline of
    `=HYPERLINK("http://...")` went into the recruiter's spreadsheet as a live
    formula. A leading apostrophe keeps it text.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _row(profile) -> dict[str, Any]:
    """One candidate as a result row.

    A headline, a place, a length of career and some skills — deliberately not a
    name and never contact details. A search result is a reason to open a
    profile, not a substitute for doing so.
    """
    return {
        "public_id": str(profile.public_id),
        "headline": profile.headline or "Candidate",
        "location": profile.location.name if profile.location_id else "",
        "experience_months": profile.experience_months,
        "experience_label": _years(profile.experience_months),
        "seeking": profile.get_seeking_display() if profile.seeking else "",
        "skills": [row.skill.name for row in profile.skills.all()[:6]],
        "relevance": round(getattr(profile, "relevance", 0.0) * 100),
    }


def _years(months: int) -> str:
    if not months:
        return "Not stated"
    if months < 12:
        return f"{months} months"
    years = months // 12
    return f"{years} year{'s' if years != 1 else ''}"
