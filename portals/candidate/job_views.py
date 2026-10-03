"""
Candidate job screens, on real postings (SRS §5.7).

The namespace stays `jobs` because the navigation, the user menu and the 404
page all reference `jobs:list`; renaming routes is not what this phase is for.
What changed is underneath — these read `Job` rows through selectors and the
search service instead of session-backed samples.

Anonymous browsing is deliberate. A job board that demands an account before it
shows a salary has lost the reader. Saving a job needs one, and only then.

Applications are P8's. Nothing here creates one, and no screen here pretends to.
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.candidates import selectors as candidate_selectors
from apps.candidates.models import AlertFrequency, CandidateProfile
from apps.candidates.services import profile as profile_service
from apps.common import ratelimit, restrictions
from apps.common.errors import DomainError, NotFound
from apps.common.pagination import page_of
from apps.companies import selectors as company_selectors
from apps.core.features import is_enabled
from apps.jobs import policies, selectors
from apps.jobs.models import (
    EmploymentType,
    ExperienceLevel,
    Job,
    RemoteScope,
    Workplace,
)
from apps.jobs.search import PAGE_SIZE, CompanyVerification, SearchQueryInput, search
from apps.jobs.services import discovery
from apps.matching.services import exposure, recommendations
from apps.taxonomy import selectors as taxonomy_selectors

from . import job_context as shape

logger = logging.getLogger(__name__)

#: The filter groups the rail draws, and the only query keys it may set. A
#: facet the search service does not understand is a checkbox that silently
#: does nothing, so the two lists are defined together.
FACETS = (
    # Company verification first (docs/Job-Search.md): all companies by
    # default, verified or not-yet-verified on request.
    ("verification", "Companies", CompanyVerification),
    ("workplace", "Work type", Workplace),
    ("employment_type", "Job type", EmploymentType),
    ("experience_level", "Experience", ExperienceLevel),
    ("remote_scope", "Remote from", RemoteScope),
)

#: What the "no filter" choice says, where "Any" would be unclear.
ANY_LABELS = {"verification": "All companies"}

SORTS = (
    ("relevance", "Most relevant"),
    ("newest", "Newest first"),
    ("salary", "Salary: high to low"),
)


def _profile(request: HttpRequest):
    """The viewer's candidate profile, or None for an anonymous reader."""
    if not request.user.is_authenticated:
        return None
    return candidate_selectors.profile_for(request.user)


def _viewer_key(request: HttpRequest) -> str:
    """A stable-enough identity for view deduplication.

    The session key for a signed-out reader, the user id for a signed-in one.
    Never an IP: the count needs "was this the same person again", not who they
    were.
    """
    if request.user.is_authenticated:
        return f"user:{request.user.pk}"
    if not request.session.session_key:
        request.session.save()
    return f"session:{request.session.session_key}"


def _query_from(data: Any) -> SearchQueryInput:
    return SearchQueryInput.from_mapping({**data.dict(), "skills": data.getlist("skills")})


def _active_filters(query: SearchQueryInput, data: Any) -> list[dict[str, str]]:
    """What is narrowing the results, in words, each removable on its own.

    "Why are there only two results" is the most common dead end on a job
    board, and it is nearly always a filter the reader has forgotten. The
    answer therefore belongs at the top of the results, not inside the control
    that set it.
    """
    labels = {name: (label, dict(choices.choices)) for name, label, choices in FACETS}
    active: list[dict[str, str]] = []

    for name, (label, values) in labels.items():
        chosen = getattr(query, name)
        if chosen:
            active.append(
                {
                    "label": label,
                    "value": values.get(chosen, chosen),
                    "remove_url": _without(data, name),
                }
            )

    if query.salary_min is not None:
        active.append(
            {
                "label": "Salary",
                "value": f"{query.salary_min:,.0f}+",
                "remove_url": _without(data, "salary_min"),
            }
        )
    for term in query.skills:
        active.append(
            {"label": "Skill", "value": term, "remove_url": _without(data, "skills", term)}
        )
    return active


def _without(data: Any, key: str, value: str | None = None) -> str:
    """The same search minus one filter."""
    params = data.copy()
    if value is None:
        params.pop(key, None)
    else:
        remaining = [item for item in params.getlist(key) if item != value]
        params.setlist(key, remaining)
    params.pop("page", None)
    encoded = params.urlencode()
    return f"{reverse('jobs:list')}?{encoded}" if encoded else reverse("jobs:list")


def _facet_groups(result_facets: dict[str, list[dict]], query: SearchQueryInput) -> list[dict]:
    """Filter groups with live counts from the rows the search returned.

    Counted rather than listed, so the rail never offers a choice that leads to
    an empty page.
    """
    groups = []
    for name, label, _choices in FACETS:
        options = result_facets.get(name) or []
        if not options:
            continue
        groups.append(
            {
                "field": name,
                "label": label,
                "options": options,
                "chosen": getattr(query, name),
                "any_label": ANY_LABELS.get(name, "Any"),
            }
        )
    return groups


class JobListView(View):
    """`/jobs/` — search and browse. Open to everyone."""

    def get(self, request: HttpRequest) -> HttpResponse:
        # §15.4 lists search among the rate-limited endpoints, and this one is
        # public: full-text plus trigram over every posting, reachable with
        # `curl` and no account. The rule **fails open** (ADR-A41) — taking job
        # search down because a cache is missing is self-inflicted damage —
        # and is keyed per client rather than per user, since most of the
        # traffic worth limiting has no user.
        ratelimit.check(ratelimit.JOB_SEARCH_PER_CLIENT, _client_key(request))

        query = _query_from(request.GET)
        result = search(query)
        page = page_of(result.jobs, number=request.GET.get("page"), per_page=PAGE_SIZE)
        page["items"] = [shape.job_row(job) for job in page["items"]]
        profile = _profile(request)

        context = {
            "page_title": "Browse jobs",
            "query": query.text,
            "search": query,
            "sort": query.sort,
            "sorts": SORTS,
            "page": page,
            "total": result.total,
            "facets": _facet_groups(result.facets, query),
            "used_trigram": result.used_trigram,
            "active_filters": _active_filters(query, request.GET),
            "clear_filters_url": reverse("jobs:list"),
            "saved_slugs": selectors.saved_job_slugs(profile),
            "saved_searches": selectors.saved_searches_for(profile) if profile else [],
            "can_save": profile is not None,
            "frequencies": AlertFrequency.choices,
            "categories": taxonomy_selectors.job_categories(),
            "locations": taxonomy_selectors.locations_in("NP"),
        }

        # HTMX asks for the results region only; the filter rail keeps its open
        # groups and the page keeps its scroll position.
        if request.headers.get("HX-Request"):
            return render(request, "jobs/_results.html", context)
        return render(request, "jobs/list.html", context)


class JobDetailView(View):
    """`/jobs/<slug>/` — one posting.

    A draft, closed, expired or archived job is a 404 here, not a 403: a URL
    that behaves differently for an unpublished posting tells a stranger the
    company has one.
    """

    def get(self, request: HttpRequest, slug: str) -> HttpResponse:
        try:
            job = policies.require_visible(selectors.published_job(slug))
        except NotFound as exc:
            raise Http404(exc.message) from exc

        discovery.record_view(
            job=job,
            viewer_key=_viewer_key(request),
            user=request.user if request.user.is_authenticated else None,
            referrer=request.META.get("HTTP_REFERER", "")[:200],
        )

        profile = _profile(request)
        return render(
            request,
            "jobs/detail.html",
            {
                "job": _detail_with_match(job, profile),
                # The model instance, for the cover-letter form. The template
                # asked for `raw_job` and nothing provided it, so "Draft a
                # letter" posted an empty job and every draft was a 404.
                "raw_job": job,
                "similar": [shape.job_row(other) for other in selectors.similar_to(job)],
                "saved_slugs": selectors.saved_job_slugs(profile),
                "can_save": profile is not None,
            },
        )


def _detail_with_match(job: Job, profile: CandidateProfile | None) -> dict[str, Any]:
    """One posting, scored against this candidate if there is one.

    `jobs/detail.html` has always had the branch — score and reasoning if
    `job.match` is set, "upload your CV and this role gets a score" if it is
    not — and nothing ever set it. So the candidate whose CV already scores this
    role at 90% on `/jobs/matched/` opened the posting and was invited to upload
    a CV, on the screen where the score is worth the most.

    One pair, scored on the way past. The feed's cache is for fifty rows; this
    is one, and it must reflect a profile edited thirty seconds ago.
    """
    detail = shape.job_detail(job)
    if profile is None or not is_enabled("job_matching"):
        return detail

    try:
        match = recommendations.score_pair(profile, job)
    except Exception:  # noqa: BLE001 - a score is never worth the page
        logger.exception("could not score this job for this profile", extra={"job": job.pk})
        return detail

    names = _skill_names_for(match)
    return {**detail, "match": shape.match_block(match, skill_names=names)}


def _skill_names_for(match: Any) -> dict[int, str]:
    wanted = (
        set(match.matched_skill_ids) | set(match.partial_skill_ids) | set(match.missing_skill_ids)
    )
    return taxonomy_selectors.skill_names(wanted) if wanted else {}


class CompanyJobsView(View):
    """`/jobs/company/<slug>/` — one company's public page."""

    def get(self, request: HttpRequest, slug: str) -> HttpResponse:
        company = company_selectors.public_company(slug)
        if company is None:
            raise Http404("No such company.")

        profile = _profile(request)
        jobs = selectors.board_for_company(company)
        return render(
            request,
            "jobs/company.html",
            {
                "page_title": company.name,
                "company": shape.company_card(company),
                "jobs": [shape.job_row(job) for job in jobs],
                "job_count": len(jobs),
                "saved_slugs": selectors.saved_job_slugs(profile),
                "can_save": profile is not None,
                **_follow_context(request, company),
                # News and upcoming events for followers and visitors (FOL-03).
                "company_updates": _updates_for(company),
            },
        )


def _updates_for(company) -> dict[str, Any]:
    from apps.companies.services import updates

    return updates.live_for(company)


def _follow_context(request: HttpRequest, company) -> dict[str, Any]:
    from apps.companies.services import follows

    return {
        "follow_slug": company.slug,
        "company_name": company.name,
        "is_following": follows.is_following(user=request.user, company=company),
    }


@method_decorator(login_required, name="dispatch")
class CompanyFollowView(View):
    """`/jobs/company/<slug>/follow/` — follow or unfollow (FOL-01).

    POST only; the direction is named rather than toggled, so a double
    submission cannot undo itself. A refusal (a blocked company, a company
    that is not public) is a `DomainError` and reaches the reader through the
    shared restriction handling.
    """

    def post(self, request: HttpRequest, slug: str) -> HttpResponse:
        from apps.companies.services import follows

        company = company_selectors.public_company(slug)
        if company is None:
            raise Http404("No such company.")

        if request.POST.get("action") == "unfollow":
            follows.unfollow(user=request.user, company=company)
            announce = f"You no longer follow {company.name}"
        else:
            follows.follow(user=request.user, company=company)
            announce = (
                f"You follow {company.name}. We will tell you when it posts a job, "
                "is verified, or shares news or an event."
            )

        if request.headers.get("HX-Request"):
            response = render(
                request,
                "jobs/_follow_control.html",
                {**_follow_context(request, company), "follow_message": announce},
            )
            return response
        messages.success(request, announce)
        return redirect("jobs:company", slug=company.slug)


@method_decorator(login_required, name="dispatch")
class SavedJobsView(View):
    """`/jobs/saved/` — the shortlist, and the searches that feed it."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = _require_profile(request)
        saved = [shape.saved_row(row) for row in selectors.saved_jobs_for(profile)]
        return render(
            request,
            "jobs/saved.html",
            {
                "page_title": "Saved jobs",
                "saved": saved,
                "saved_slugs": {row["slug"] for row in saved},
                "searches": selectors.saved_searches_for(profile),
                "matches": [
                    {"job": shape.job_row(match.job), "search": match.saved_search}
                    for match in selectors.alert_matches_for(profile, limit=20)
                ],
                "frequencies": AlertFrequency.choices,
                "can_save": True,
            },
        )


@method_decorator(login_required, name="dispatch")
class JobActionView(View):
    """`/jobs/act/` — save, unsave, and manage saved searches.

    One endpoint because these are all "the reader changed their mind about a
    job or a search", and four different screens post them over HTMX.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = _require_profile(request)
        command = request.POST.get("command") or request.POST.get("action") or ""

        try:
            return self._run(request, profile, command)
        except DomainError as exc:
            if request.headers.get("HX-Request"):
                # Redraw the control in its true state and say why. Leaving a
                # half-toggled bookmark on screen is how a save that failed
                # gets read as one that worked.
                return self._control(
                    request, self._job(request), saved=False, error=exc.message, status=400
                )
            restrictions.flash(request, exc)
            return redirect(_next_or(request, reverse("jobs:list")))

    def _run(self, request: HttpRequest, profile, command: str) -> HttpResponse:
        if command in {"save", "unsave"}:
            job = self._job(request)
            # One button, so the server decides the direction from the record
            # rather than trusting a field the page rendered minutes ago.
            currently = job.slug in selectors.saved_job_slugs(profile)
            if command == "unsave" or currently:
                discovery.unsave_job(profile=profile, actor=request.user, job=job)
                saved = False
            else:
                discovery.save_job(
                    profile=profile, actor=request.user, job=job, note=request.POST.get("note", "")
                )
                saved = True

            if request.headers.get("HX-Request"):
                return self._control(request, job, saved=saved)
            return redirect(_next_or(request, job.get_absolute_url()))

        if command == "save_search":
            discovery.save_search(
                profile=profile,
                actor=request.user,
                name=request.POST.get("name", ""),
                query=_query_from(request.POST),
                alert_frequency=request.POST.get("alert_frequency", AlertFrequency.OFF),
            )
            messages.success(request, "Search saved.")
            return redirect(_next_or(request, reverse("jobs:saved")))

        if command in {"set_alert", "delete_search"}:
            saved_search = selectors.saved_search_for(
                profile, _as_int(request.POST.get("search_id"))
            )
            if saved_search is None:
                raise NotFound("That saved search is gone.")
            if command == "set_alert":
                discovery.set_alert(
                    saved_search=saved_search,
                    actor=request.user,
                    alert_frequency=request.POST.get("alert_frequency", AlertFrequency.OFF),
                )
                messages.success(request, "Alert updated.")
            else:
                discovery.delete_search(saved_search=saved_search, actor=request.user)
                messages.success(request, "Search deleted.")
            return redirect(reverse("jobs:saved"))

        raise Http404("Unknown command.")

    def _control(
        self, request: HttpRequest, job, *, saved: bool, error: str = "", status: int = 200
    ) -> HttpResponse:
        """Re-render whichever save control posted this."""
        template = (
            "jobs/_save_button.html"
            if request.POST.get("render") == "save_button"
            else "jobs/_control.html"
        )
        return render(
            request,
            template,
            {
                "job": shape.job_row(job),
                "saved_slugs": {job.slug} if saved else set(),
                "error": error,
                "can_save": True,
            },
            status=status,
        )

    def _job(self, request: HttpRequest):
        job = selectors.published_job(request.POST.get("slug") or "")
        if job is None:
            raise Http404("That job is no longer listed.")
        return job


def _require_profile(request: HttpRequest):
    if not request.user.is_authenticated:
        raise Http404("Sign in first.")
    # Created on first use, as every other candidate screen does. These four
    # answered 404 for an account that had not opened the dashboard yet —
    # an employer clicking "Saved jobs" in their own account menu, say.
    return profile_service.get_or_create_profile(user=request.user)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class RecommendedJobsView(View):
    """`/jobs/matched/` — roles scored against this candidate's profile (FR-8.3).

    Computed on read, cached in Redis, and never persisted (ADR-A21). Behind the
    `job_matching` flag, which the navigation has referenced since P7 and which
    P13 is what finally makes true (SRS INV-6).

    Signed in only, and not because the scoring is sensitive: a feed is a
    statement about one person's profile, and there is no such thing as an
    anonymous one.
    """

    @method_decorator(login_required)
    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if not is_enabled("job_matching"):
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = candidate_selectors.profile_for(request.user)
        if profile is None:
            return redirect("dashboard:profile")

        feed = recommendations.feed_for(profile)
        names = _skill_names(feed.items)

        # Counted here rather than in the service: an impression is something a
        # person saw, and the service is also called by the employer ranking and
        # by the snapshot, where nobody saw anything (FR-8.8).
        exposure.record_impressions([item.job.pk for item in feed.items])

        # The company filter, read from each company's verification record
        # (docs/Job-Search.md). Applied after scoring, so the cached feed is
        # one feed whichever view of it is asked for.
        verification = request.GET.get("verification", "")
        if verification not in CompanyVerification.values:
            verification = ""
        items = feed.items
        if verification == CompanyVerification.VERIFIED:
            items = [i for i in items if i.job.company.verification_state == "verified"]
        elif verification == CompanyVerification.UNVERIFIED:
            items = [i for i in items if i.job.company.verification_state != "verified"]

        rows = [shape.with_match(item.job, item.match, skill_names=names) for item in items]
        page = page_of(rows, number=request.GET.get("page"), per_page=PAGE_SIZE)

        context = {
            "page_title": "Matched to your profile",
            "page": page,
            "total": len(rows),
            "feed": feed,
            "saved_slugs": selectors.saved_job_slugs(profile),
            "skills_url": reverse("dashboard:profile"),
            "verification": verification,
            "verification_choices": [("", "All companies"), *CompanyVerification.choices],
        }

        if request.headers.get("HX-Request"):
            return render(request, "jobs/_matched_results.html", context)
        return render(request, "jobs/matched.html", context)


def _skill_names(items: list[Any]) -> dict[int, str]:
    """Names for every skill any of these matches mentions.

    One query for the whole feed. Resolving them per card would be fifty
    queries to render one page, which is the shape of N+1 that survives review
    because each individual line looks harmless.
    """

    wanted: set[int] = set()
    for item in items:
        wanted |= set(item.match.matched_skill_ids)
        wanted |= set(item.match.partial_skill_ids)
        wanted |= set(item.match.missing_skill_ids)

    if not wanted:
        return {}
    return taxonomy_selectors.skill_names(wanted)


def _client_key(request: HttpRequest) -> str:
    """Who to count a search against.

    The signed-in user when there is one, so a shared office address does not
    have one person's browsing count against everybody else's; the client
    address otherwise, which is all an anonymous visitor gives us.
    """
    if request.user.is_authenticated:
        return f"user:{request.user.pk}"
    return f"ip:{ratelimit.client_ip(request)}"


def _next_or(request: HttpRequest, default: str) -> str:
    """Where the form asked to go back to — only if it is on this site.

    `next` is a form field, and a field can hold any address. Passed straight
    to `redirect`, a crafted form sent somebody from a job page to another
    site that looked like this one.
    """
    from django.utils.http import url_has_allowed_host_and_scheme

    target = (request.POST.get("next") or "").strip()
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return default
