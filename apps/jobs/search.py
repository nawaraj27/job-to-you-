"""
Job search: full text, trigram, filters, facets, stable ranking (SRS §5.7).

Three passes, in this order, because each covers what the previous one misses:

  1. **Full text** over the generated `search_vector` — fast, indexed, and
     what actually answers "python developer kathmandu".
  2. **Trigram similarity** on the title — catches the typo full text drops,
     and is only consulted when full text found little, because it is the
     expensive one.
  3. **Filters**, always applied, never ranked: a salary floor is a boundary,
     not a preference, and a search that returns jobs below it has ignored the
     person.

**Ranking is stable.** Every ordering ends with `-published_at, -id`, because
two jobs with identical relevance must not swap places between page one and
page two — the symptom is a job that never appears on either.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib.postgres.search import SearchQuery, SearchRank, TrigramSimilarity
from django.db import models
from django.db.models import Count, F, QuerySet, Value
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _

from apps.companies.models import VerificationState
from apps.taxonomy.services import skills as skill_resolver

from .models import (
    EmploymentType,
    ExperienceLevel,
    Job,
    RemoteScope,
    Workplace,
)

#: Below this, a full-text hit is noise. Tuned against the seed corpus.
MIN_RANK = 0.01

#: Only consulted when full text is thin, and only above this similarity.
TRIGRAM_THRESHOLD = 0.3
TRIGRAM_FALLBACK_BELOW = 5

PAGE_SIZE = 20


class CompanyVerification(models.TextChoices):
    """The job list's company filter (docs/Job-Search.md).

    Read from `Company.verification_state`, which only staff decisions write:
    nothing in a request can make a company verified. The default, no value,
    is every company: an unverified employer is not hidden, only labelled.
    """

    VERIFIED = "verified", _("Verified companies")
    UNVERIFIED = "unverified", _("Not yet verified")


SORTS = {
    "relevance": "relevance",
    "newest": "newest",
    "salary": "salary",
}


@dataclass(frozen=True)
class SearchQueryInput:
    """The filters a search runs with.

    A dataclass rather than loose kwargs because this is what a `SavedSearch`
    stores: the alert that runs tomorrow must mean exactly what the search
    meant today, and a dict of whatever the querystring happened to contain
    does not survive a screen redesign.
    """

    text: str = ""
    location_id: int | None = None
    workplace: str = ""
    remote_scope: str = ""
    employment_type: str = ""
    experience_level: str = ""
    category_id: int | None = None
    salary_min: Decimal | None = None
    skills: tuple[str, ...] = ()
    company_id: int | None = None
    verification: str = ""
    sort: str = "relevance"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> SearchQueryInput:
        """Build from a querystring or a stored saved search. Never raises.

        A malformed filter is dropped rather than fatal: a search screen that
        500s because somebody edited the URL is worse than one that ignores a
        nonsense value.
        """
        return cls(
            text=str(data.get("q") or data.get("text") or "").strip()[:120],
            location_id=_as_int(data.get("location") or data.get("location_id")),
            workplace=_as_choice(data.get("workplace"), Workplace.values),
            remote_scope=_as_choice(data.get("remote_scope"), RemoteScope.values),
            employment_type=_as_choice(data.get("employment_type"), EmploymentType.values),
            experience_level=_as_choice(data.get("experience_level"), ExperienceLevel.values),
            category_id=_as_int(data.get("category") or data.get("category_id")),
            salary_min=_as_decimal(data.get("salary_min")),
            skills=tuple(
                str(item).strip()[:60] for item in (data.get("skills") or []) if str(item).strip()
            )[:6],
            company_id=_as_int(data.get("company") or data.get("company_id")),
            verification=_as_choice(data.get("verification"), CompanyVerification.values),
            sort=SORTS.get(str(data.get("sort") or "relevance"), "relevance"),
        )

    def as_mapping(self) -> dict[str, Any]:
        """What a `SavedSearch` stores. Only what was actually chosen."""
        data: dict[str, Any] = {}
        if self.text:
            data["q"] = self.text
        for name in (
            "location_id",
            "workplace",
            "remote_scope",
            "employment_type",
            "experience_level",
            "category_id",
            "company_id",
            "verification",
        ):
            value = getattr(self, name)
            if value:
                data[name] = value
        if self.salary_min is not None:
            data["salary_min"] = str(self.salary_min)
        if self.skills:
            data["skills"] = list(self.skills)
        if self.sort != "relevance":
            data["sort"] = self.sort
        return data

    @property
    def is_empty(self) -> bool:
        return not self.as_mapping()


@dataclass
class SearchResult:
    jobs: QuerySet[Job]
    total: int
    facets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    used_trigram: bool = False


def search(query: SearchQueryInput, *, base: QuerySet[Job] | None = None) -> SearchResult:
    """Run a search over published jobs."""
    queryset: QuerySet[Job] = base if base is not None else Job.objects.published()
    queryset = _apply_filters(queryset, query)

    used_trigram = False
    if query.text:
        matched = _full_text(queryset, query.text)
        if matched.count() < TRIGRAM_FALLBACK_BELOW:
            fuzzy = _trigram(queryset, query.text)
            if fuzzy.exists():
                matched = fuzzy
                used_trigram = True
        queryset = matched
    else:
        queryset = queryset.annotate(relevance=Value(0.0))

    ordered = _order(queryset, query.sort)
    return SearchResult(
        # `skills` is prefetched, not joined: every result row renders its skill
        # chips, and without this the page runs one query per posting — invisible
        # with a dozen postings on a laptop and the first thing to fall over with
        # a full page of results on a shared database (P19 audit).
        jobs=ordered.select_related("company", "location", "category").prefetch_related(
            "skills__skill"
        ),
        total=ordered.count(),
        facets={
            **facets(_apply_filters(base if base is not None else Job.objects.published(), query)),
            # A single choice is counted without itself, so "Not yet verified"
            # stays on offer while "Verified companies" is chosen.
            "verification": _verification_facet(
                _apply_filters(
                    base if base is not None else Job.objects.published(),
                    replace(query, verification=""),
                )
            ),
        },
        used_trigram=used_trigram,
    )


def facets(queryset: QuerySet[Job]) -> dict[str, list[dict[str, Any]]]:
    """Counts per filter value, from the same rows the search returned.

    Computed over the filtered set rather than over everything, so a facet
    never offers a choice that returns nothing.
    """
    return {
        "workplace": _facet(queryset, "workplace", Workplace),
        "employment_type": _facet(queryset, "employment_type", EmploymentType),
        "experience_level": _facet(queryset, "experience_level", ExperienceLevel),
        "remote_scope": _facet(queryset, "remote_scope", RemoteScope),
        "verification": _verification_facet(queryset),
    }


# --- internals ------------------------------------------------------------


def _full_text(queryset: QuerySet[Job], text: str) -> QuerySet[Job]:
    search_query = SearchQuery(text, config="english", search_type="websearch")
    return queryset.annotate(relevance=SearchRank(F("search_vector"), search_query)).filter(
        search_vector=search_query, relevance__gte=MIN_RANK
    )


def _trigram(queryset: QuerySet[Job], text: str) -> QuerySet[Job]:
    """The typo pass. Title only — a fuzzy match over a whole description
    matches everything, slowly."""
    return queryset.annotate(relevance=TrigramSimilarity("title", text)).filter(
        relevance__gte=TRIGRAM_THRESHOLD
    )


def _apply_filters(queryset: QuerySet[Job], query: SearchQueryInput) -> QuerySet[Job]:
    if query.location_id:
        queryset = queryset.filter(location_id=query.location_id)
    if query.workplace:
        queryset = queryset.filter(workplace=query.workplace)
    if query.remote_scope:
        queryset = queryset.filter(remote_scope=query.remote_scope)
    if query.employment_type:
        queryset = queryset.filter(employment_type=query.employment_type)
    if query.experience_level:
        queryset = queryset.filter(experience_level=query.experience_level)
    if query.category_id:
        queryset = queryset.filter(category_id=query.category_id)
    if query.company_id:
        queryset = queryset.filter(company_id=query.company_id)
    if query.verification == CompanyVerification.VERIFIED:
        queryset = queryset.filter(company__verification_state=VerificationState.VERIFIED)
    elif query.verification == CompanyVerification.UNVERIFIED:
        queryset = queryset.exclude(company__verification_state=VerificationState.VERIFIED)

    if query.salary_min is not None:
        # Compared on the annualised base, which is the only figure two
        # currencies can be compared in (ADR-A39). A posting with no base
        # figure is excluded rather than assumed to qualify.
        queryset = queryset.filter(salary_max_base__gte=query.salary_min)

    for term in query.skills:
        match = skill_resolver.resolve(term)
        if match is not None:
            queryset = queryset.filter(skills__skill=match.skill)

    return queryset.distinct()


def _order(queryset: QuerySet[Job], sort: str) -> QuerySet[Job]:
    """Order, always ending in a tiebreak.

    Without the final `-id`, two equally ranked rows can swap between pages and
    a job appears on neither.
    """
    if sort == "newest":
        return queryset.order_by("-published_at", "-id")

    if sort == "salary":
        return queryset.order_by(
            Coalesce("salary_max_base", "salary_min_base").desc(nulls_last=True),
            "-published_at",
            "-id",
        )
    # `relevance` is an annotation, not a column; the type checker cannot see
    # that every path into here has annotated it, but every path has.
    return queryset.order_by("-relevance", "-published_at", "-id")  # type: ignore[misc]


def _verification_facet(queryset: QuerySet[Job]) -> list[dict[str, Any]]:
    verified = queryset.filter(company__verification_state=VerificationState.VERIFIED)
    counts = {
        CompanyVerification.VERIFIED: verified.values("id").distinct().count(),
        CompanyVerification.UNVERIFIED: queryset.exclude(
            company__verification_state=VerificationState.VERIFIED
        )
        .values("id")
        .distinct()
        .count(),
    }
    labels = dict(CompanyVerification.choices)
    return [
        {"value": value, "label": labels[value], "count": count}
        for value, count in counts.items()
        if count
    ]


def _facet(queryset: QuerySet[Job], field_name: str, choices) -> list[dict[str, Any]]:
    labels = dict(choices.choices)
    rows = queryset.values(field_name).annotate(count=Count("id", distinct=True)).order_by("-count")
    return [
        {
            "value": row[field_name],
            "label": labels.get(row[field_name], row[field_name]),
            "count": row["count"],
        }
        for row in rows
        if row["count"]
    ]


def _as_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _as_decimal(value: Any) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount >= 0 else None


def _as_choice(value: Any, allowed) -> str:
    text = str(value or "").strip()
    return text if text in allowed else ""


def matches_query(job: Job, query: SearchQueryInput) -> bool:
    """Does one job satisfy a saved search? Used by the alert batch.

    Runs the same filter chain against a single row rather than reimplementing
    it, because two copies of "what this search means" is how an alert starts
    disagreeing with the screen that created it.
    """
    return search(query, base=Job.objects.published().filter(pk=job.pk)).total > 0
