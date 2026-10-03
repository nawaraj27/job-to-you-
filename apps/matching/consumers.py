"""
What matching does when something happens elsewhere (SRS §13.3, §10.1).

Two jobs, and they are the reason recommendations can be computed on read
without ever being stale:

  * **Bump the corpus generation** whenever a posting appears, changes or goes
    away. Every cached feed keys on that number, so one increment invalidates
    all of them at once. Without it a newly published job could not appear in
    any cached feed until the candidate happened to edit their profile — the
    feed would be stale exactly when a relevant job is posted, which is the one
    moment the product exists for.
  * **Queue re-embedding** when the text a vector was made from changes. A
    posting whose description was rewritten still carrying the old vector is
    the silent-failure class ADR-A27 exists for: nothing errors, retrieval just
    quietly answers a question about the previous text.

Subscribing rather than calling these from the job and profile services keeps
matching out of their transactions: a publication must not fail because Redis
was briefly unreachable.
"""

from __future__ import annotations

import logging

from apps.notifications.consumers import subscribe
from apps.notifications.models import OutboxEvent

from .services import exposure

logger = logging.getLogger(__name__)

#: Fields whose change makes an existing job vector wrong. Editing a salary or a
#: closing date does not: the vector is built from what the posting *says* it is
#: looking for, and re-embedding on every edit would pay for nothing.
EMBEDDING_FIELDS = frozenset({"title", "description", "category", "skills"})


@subscribe("job.published")
def bump_on_publish(event: OutboxEvent) -> None:
    """A new posting invalidates every cached feed, and needs a vector."""
    exposure.bump_generation()
    _queue_job_embedding(event)


@subscribe("job.updated")
def bump_on_update(event: OutboxEvent) -> None:
    """An edited posting may have changed what it matches."""
    exposure.bump_generation()

    changed = set((event.payload or {}).get("fields") or [])
    if changed & EMBEDDING_FIELDS:
        _queue_job_embedding(event)


@subscribe("job.closed")
def bump_on_close(event: OutboxEvent) -> None:
    """A closed posting must leave every feed it is in."""
    exposure.bump_generation()


@subscribe("job.expired")
def bump_on_expiry(event: OutboxEvent) -> None:
    """Same as closing, by the calendar rather than by a decision."""
    exposure.bump_generation()


@subscribe("profile.updated")
def reembed_profile(event: OutboxEvent) -> None:
    """The candidate changed something a vector was built from.

    The cached feed needs no invalidation here: `profile.updated_at` is part of
    the cache key, so the edit that raised this event already changed it.
    """
    _queue_profile_embedding(event)


@subscribe("skills.changed")
def reembed_on_skills(event: OutboxEvent) -> None:
    """Skills are the largest single input to both the vector and the score."""
    _queue_profile_embedding(event)


# --- internals ------------------------------------------------------------


def _queue_job_embedding(event: OutboxEvent) -> None:
    from apps.jobs.models import Job

    from .tasks import embed_job

    public_id = (event.payload or {}).get("job_public_id")
    if not public_id:
        return

    job_id = Job.objects.filter(public_id=public_id).values_list("pk", flat=True).first()
    if job_id:
        embed_job.delay(job_id)


def _queue_profile_embedding(event: OutboxEvent) -> None:
    from apps.candidates.models import CandidateProfile

    from .tasks import embed_profile

    public_id = (event.payload or {}).get("profile_public_id")
    if not public_id:
        return

    profile_id = (
        CandidateProfile.objects.filter(public_id=public_id).values_list("pk", flat=True).first()
    )
    if profile_id:
        embed_profile.delay(profile_id)
