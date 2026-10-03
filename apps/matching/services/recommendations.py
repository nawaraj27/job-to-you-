"""
Recommendations, computed on read (ADR-A21, SRS §10.1, FR-8.3).

There is no candidate×job score table, deliberately: it would be the largest and
churniest object in the schema, with write amplification on every publication
and most rows read once or never. For one candidate the whole pipeline runs in
well under a second, so it runs when asked.

**The cache key is the invalidation.** Every staleness class is a key component,
so nothing has to be swept:

    (profile_id, algorithm_version, embedding_model, profile_updated_at, job_generation)

`job_generation` is the one that is easy to leave out and expensive to miss:
without it a newly published job cannot appear in any cached feed until the
candidate happens to edit their profile — the feed would be stale exactly when a
relevant job is posted, which is the one moment the product exists for. The TTL
is a backstop, not the mechanism.

The cache must be correct when empty, and it is: every key is recomputable from
PostgreSQL, so a flush costs latency and never correctness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.core.cache import cache

from apps.common.errors import NotFound

from .. import extract, retrieval, scoring
from ..scoring import Match
from . import exposure
from .embeddings import current_model

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job

logger = logging.getLogger(__name__)

#: A backstop, not the invalidation mechanism (SRS §10.1).
CACHE_TTL_SECONDS = 15 * 60

#: How many recommendations a feed holds. More than a screen, so paging works
#: without recomputing; far fewer than the corpus, because nobody reads 400.
FEED_SIZE = 50

#: Below this many confirmed skills a candidate is matched on the cold-start
#: path and told so (FR-8.7).
COLD_START_SKILLS = 3


@dataclass
class Recommendation:
    """One scored job, ready to render."""

    job: Job
    match: Match

    @property
    def score(self) -> int:
        return self.match.score

    @property
    def band(self) -> str:
        return self.match.band


@dataclass
class Feed:
    """A candidate's recommendations, and how they were produced."""

    items: list[Recommendation] = field(default_factory=list)
    strategy: str = "small"
    widened: bool = False
    relaxed: list[str] = field(default_factory=list)
    provisional: bool = False
    considered: int = 0
    cached: bool = False

    def __len__(self) -> int:
        return len(self.items)


def feed_for(profile: CandidateProfile, *, limit: int = FEED_SIZE, use_cache: bool = True) -> Feed:
    """The candidate's recommended jobs, best first.

    Computed unless an identical computation is already cached — identical
    meaning same profile, same profile state, same scorer, same embedding model
    and same job corpus generation.
    """
    key = cache_key(profile)

    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            hydrated = _hydrate(cached, limit=limit)
            if hydrated is not None:
                return hydrated

    result = _compute(profile, limit=limit)
    cache.set(key, _dehydrate(result), CACHE_TTL_SECONDS)
    return result


def score_pair(profile: CandidateProfile, job: Job) -> Match:
    """Score one candidate against one job (FR-8.1).

    The same scorer the feed and the employer's applicant ranking use — one
    implementation, so a candidate and an employer looking at the same pair see
    the same number (FR-8.4).
    """
    credit = extract.skill_credit_for(profile)
    return scoring.score(
        extract.candidate_features(profile),
        extract.job_features(job),
        credit=credit,
        provisional=_is_cold_start(profile),
    )


def rank_candidates(
    job: Job, profiles: list[CandidateProfile]
) -> list[tuple[CandidateProfile, Match]]:
    """Rank candidates against one job (FR-8.4).

    The job's features are extracted once and the skill graph once per
    candidate, which is the whole difference between ranking a shortlist and
    ranking a shortlist slowly.
    """
    job_vector = extract.job_features(job)
    ranked = [
        (
            profile,
            scoring.score(
                extract.candidate_features(profile),
                job_vector,
                credit=extract.skill_credit_for(profile),
                provisional=_is_cold_start(profile),
            ),
        )
        for profile in profiles
    ]
    ranked.sort(key=lambda pair: (-pair[1].score, pair[0].pk))
    return ranked


def cache_key(profile: CandidateProfile) -> str:
    """Every staleness class, in the key (SRS §10.1)."""
    # Microseconds, not seconds. `auto_now` records them, and truncating to a
    # whole second means an edit and a read inside the same second share a key —
    # the candidate saves a change and the feed does not move, which is exactly
    # the staleness this key construction exists to make impossible.
    stamp = profile.updated_at.timestamp() if profile.updated_at else 0
    return ":".join(
        [
            "match:feed",
            str(profile.pk),
            str(scoring.ALGORITHM_VERSION),
            current_model() or "novec",
            f"{stamp:.6f}",
            str(exposure.job_generation()),
        ]
    )


def invalidate(profile: CandidateProfile) -> None:
    """Drop this candidate's cached feed.

    Rarely needed — the key construction already covers profile edits and new
    postings — but erasure and a manual "recompute" both want it, and a sweep
    that has to guess which keys exist is worse than one explicit delete.
    """
    cache.delete(cache_key(profile))


# --- internals ------------------------------------------------------------


def _compute(profile: CandidateProfile, *, limit: int) -> Feed:
    provisional = _is_cold_start(profile)
    model = current_model()

    found = retrieval.retrieve(
        profile,
        embedding=list(profile.embedding) if profile.embedding is not None else None,
        embedding_model=model if profile.embedding_model == model else "",
    )

    candidate_vector = extract.candidate_features(profile)
    credit = extract.skill_credit_for(profile)

    scored = [
        Recommendation(
            job=job,
            match=scoring.score(
                candidate_vector,
                extract.job_features(job),
                credit=credit,
                provisional=provisional,
            ),
        )
        for job in found.jobs
    ]

    # Deterministic ordering: score first, then the same tiebreaker every other
    # ranked list in the system uses. Without it, two equal scores swap places
    # between requests and a candidate sees one posting twice and another never
    # (SRS §10.5).
    scored.sort(key=lambda item: (-item.match.score, -item.job.pk))

    adjusted = exposure.apply_caps(scored, profile=profile)

    return Feed(
        items=adjusted[:limit],
        strategy=found.strategy,
        widened=found.widened,
        relaxed=found.relaxed,
        provisional=provisional,
        considered=found.considered,
    )


def _is_cold_start(profile: CandidateProfile) -> bool:
    """FR-8.7. Too few confirmed skills to match on skills.

    The result is still produced and still ordered — it is labelled provisional
    so the interface can say what it is and point at the one action that fixes
    it, which is confirming skills.
    """
    return profile.skills.count() < COLD_START_SKILLS


def _dehydrate(feed: Feed) -> dict[str, Any]:
    """What goes into Redis: ids and scores, never model instances.

    Pickled Django models in a cache are a schema migration away from an
    exception nobody can reproduce, and they hold personal data in a store with
    a different retention from the database.
    """
    return {
        "items": [{"job_id": item.job.pk, "match": item.match.as_dict()} for item in feed.items],
        "strategy": feed.strategy,
        "widened": feed.widened,
        "relaxed": feed.relaxed,
        "provisional": feed.provisional,
        "considered": feed.considered,
    }


def _hydrate(payload: dict[str, Any], *, limit: int) -> Feed | None:
    """Rebuild a feed from cached ids, or return None to recompute.

    None rather than a partial feed when a job has since been closed or
    deleted: showing a candidate a posting that has gone is worse than the
    fraction of a second recomputing costs.
    """
    from apps.jobs.models import Job

    rows = payload.get("items", [])[:limit]
    if not rows:
        return Feed(
            items=[],
            strategy=payload.get("strategy", "small"),
            widened=payload.get("widened", False),
            relaxed=payload.get("relaxed", []),
            provisional=payload.get("provisional", False),
            considered=payload.get("considered", 0),
            cached=True,
        )

    ids = [row["job_id"] for row in rows]
    jobs = {
        job.pk: job
        for job in Job.objects.published()
        .filter(pk__in=ids)
        .select_related("company", "location", "category")
    }
    if len(jobs) != len(ids):
        return None

    return Feed(
        items=[
            Recommendation(job=jobs[row["job_id"]], match=_match_from(row["match"])) for row in rows
        ],
        strategy=payload.get("strategy", "small"),
        widened=payload.get("widened", False),
        relaxed=payload.get("relaxed", []),
        provisional=payload.get("provisional", False),
        considered=payload.get("considered", 0),
        cached=True,
    )


def _match_from(payload: dict[str, Any]) -> Match:
    return Match(
        score=payload["score"],
        band=payload["band"],
        algorithm_version=payload["algorithm_version"],
        components=[
            scoring.Component(
                key=component["key"],
                label=component["label"],
                weight=component["weight"],
                points=component["points"],
                note=component["note"],
            )
            for component in payload.get("components", [])
        ],
        matched_skill_ids=payload.get("matched_skill_ids", []),
        partial_skill_ids=payload.get("partial_skill_ids", []),
        missing_skill_ids=payload.get("missing_skill_ids", []),
        provisional=payload.get("provisional", False),
    )


def recommendation_for(profile: CandidateProfile, *, job_public_id: str) -> Recommendation:
    """One job from this candidate's feed, by public id.

    Raises `NotFound` for a job that is not published or not visible to this
    candidate — never `NotAllowed`, because the existence of a posting a
    blocked company made is itself something this candidate should not learn.
    """
    from apps.jobs.models import Job

    job = (
        Job.objects.published()
        .filter(public_id=job_public_id)
        .select_related("company", "location", "category")
        .first()
    )
    if job is None:
        raise NotFound("That job is not available.")
    return Recommendation(job=job, match=score_pair(profile, job))
