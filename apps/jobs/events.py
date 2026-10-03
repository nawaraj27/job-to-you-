"""Job events (SRS §13.2)."""

from __future__ import annotations

from apps.notifications.events import register

register(
    "job.created",
    schema_version=1,
    required={"job_public_id", "company_id", "title"},
    description="A posting was drafted.",
)
register(
    "job.updated",
    schema_version=1,
    required={"job_public_id", "company_id", "fields"},
    description=(
        "A posting changed. Carries which fields, not their values: the outbox has a "
        "different retention from the job and must not accumulate salary history."
    ),
)
register(
    "job.paused",
    schema_version=1,
    required={"job_public_id", "company_id"},
    description="A posting was taken off the site for now; applications already made continue.",
)
register(
    "job.resumed",
    schema_version=1,
    required={"job_public_id", "company_id"},
    description="A paused posting went back on the site.",
)
register(
    "job.taken_down",
    schema_version=1,
    required={"job_public_id", "company_id", "title", "reason"},
    description="Platform staff unpublished a posting. The company's editors are told why.",
    audience="company_editors",
    title="{title} was taken down",
    body="{reason}",
)
register(
    "job.published",
    schema_version=1,
    required={"job_public_id", "company_id", "title"},
    description=(
        "A posting became visible to candidates. Alerts and indexing read this, and the "
        "company's followers are told (FOL-01)."
    ),
    # Everything a followed company says reaches followers under one switch,
    # "Companies you follow" (FOL-03): new postings, verification, news and
    # events. It was "Job matches" when postings were the only thing a follow
    # delivered; muting follows now does not also mute job alerts.
    preference_group="company_updates",
    audience="company_followers",
    title="{company_name} posted a new job",
    body="{title}",
    # One notice per posting per person. Taking a job back to draft and
    # publishing it again is not a second announcement.
    dedupe="job.published:{job_public_id}",
)
register(
    "job.closed",
    schema_version=1,
    required={"job_public_id", "company_id", "reason"},
    description="A posting stopped accepting applications by somebody's decision.",
    preference_group="job_matches",
    audience="saved_job_holders",
    title="A job you saved has closed",
    body="It is no longer accepting applications.",
)
register(
    "job.expired",
    schema_version=1,
    required={"job_public_id", "company_id"},
    description=(
        "A posting closed because its date passed. Distinct from `job.closed` because "
        "nobody decided it, and a candidate mid-pipeline is owed the difference."
    ),
)
register(
    "job.alert_matched",
    schema_version=1,
    required={"saved_search_id", "profile_public_id", "matches"},
    description="A saved search found new jobs.",
    preference_group="job_matches",
    audience="saved_search_owner",
    title="{matches} new job(s) match a search you saved",
    body="Open your saved searches to see them.",
    dedupe="job.alert_matched:{saved_search_id}",
)
