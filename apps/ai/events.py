"""AI events (SRS §13.2)."""

from __future__ import annotations

from apps.notifications.events import register

register(
    "ai.run_completed",
    schema_version=1,
    required={"run_public_id", "task_key", "status", "cost"},
    description=(
        "One AI call finished, successfully or not. Cost dashboards and analytics "
        "subscribe. Carries no output: the run row holds that, with the retention "
        "of the record it came from."
    ),
)
