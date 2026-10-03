"""
Background matching work (DEVELOPER.md §3.9).

Embedding runs on the `ai` queue for the reason every AI task does: a provider
having a bad afternoon must not consume the capacity ordinary background work
needs. The backfill runs on `default`, because it is a loop that enqueues rather
than a call that waits.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

#: How many rows one backfill pass enqueues. Bounded so a first run over a large
#: corpus is a series of small batches rather than one task holding a worker for
#: an hour — and so the budget is spent visibly rather than all at once.
BACKFILL_BATCH = 200


@shared_task(queue="ai")
def embed_job(job_id: int) -> str:
    """Give one posting a vector."""
    from apps.jobs.models import Job

    from .services import embeddings

    job = Job.objects.filter(pk=job_id).select_related("company").prefetch_related("skills").first()
    if job is None:
        return "gone"
    return "embedded" if embeddings.embed_job(job) else "unavailable"


@shared_task(queue="ai")
def embed_profile(profile_id: int) -> str:
    """Give one profile a vector."""
    from apps.candidates.models import CandidateProfile

    from .services import embeddings

    profile = (
        CandidateProfile.objects.filter(pk=profile_id)
        .select_related("user")
        .prefetch_related("skills__skill", "experiences")
        .first()
    )
    if profile is None:
        return "gone"
    return "embedded" if embeddings.embed_profile(profile) else "unavailable"


@shared_task(queue="default")
def backfill_embeddings(limit: int = BACKFILL_BATCH) -> dict[str, int]:
    """Queue embeddings for rows that have none, or have a superseded model's.

    The second half is the model-migration procedure from ADR-A27: after the
    routing table changes, every row still carrying the old model's vector is
    found by a query rather than by a guess about which rows were done, and the
    backfill is re-runnable until the query returns nothing.
    """
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job

    from .services.embeddings import current_model

    model = current_model()
    if not model:
        logger.info("no embedding model configured; nothing to backfill")
        return {"jobs": 0, "profiles": 0}

    jobs = list(
        Job.objects.published()
        .exclude(embedding__isnull=False, embedding_model=model)
        .values_list("pk", flat=True)[:limit]
    )
    for job_id in jobs:
        embed_job.delay(job_id)

    profiles = list(
        CandidateProfile.objects.searchable()
        .exclude(embedding__isnull=False, embedding_model=model)
        .values_list("pk", flat=True)[:limit]
    )
    for profile_id in profiles:
        embed_profile.delay(profile_id)

    return {"jobs": len(jobs), "profiles": len(profiles)}
