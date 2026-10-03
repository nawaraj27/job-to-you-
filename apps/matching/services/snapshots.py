"""
Freezing the score onto an application (FR-8.5, ADR-A21).

The one place a match score is persisted for a candidate×job pair, and the
reason it is persisted: **an application is a historical record.** Six months
later the candidate has different skills, the posting has been edited and the
scorer has been retuned; recomputing then would answer a question nobody asked
and would silently rewrite what an employer saw when they made a decision.

So the score is written once, at submission, and never recomputed. The stored
shape carries `algorithm_version`, so a snapshot from an old scorer is
recognisable rather than quietly compared with a new one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .recommendations import score_pair

if TYPE_CHECKING:  # pragma: no cover
    from apps.applications.models import Application
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job

logger = logging.getLogger(__name__)


def snapshot_for(profile: CandidateProfile, job: Job) -> dict[str, Any]:
    """The match, in the shape `Application.match_snapshot` stores.

    Never raises. A submission must not fail because matching had a bad moment:
    the application is the thing that matters to the candidate, and an empty
    snapshot is a missing analytic rather than a lost opportunity. The empty
    case is distinguishable from a real zero because it has no
    `algorithm_version`.
    """
    try:
        return score_pair(profile, job).as_dict()
    except Exception:  # noqa: BLE001 - the application matters more than the score
        logger.exception("match snapshot failed", extra={"profile": profile.pk, "job": job.pk})
        return {}


def is_stale(application: Application, *, current_version: int) -> bool:
    """Whether this snapshot came from a superseded scorer.

    For an interface that wants to say "scored by an earlier method" rather than
    presenting two incomparable numbers side by side — the same honesty the
    resume analysis history screen applies to its own score line.
    """
    stored = (application.match_snapshot or {}).get("algorithm_version")
    return stored is not None and stored != current_version
