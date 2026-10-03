"""Messaging events (SRS §13.2).

`message.sent` maps to the `employer_messages` preference group, which is the
whole point of ADR-A26: the settings toggle that has existed since P3 now
governs something real, and a muted channel is genuinely silent.
"""

from __future__ import annotations

from apps.notifications.events import register

register(
    "conversation.opened",
    schema_version=1,
    required={"conversation_public_id", "company_id"},
    description="A company started a thread with a candidate they had a reason to see.",
    preference_group="employer_messages",
)
register(
    "message.sent",
    schema_version=1,
    required={"conversation_public_id", "company_id", "sender_kind"},
    description=(
        "One message. Maps to the `employer_messages` preference, so muting that "
        "channel makes it genuinely silent rather than merely quieter."
    ),
    preference_group="employer_messages",
    audience="conversation_other_side",
    title="New message from {from_name}",
    body="{about} Open the conversation to reply.",
    # One line per thread while it is unread, however many arrive.
    dedupe="message.sent:{conversation_public_id}",
)
register(
    "conversation.blocked",
    schema_version=1,
    required={"conversation_public_id", "company_id"},
    description=(
        "The candidate blocked the company. Distinct from muting: the thread ends and "
        "the company loses sight of them everywhere else too."
    ),
)
register(
    "conversation.reported",
    schema_version=1,
    required={"conversation_public_id", "company_id", "reason"},
    description="Somebody reported a thread. Goes to the staff moderation queue.",
)
