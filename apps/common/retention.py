"""
The retention schedule, in one place (SRS §15.3, NFR-8).

§15.3 states a retention duration per data class, and NFR-8 says it is
"executed by a scheduled job". Both are promises made to a person on a privacy
page, and a promise kept by six numbers scattered across six task modules is a
promise nobody can audit — the question "what do we actually delete, and when"
should have one answer that a lawyer and an engineer can read together.

So the durations live here, named after the class they belong to, and the
sweeps that use them are ordinary tasks in their own apps. This module holds no
logic: it is the table from §15.3 written as code, and `SCHEDULE` is what the
staff screen and the test suite both read.

**What is deliberately not here.** Anything whose retention is "life of the
account" needs no sweep — erasure and account closure handle it, and a job
scanning for rows that should live forever is a job that eventually deletes
one.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Interview transcripts (§15.3, restricted). Twelve months: long enough for a
#: dispute about a hiring decision, short enough that a recording of somebody
#: answering questions badly does not follow them for years.
INTERVIEW_TRANSCRIPT_DAYS = 365

#: Applications, notes, ratings and match snapshots (§15.3, confidential),
#: measured from **closure** rather than creation — an open application is
#: current data no matter how old the posting is.
CLOSED_APPLICATION_DAYS = 730

#: Rendered CVs (§14.7). They regenerate on demand from an immutable version,
#: so keeping them is a cache decision rather than a record.
RENDER_DAYS = 90

#: Unclaimed guest uploads (ADR-A32). Hours, not days: nothing has been
#: promised to anybody, and the file was never processed.
UNCLAIMED_UPLOAD_HOURS = 24

#: Raw analytics events (§5.15). A year covers the year-on-year comparisons the
#: dashboards imply; past that the rollups are the record.
ANALYTICS_EVENT_DAYS = 365

#: The personal text inside an `AIRun` (§5.11). The rows outlive it — cost
#: history and the eval corpus need the shape, not the content.
AI_SNAPSHOT_DAYS = 30

#: Soft-deleted rows, per ADR-A12 and §5.16 rule 5. "Soft delete" without a
#: purge means "grows forever" and, after an erasure request, means we said we
#: deleted it and did not.
SOFT_DELETED_DAYS = 30


@dataclass(frozen=True)
class Rule:
    """One line of §15.3, with the job that keeps it."""

    key: str
    label: str
    days: float
    task: str
    note: str = ""


#: Every retention promise the platform makes, and what keeps it. A rule with
#: no task is a promise nothing enforces, which is the thing this table exists
#: to make visible.
SCHEDULE: tuple[Rule, ...] = (
    Rule(
        key="unclaimed_uploads",
        label="Unclaimed guest CVs",
        days=UNCLAIMED_UPLOAD_HOURS / 24,
        task="apps.resumes.tasks.sweep_unclaimed_uploads",
        note="Never processed, never claimed (ADR-A32).",
    ),
    Rule(
        key="ai_snapshots",
        label="AI input and output text",
        days=AI_SNAPSHOT_DAYS,
        task="apps.ai.tasks.purge_run_snapshots",
        note="The run rows stay; the personal text in them goes.",
    ),
    Rule(
        key="renders",
        label="Rendered CV files",
        days=RENDER_DAYS,
        task="apps.resumes.tasks.purge_expired_renders",
        note=(
            "Regenerated on demand from the immutable version. The owning app "
            "releases the render row, then `files.purge` removes the bytes — "
            "the attachment is PROTECTed by the render, so the file job cannot "
            "do it alone."
        ),
    ),
    Rule(
        key="soft_deleted",
        label="Soft-deleted rows",
        days=SOFT_DELETED_DAYS,
        task="apps.common.tasks.purge_soft_deleted",
        note="ADR-A12: every soft-deleted table has a purge date.",
    ),
    Rule(
        key="interview_transcripts",
        label="Interview transcripts",
        days=INTERVIEW_TRANSCRIPT_DAYS,
        task="apps.interviews.tasks.purge_old_transcripts",
        note="The session and its outcome survive; the answers do not.",
    ),
    Rule(
        key="closed_applications",
        label="Closed applications and their notes",
        days=CLOSED_APPLICATION_DAYS,
        task="apps.applications.tasks.purge_closed_applications",
        note="Measured from closure. The application shell and outcome remain.",
    ),
    Rule(
        key="analytics_events",
        label="Raw analytics events",
        days=ANALYTICS_EVENT_DAYS,
        task="apps.analytics.tasks.prune_events",
        note="The rollups are the record past this point.",
    ),
)


def rule(key: str) -> Rule:
    for row in SCHEDULE:
        if row.key == key:
            return row
    raise KeyError(f"{key!r} is not a retention rule. Add it to apps/common/retention.py.")
