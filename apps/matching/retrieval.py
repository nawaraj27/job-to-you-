"""
Finding the candidate set before scoring it (ADR-A24, SRS §10.2).

An HNSW index cannot honour arbitrary `WHERE` clauses. Filtering *after* an
approximate search loses recall and can return nothing from a healthy corpus —
a candidate in Pokhara with a narrow salary band is exactly the case that
breaks, and it fails silently, as an empty feed that reads like "there are no
jobs for you". So the strategy is chosen from the size of the filtered set:

    small   (< SMALL_SET)   exact distance over the filtered set — no ANN
    medium  (< MEDIUM_SET)  filtered scan, ordered by distance in the database
    large                   ANN over the vector index, then filter, then top up

**All three are correct; they differ in cost.** The small path is both faster
and more accurate than ANN at its size, which is why it is not a fallback but
the expected path for this market at launch.

Two things this module deliberately does not do. It does not score — retrieval
decides *which* jobs are worth scoring, and `scoring.py` decides how well they
match, because mixing the two would make the score depend on the corpus. And it
never returns fewer than `FLOOR` results while a softer filter could be relaxed
(the thin-result rule), because an empty state is the moment a candidate leaves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.db.models import Q, QuerySet
from pgvector.django import CosineDistance

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job

logger = logging.getLogger(__name__)

#: Below this many rows, scan the filtered set exactly. Chosen against this
#: market's corpus size rather than a benchmark: a few thousand postings is a
#: sequential scan of a few milliseconds, and ANN's approximation buys nothing.
SMALL_SET = 5_000

#: Above this, the filtered scan stops being cheap and the vector index earns
#: its keep.
MEDIUM_SET = 50_000

#: How many neighbours the ANN path asks for before filtering. Large, because
#: the filters may reject most of them and a second round trip is worse than
#: over-fetching once.
ANN_FETCH = 1_000

#: Never show a candidate fewer than this while a filter could still be
#: relaxed (SRS §10.2, the thin-result floor).
FLOOR = 10

#: Hard ceiling on what is scored in one pass, whatever the strategy. Scoring is
#: cheap per pair and ruinous unbounded.
MAX_CANDIDATES = 400


@dataclass
class Retrieved:
    """What retrieval found, and how honest it is about how it found it."""

    jobs: list[Job] = field(default_factory=list)
    strategy: str = "small"
    #: True when a filter was relaxed to reach the floor. The interface says so;
    #: a widened result presented as an exact one is a lie the candidate can
    #: detect and nobody else can.
    widened: bool = False
    relaxed: list[str] = field(default_factory=list)
    considered: int = 0


def retrieve(
    profile: CandidateProfile,
    *,
    base: QuerySet[Job] | None = None,
    limit: int = MAX_CANDIDATES,
    embedding: list[float] | None = None,
    embedding_model: str = "",
) -> Retrieved:
    """The set of jobs worth scoring for this candidate.

    `embedding` is optional and its absence is ordinary: a platform with no
    embedding provider configured, or a profile not yet embedded, still gets a
    complete feed — filters and the deterministic scorer do all the work, and
    the vector only ever changed the *order* in which candidates were considered
    (ADR-A6). This is the difference between a degraded feature and a broken one.
    """
    from apps.jobs.models import Job

    queryset = base if base is not None else Job.objects.published()
    filtered = _apply_filters(queryset, profile)

    size = filtered.count()
    strategy = _strategy(size, embedding=embedding, embedding_model=embedding_model)

    if strategy == "vector":
        jobs = _by_vector(filtered, embedding, embedding_model, limit=limit)
    elif strategy == "ann":
        jobs = _by_ann(filtered, embedding, embedding_model, limit=limit)
    else:
        jobs = list(_ordered(filtered)[:limit])

    result = Retrieved(jobs=jobs, strategy=strategy, considered=size)

    if len(jobs) < FLOOR:
        _widen(result, queryset, profile, limit=limit)
    return result


# --- filters --------------------------------------------------------------


#: Filters that may be relaxed to reach the floor, softest first (SRS §10.2).
#: Salary before location because a candidate will look at a job paying slightly
#: less in their own city long before they will move for one.
SOFT_FILTERS: tuple[str, ...] = ("salary", "workplace")


def _apply_filters(
    queryset: QuerySet[Job], profile: CandidateProfile, *, relax: frozenset[str] = frozenset()
) -> QuerySet[Job]:
    """The hard filters from SRS §10.2.

    Two kinds live here and the difference matters. **Absolute** filters — a
    blocked company, an already-submitted application — are never relaxed:
    showing a candidate a posting from a company they blocked is a bug wearing a
    feature's clothes. **Soft** filters — salary and workplace — are compatibility
    rather than truth, and `relax` drops them when the result would otherwise be
    empty.
    """
    from apps.applications.models import Application
    from apps.candidates.models import BlockedCompany

    blocked = BlockedCompany.objects.filter(profile=profile).values("company_id")
    applied = Application.objects.filter(candidate=profile).values("job_id")

    queryset = (
        queryset.exclude(company_id__in=blocked)
        .exclude(pk__in=applied)
        .select_related("company", "location", "category")
        .prefetch_related("skills")
    )

    if "salary" not in relax:
        queryset = queryset.filter(_salary_compatible(profile))
    if "workplace" not in relax:
        queryset = queryset.filter(_workplace_compatible(profile))
    return queryset


def _salary_compatible(profile: CandidateProfile) -> Q:
    """Postings whose range could meet what the candidate asked for.

    A posting that publishes no range passes. Most postings in this market do
    not publish one, and filtering them out would remove the majority of the
    corpus to enforce a preference the employer never contradicted.
    """
    wants = profile.salary_min_base
    if wants is None:
        return Q()
    return Q(salary_max_base__isnull=True) | Q(salary_max_base__gte=wants)


def _workplace_compatible(profile: CandidateProfile) -> Q:
    """Postings whose arrangement the candidate said they would accept.

    Silence means yes. A profile that lists nothing under `open_to` has stated
    no constraint, and inventing one would hide the whole corpus from the
    candidates who have filled in the least.
    """
    accepted = {str(item).lower() for item in profile.open_to or []}
    arrangements = {"remote", "hybrid", "on_site"}
    if not accepted or not (accepted & arrangements):
        return Q()

    allowed = accepted & arrangements
    # A remote posting is acceptable to someone who will work on-site too: it
    # asks less of them, not more.
    if "remote" not in allowed and ("hybrid" in allowed or "on_site" in allowed):
        allowed = allowed | {"remote"}
    return Q(workplace__in=sorted(allowed))


def _ordered(queryset: QuerySet[Job]) -> QuerySet[Job]:
    """Deterministic order for the non-vector paths.

    `(-published_at, -id)` and never a bare `-published_at`: two jobs published
    in the same second would otherwise swap places between pages, and a
    candidate would see the same posting twice while never seeing another
    (SRS §10.5).
    """
    return queryset.order_by("-published_at", "-id")


# --- strategies -----------------------------------------------------------


def _strategy(size: int, *, embedding: list[float] | None, embedding_model: str) -> str:
    """Pick the cheapest correct approach for a set of this size."""
    if embedding is None or not embedding_model:
        return "small"
    if size < SMALL_SET:
        # Exact distance over a small filtered set: more accurate than ANN and,
        # at this size, faster too.
        return "vector"
    if size < MEDIUM_SET:
        return "vector"
    return "ann"


def _by_vector(
    queryset: QuerySet[Job], embedding: list[float] | None, model: str, *, limit: int
) -> list[Job]:
    """Order the filtered set by distance, in the database.

    Restricted to vectors from the same embedding model, because cosine distance
    between two models' vectors is noise and nothing errors (ADR-A27). Jobs with
    no vector are appended after the ordered ones rather than dropped: an
    unembedded posting is still a real job.
    """
    embedded: list[Job] = list(
        queryset.filter(embedding__isnull=False, embedding_model=model)
        .annotate(distance=CosineDistance("embedding", embedding))
        .order_by("distance", "-published_at", "-id")[:limit]
    )
    if len(embedded) >= limit:
        return embedded

    seen = {job.pk for job in embedded}
    remainder = _ordered(queryset.exclude(pk__in=seen))[: limit - len(embedded)]
    return embedded + list(remainder)


def _by_ann(
    queryset: QuerySet[Job], embedding: list[float] | None, model: str, *, limit: int
) -> list[Job]:
    """Approximate search over the index, then filter, then top up if thin.

    The order matters and is the whole of ADR-A24: the ANN query is deliberately
    unfiltered so the index can be used, and the filters are applied to the
    neighbours it returned. Topping up covers the case where the filters reject
    most of them.
    """
    from apps.jobs.models import Job

    neighbours = list(
        Job.objects.published()
        .filter(embedding__isnull=False, embedding_model=model)
        .annotate(distance=CosineDistance("embedding", embedding))
        .order_by("distance")
        .values_list("pk", flat=True)[:ANN_FETCH]
    )

    survivors = list(queryset.filter(pk__in=neighbours))
    rank = {pk: index for index, pk in enumerate(neighbours)}
    survivors.sort(key=lambda job: rank.get(job.pk, len(neighbours)))
    survivors = survivors[:limit]

    if len(survivors) >= FLOOR:
        return survivors

    seen = {job.pk for job in survivors}
    topped = _ordered(queryset.exclude(pk__in=seen))[: limit - len(survivors)]
    return survivors + list(topped)


# --- the thin-result floor ------------------------------------------------


def _widen(
    result: Retrieved, queryset: QuerySet[Job], profile: CandidateProfile, *, limit: int
) -> None:
    """Relax soft filters, one at a time, until the floor is reached.

    One at a time and softest first, so the results stay as close to what the
    candidate asked for as the corpus allows — dropping every filter at once
    would reach ten results faster and make them worth less.

    Whatever happens, `widened` and `relaxed` record it. An empty state reads to
    a candidate as "there is nothing for you", which is both untrue and the
    moment they leave; a *widened* state presented as an exact one is a lie they
    can detect and nobody else can.
    """
    relaxed: set[str] = set()

    for name in SOFT_FILTERS:
        if len(result.jobs) >= FLOOR:
            break

        relaxed.add(name)
        seen = {job.pk for job in result.jobs}
        wider = _apply_filters(queryset, profile, relax=frozenset(relaxed)).exclude(pk__in=seen)

        extra = list(_ordered(wider)[: limit - len(result.jobs)])
        if not extra:
            continue

        result.jobs.extend(extra)
        result.widened = True
        result.relaxed = sorted(relaxed)
        logger.info(
            "widened matches for profile %s by relaxing %s, now %s results",
            profile.pk,
            sorted(relaxed),
            len(result.jobs),
        )


def text_for(obj: Any) -> str:
    """The text a job or a profile is embedded from.

    One function so both sides are embedded the same way. What goes in is what
    the posting or the profile *says* — never a name, never contact details:
    those carry nothing about fit, and an embedding is the one place personal
    data would leave the platform without anybody reading it (SRS §9.3.7).
    """
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job

    if isinstance(obj, Job):
        category = obj.category.name if obj.category is not None else ""
        parts = [obj.title, category, obj.description]
        parts += [row.skill.name for row in obj.skills.all()]
        return "\n".join(part for part in parts if part).strip()

    if isinstance(obj, CandidateProfile):
        parts = [obj.headline, obj.summary]
        parts += [row.skill.name for row in obj.skills.all()]
        parts += [row.title for row in obj.experiences.all()[:10]]
        return "\n".join(part for part in parts if part).strip()

    raise TypeError(f"{type(obj).__name__} cannot be embedded.")
