"""
Diversity and exposure limits (FR-8.8), and the corpus generation counter.

Two problems, both of which look like the feature working until you look at the
whole market rather than one screen.

**Diversity.** A candidate whose skills fit one company's stack will match nine
of that company's twelve postings, and a feed that is nine-tenths one employer
is a feed with one employer in it. The fix is a per-company penalty that grows
with each posting already shown — not a hard cap, because if a candidate really
does only fit one company's roles, hiding them serves nobody.

**Exposure.** The other direction: one popular posting appearing at the top of
every similar candidate's feed. That is bad for the employer, who gets four
hundred near-identical applications, and worse for the candidates, who are all
competing for the same role while others go unseen. A per-job counter tracks how
often a posting has been shown today and tapers its position once it is beyond
what the employer can read.

Both are **ordering adjustments, never filters.** A job removed from a feed is a
job a candidate cannot find; a job moved down is one they find after three
others. The score itself is untouched — a candidate who opens the posting sees
the same number an employer sees, because a score that changed with the queue
would make FR-8.1 meaningless.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.core.cache import cache

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile

logger = logging.getLogger(__name__)

#: Key for the monotonic corpus counter (SRS §10.1).
GENERATION_KEY = "match:job_generation"

#: Points removed for each earlier posting from the same company. Small: three
#: postings from one employer is a coincidence, nine is a feed nobody trusts.
COMPANY_PENALTY = 4.0

#: Never penalise a company below this, however many postings they have. A
#: candidate who genuinely only fits one employer must still see those jobs.
MAX_COMPANY_PENALTY = 20.0

#: Impressions per posting per day beyond which its position starts to taper.
#: An employer who cannot read four hundred applications is not served by
#: getting them.
DAILY_IMPRESSION_SOFT_CAP = 300

#: The most a posting's position can be tapered by exposure.
MAX_EXPOSURE_PENALTY = 12.0

#: How long an impression counter lives. A day, because the cap is a daily one
#: and a counter that never expires measures history rather than today.
IMPRESSION_TTL_SECONDS = 24 * 60 * 60


def job_generation() -> int:
    """A monotonic counter over the job corpus (SRS §10.1).

    Part of every feed cache key. Incremented whenever a posting appears,
    changes or goes away, so a newly published job invalidates every cached feed
    at once — without it, a candidate would not see a relevant new posting until
    they happened to edit their profile, which is the exact moment the product
    is supposed to work.

    Zero when the cache is cold, which is correct: a fresh counter with cold
    caches has nothing stale to invalidate.
    """
    return int(cache.get(GENERATION_KEY) or 0)


def bump_generation() -> int:
    """Advance the corpus generation. Called from the job event consumers.

    `cache.incr` is atomic, and the `ValueError` branch is the cold-cache case
    rather than an error: Django raises when the key does not exist, and the
    correct response is to start the counter, not to fail a job publication.
    """
    try:
        return int(cache.incr(GENERATION_KEY))
    except ValueError:
        cache.set(GENERATION_KEY, 1, None)
        return 1


def record_impressions(job_ids: list[int]) -> None:
    """Count that these postings were shown to somebody.

    Best-effort by design. An impression counter is not an audit record: if
    Redis drops it, a job is shown a few more times than the soft cap intended,
    which is a mild fairness cost and not a correctness one. Paying for
    durability here would mean a write to PostgreSQL on every feed render.
    """
    for job_id in job_ids:
        key = _impression_key(job_id)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, IMPRESSION_TTL_SECONDS)


def impressions_for(job_id: int) -> int:
    return int(cache.get(_impression_key(job_id)) or 0)


def apply_caps(items: list[Any], *, profile: CandidateProfile | None = None) -> list[Any]:
    """Re-order a scored feed for diversity and exposure (FR-8.8).

    Takes and returns the caller's own objects — anything with `.job` and
    `.match` — so this module needs no import from the recommendation service
    and cannot end up owning half of it.

    The adjustment is applied to a *position value*, never to `match.score`. The
    score is what the candidate is told and what the employer sees; moving a job
    down a list must not change what it is worth.
    """
    if not items:
        return items

    seen_per_company: dict[int, int] = {}
    ranked: list[tuple[float, int, Any]] = []

    for item in items:
        company_id = item.job.company_id
        already = seen_per_company.get(company_id, 0)
        seen_per_company[company_id] = already + 1

        penalty = min(already * COMPANY_PENALTY, MAX_COMPANY_PENALTY)
        penalty += _exposure_penalty(item.job.pk)

        # The tiebreaker stays `-pk`, the same one every ranked list in this
        # system ends with, so equal positions never reorder between requests.
        ranked.append((-(item.match.score - penalty), -item.job.pk, item))

    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked]


# --- internals ------------------------------------------------------------


def _exposure_penalty(job_id: int) -> float:
    shown = impressions_for(job_id)
    if shown <= DAILY_IMPRESSION_SOFT_CAP:
        return 0.0
    over = (shown - DAILY_IMPRESSION_SOFT_CAP) / DAILY_IMPRESSION_SOFT_CAP
    return min(over * MAX_EXPOSURE_PENALTY, MAX_EXPOSURE_PENALTY)


def _impression_key(job_id: int) -> str:
    from django.utils import timezone

    return f"match:impressions:{timezone.localdate().isoformat()}:{job_id}"
