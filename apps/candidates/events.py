"""Candidate profile events (SRS §13.2)."""

from __future__ import annotations

from apps.notifications.events import register

register(
    "candidate.profile_created",
    schema_version=1,
    required={"profile_public_id", "user_id"},
    description="A candidate profile was created, normally at first sign-in.",
)
register(
    "candidate.profile_updated",
    schema_version=1,
    required={"profile_public_id", "sections"},
    description=(
        "One or more profile sections changed. Carries which sections, not the values: "
        "the outbox has a different retention from the profile and must not accumulate "
        "personal data (DEVELOPER.md §4.4)."
    ),
)
register(
    "candidate.skills_changed",
    schema_version=1,
    required={"profile_public_id", "added", "removed"},
    description=(
        "Skills were added or removed. The matcher and the embedding job both read "
        "this rather than diffing the profile."
    ),
)
register(
    "candidate.visibility_changed",
    schema_version=1,
    required={"profile_public_id", "from_visibility", "to_visibility"},
    description=(
        "Profile visibility changed. Search indexes and visibility grants both act on "
        "it, and a candidate turning themselves off expects that to be immediate."
    ),
)
register(
    "candidate.data_erased",
    schema_version=1,
    required={"user_id", "sections_cleared"},
    description="A candidate erased their profile data.",
)
register(
    "candidate.access_anomaly",
    schema_version=1,
    required={"company_id", "company_slug", "signals"},
    description=(
        "One company's rate of reading or exporting candidate profiles crossed a "
        "threshold (SRS §15.4, FR-9.4). Carries the kinds and the counts and never "
        "which candidates: an alert about somebody reading a list of people must not "
        "itself carry the list. Nothing is blocked — the response is a human decision."
    ),
)
