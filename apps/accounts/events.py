"""Identity events (SRS §13.2)."""

from __future__ import annotations

from apps.notifications.events import register

register(
    "user.registered",
    schema_version=1,
    required={"user_id", "email"},
    description=(
        "An account was created. Distinct from account.verified: onboarding and "
        "activation analytics subscribe to registration, while anything "
        "privileged waits for verification."
    ),
)
register(
    "account.verified",
    schema_version=1,
    required={"user_id", "email"},
    description="An email address was confirmed.",
)
register(
    "consent.changed",
    schema_version=1,
    required={"user_id", "purpose", "granted"},
    description="A consent decision was recorded or withdrawn.",
)
register(
    "account.erasure_requested",
    schema_version=1,
    required={"user_id", "effective_on"},
    description=(
        "An account erasure was requested (SRS §19.4). Carries the date it takes "
        "effect, because the fourteen-day grace is the whole point of the request "
        "being separate from the erasure."
    ),
    preference_group="product_news",
    audience="account_owner",
    title="Your account will be erased on {effective_on}",
    body="Nothing has been removed yet. You can cancel any time before that date.",
)
register(
    "candidate.erasure_completed",
    schema_version=1,
    required={"user_id", "stores"},
    description=(
        "An erasure finished. Carries the names of the stores that were walked and "
        "no identifiers from them: a record of an erasure that listed what was "
        "erased would defeat its own purpose. Downstream purges subscribe to this."
    ),
)
