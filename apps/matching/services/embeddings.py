"""
Keeping vectors on rows (ADR-A27, SRS §5.4, §5.7).

Every embedding goes through `ai.services.runner.embed`, which is the same
pipeline as every other AI call — kill switch, budget, provider chain, retry
classification, cost accounting, `AIRun`. Nothing here knows what a provider is.

**Failure is ordinary and never raises.** A row that could not be embedded keeps
a null vector, retrieval falls back to the deterministic path, and the feed is
complete. This is the difference between a degraded feature and a broken one,
and it is the same rule P12 established for resume analysis.

The vector lives on the row rather than in `AIRun.output`, which is why there is
no cache lookup: the column *is* the cache, invalidated by writing over it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from apps.ai.services import runner

from ..retrieval import text_for

if TYPE_CHECKING:  # pragma: no cover
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job

logger = logging.getLogger(__name__)

#: How much text is embedded. Long enough for any real posting or profile,
#: short enough that one pathological description cannot run up a bill on its
#: own — and embedding models truncate anyway, silently, which is worse.
MAX_CHARS = 8_000


def embed_job(job: Job) -> bool:
    """Embed one posting. Returns whether a vector was stored.

    Not attributed to a user: a posting is embedded because it was published,
    not because someone asked, so the cost belongs to the company that posted
    it. That also keeps a candidate's personal budget away from work they did
    not cause.
    """
    return _embed(job, company=job.company)


def embed_profile(profile: CandidateProfile) -> bool:
    """Embed one profile. Returns whether a vector was stored."""
    return _embed(profile, user=profile.user)


def needs_embedding(obj: Any, *, model: str = "") -> bool:
    """Whether this row's vector is missing or from a superseded model.

    The second half is what makes a model change a procedure rather than an
    incident: after the swap, every row still carrying the old model's vector
    reports as needing one, and the backfill is a query rather than a guess
    about which rows were done.
    """
    if obj.embedding is None:
        return True
    return bool(model) and obj.embedding_model != model


def current_model() -> str:
    """The embedding model retrieval should filter on.

    Read from the routing table rather than stored, so a configuration change
    takes effect without a migration — and so a row embedded by a fallback
    provider is correctly treated as being from a different model.
    """
    from config import ai as ai_config

    chain = ai_config.chain_for(runner.EMBED_TASK)
    return chain[0][1].name if chain else ""


# --- internals ------------------------------------------------------------


def _embed(obj: Any, *, user: Any = None, company: Any = None) -> bool:
    text = text_for(obj)[:MAX_CHARS]
    if not text.strip():
        # Nothing to embed is not a failure. An empty profile has no vector for
        # the same reason it has no match: there is nothing there yet.
        return False

    result = runner.embed(text=text, user=user, company=company, input_ref=obj)
    if not result.ok:
        logger.info(
            "embedding unavailable for %s %s: %s",
            type(obj).__name__,
            obj.pk,
            result.run.error or result.run.status,
        )
        return False

    # `update` rather than `save`: this runs from a task and must not overwrite
    # a title or a salary somebody edited while the model was thinking.
    with transaction.atomic():
        type(obj).objects.filter(pk=obj.pk).update(
            embedding=result.vector,
            embedding_model=result.model,
            embedded_at=timezone.now(),
        )
    return True
