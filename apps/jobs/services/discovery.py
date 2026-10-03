"""
The candidate's side of jobs: saving, saved searches, alerts, view counting.

Two rules shape it:

  * **A saved search stores its filters, not its URL.** An alert that runs next
    week must mean what the search meant when it was saved, and a querystring
    stops meaning that the first time the search screen changes.
  * **Alerts persist their matches** (ADR-A21). They are batch work with a
    fixed recipient set, so the result is worth a row; recommendations are
    computed on read and are not. `score` here is search relevance, not the
    P13 matcher, and the column says so.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from django.db import models, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.candidates.models import AlertFrequency, SavedJob, SavedSearch
from apps.common.errors import Conflict, InvalidState
from apps.common.hashing import sha256_bytes
from apps.notifications import events

from ..models import Job, JobAlertMatch, JobView
from ..search import SearchQueryInput, search

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile

logger = logging.getLogger(__name__)

MAX_SAVED_SEARCHES = 20

#: One view per person per job per window. Without it a refresh is a view, and
#: the number an employer reads to decide whether a posting works is a lie.
VIEW_WINDOW_HOURS = 6

#: How many jobs one alert run may report. A search matching four hundred jobs
#: needs narrowing, not an email with four hundred links in it.
MAX_ALERT_MATCHES = 20


@transaction.atomic
def save_job(*, profile: CandidateProfile, actor: User, job: Job, note: str = "") -> SavedJob:
    """Keep a job to come back to. Idempotent."""
    if not job.is_published:
        raise InvalidState("That job is no longer open.")

    record, created = SavedJob.objects.get_or_create(
        profile=profile, job=job, defaults={"note": note.strip()[:200]}
    )
    if created:
        audit.record(verb="job.saved", actor=actor, target=job, metadata={"job": job.slug})
    return record


@transaction.atomic
def unsave_job(*, profile: CandidateProfile, actor: User, job: Job) -> None:
    removed, _ = SavedJob.objects.filter(profile=profile, job=job).delete()
    if removed:
        audit.record(verb="job.unsaved", actor=actor, target=job, metadata={"job": job.slug})


@transaction.atomic
def save_search(
    *,
    profile: CandidateProfile,
    actor: User,
    name: str,
    query: SearchQueryInput,
    alert_frequency: str = AlertFrequency.OFF,
) -> SavedSearch:
    """Keep a search, and optionally be told when it matches something new."""
    name = (name or "").strip()
    if not name:
        raise InvalidState("Give the search a name.")
    if query.is_empty:
        raise InvalidState("An empty search would match every job.")
    if alert_frequency not in AlertFrequency.values:
        raise InvalidState("That is not an alert frequency.")
    if SavedSearch.objects.filter(profile=profile).count() >= MAX_SAVED_SEARCHES:
        raise InvalidState(f"You can keep up to {MAX_SAVED_SEARCHES} searches.")
    if SavedSearch.objects.filter(profile=profile, name__iexact=name).exists():
        raise Conflict("You already have a search with that name.")

    record = SavedSearch.objects.create(
        profile=profile,
        name=name[:120],
        query=query.as_mapping(),
        alert_frequency=alert_frequency,
    )
    audit.record(verb="search.saved", actor=actor, target=profile, metadata={"name": record.name})
    return record


@transaction.atomic
def set_alert(*, saved_search: SavedSearch, actor: User, alert_frequency: str) -> SavedSearch:
    if alert_frequency not in AlertFrequency.values:
        raise InvalidState("That is not an alert frequency.")

    saved_search.alert_frequency = alert_frequency
    saved_search.save(update_fields=["alert_frequency", "updated_at"])
    audit.record(
        verb="search.alert_changed",
        actor=actor,
        target=saved_search.profile,
        metadata={"name": saved_search.name, "frequency": alert_frequency},
    )
    return saved_search


@transaction.atomic
def delete_search(*, saved_search: SavedSearch, actor: User) -> None:
    name = saved_search.name
    saved_search.delete()
    audit.record(verb="search.deleted", actor=actor, metadata={"name": name})


def record_view(*, job: Job, viewer_key: str, user: User | None = None, referrer: str = "") -> bool:
    """Count one read of a posting. Returns whether it counted.

    Deduplicated per viewer per window, and never counted for the company's own
    team — an employer refreshing their own posting must not inflate the number
    they use to judge it.
    """
    if not job.is_published:
        return False

    if user is not None and user.is_authenticated:
        from apps.companies.models import CompanyMembership

        if CompanyMembership.objects.active().filter(company_id=job.company_id, user=user).exists():
            return False

    viewer_hash = sha256_bytes(f"{job.pk}:{viewer_key}".encode())
    since = timezone.now() - dt.timedelta(hours=VIEW_WINDOW_HOURS)
    if JobView.objects.filter(job=job, viewer_hash=viewer_hash, created_at__gte=since).exists():
        return False

    with transaction.atomic():
        JobView.objects.create(
            job=job,
            viewer_hash=viewer_hash,
            user=user if user is not None and user.is_authenticated else None,
            referrer=(referrer or "")[:200],
        )
        # Incremented in the database rather than read-then-written: two
        # simultaneous views must not lose one another.
        Job.objects.filter(pk=job.pk).update(views_count=models.F("views_count") + 1)

    return True


def run_alerts(*, frequency: str = AlertFrequency.DAILY, limit: int = 500) -> dict[str, int]:
    """Find new jobs for every saved search on this schedule.

    Idempotent by construction: a match is unique per (search, job), so a
    second run the same day adds nothing and tells nobody twice.
    """
    searches = SavedSearch.objects.filter(alert_frequency=frequency).select_related("profile")[
        :limit
    ]

    scanned = matched = 0
    for saved_search in searches:
        scanned += 1
        matched += _run_one(saved_search)

    return {"searches": scanned, "matches": matched}


@transaction.atomic
def _run_one(saved_search: SavedSearch) -> int:
    """One saved search against everything published since it last ran."""
    query = SearchQueryInput.from_mapping(saved_search.query or {})
    if query.is_empty:
        logger.info("saved search %s has no filters; skipped", saved_search.pk)
        return 0

    base = Job.objects.published()
    if saved_search.last_run_at is not None:
        # Only what is new. Re-reporting a job somebody already saw is how an
        # alert becomes something people mute.
        base = base.filter(published_at__gt=saved_search.last_run_at)

    result = search(query, base=base)
    found = 0

    for job in result.jobs[:MAX_ALERT_MATCHES]:
        _, created = JobAlertMatch.objects.get_or_create(
            saved_search=saved_search, job=job, defaults={"score": _relevance(job)}
        )
        found += int(created)

    saved_search.last_run_at = timezone.now()
    saved_search.save(update_fields=["last_run_at", "updated_at"])

    if found:
        events.emit(
            "job.alert_matched",
            saved_search,
            payload={
                "saved_search_id": saved_search.pk,
                "profile_public_id": str(saved_search.profile.public_id),
                "matches": found,
            },
        )

    return found


def _relevance(job: Job) -> float:
    """Search relevance, when the search annotated one.

    Zero rather than a guess when it did not: this column is not a match score
    and must not be read as one before P13 exists.
    """
    value = getattr(job, "relevance", 0) or 0
    return round(min(float(value), 1.0), 4)
