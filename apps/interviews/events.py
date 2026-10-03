"""
Interview events (SRS §13.2).

All of these map to the **`application_updates`** preference group rather than
an "interviews" group of their own. An interview is a step in an application,
and a candidate who muted application updates has already said they do not want
mail about its progress. §13.1 requires every delivered event to map to a group
somebody can toggle; a new group would need a new toggle on the preferences
screen, and there is no separate decision for a candidate to make here.
"""

from __future__ import annotations

from apps.notifications.events import register

register(
    "interview.scheduled",
    schema_version=1,
    required={"session_public_id", "candidate_public_id", "kind"},
    description="An interview was booked. Reminders and ICS invites read this.",
    preference_group="application_updates",
    audience="interview_participants",
    title="Interview booked: {job}",
    body="The interview for {job} at {company} is booked for {when}.",
)
register(
    "interview.rescheduled",
    schema_version=1,
    required={"session_public_id", "candidate_public_id"},
    description=(
        "An interview moved. Distinct from cancel-then-schedule because both sides "
        "need to see continuity rather than a cancellation followed by a surprise."
    ),
    preference_group="application_updates",
    audience="interview_participants",
    title="Interview moved: {job}",
    body="The interview for {job} at {company} has moved to {when}.",
)
register(
    "interview.cancelled",
    schema_version=1,
    required={"session_public_id", "candidate_public_id", "reason"},
    description="An interview was called off by somebody.",
    preference_group="application_updates",
    audience="interview_participants",
    title="Interview cancelled: {job}",
    body="The interview for {job} at {company} was cancelled. {reason}",
)
register(
    "interview.completed",
    schema_version=1,
    required={"session_public_id", "kind", "turns"},
    description=(
        "A session finished. Carries counts, never answers: the outbox has a different "
        "retention from the interview and must not accumulate what a candidate said."
    ),
)
register(
    "interview.no_show",
    schema_version=1,
    required={"session_public_id", "missing_side"},
    description=(
        "Nobody came. `missing_side` matters because a candidate no-show and an "
        "interviewer no-show are different failures with different apologies owed."
    ),
)
register(
    "interview.outcome_shared",
    schema_version=1,
    required={"session_public_id", "recommendation"},
    description=(
        "An employer chose to share an interview summary with the candidate (FR-10.11). "
        "'We'll be in touch' is the most-disliked thing about applying anywhere, and "
        "the data to do better already exists."
    ),
    preference_group="application_updates",
    audience="session_candidate",
    title="Feedback on your interview",
    body="{company} shared feedback from your interview.",
)
