"""Company and team events (SRS §13.2)."""

from __future__ import annotations

from apps.notifications.events import register

register(
    "company.created",
    schema_version=1,
    required={"company_id", "slug", "created_by_id"},
    description="A company was registered.",
)
register(
    "company.verification_state_changed",
    schema_version=1,
    required={"company_id", "from_state", "to_state"},
    description="Verification moved between states.",
)
register(
    "company.verified",
    schema_version=1,
    required={"company_id", "slug"},
    description=(
        "Verification succeeded. Emitted alongside the state-change event because "
        "its subscribers — badge, notification, search boost — should not have to "
        "parse a state diff to learn the one thing they care about. Followers are "
        "told (FOL-03)."
    ),
    preference_group="company_updates",
    audience="company_followers",
    title="{company_name} is now verified",
    body="Its registration has been checked.",
    # Once per company: verified, then re-reviewed, then verified again is
    # not news twice to somebody who already knows.
    dedupe="company.verified:{company_id}",
)
register(
    "company.update_published",
    schema_version=1,
    required={"company_id", "company_name", "update_public_id", "title", "target_url"},
    description=("A company posted an announcement or an event for its followers (FOL-03)."),
    preference_group="company_updates",
    audience="company_followers",
    title="{company_name}: {title}",
    body="{summary}",
    dedupe="company.update_published:{update_public_id}",
)
register(
    "company.verification_reviewed",
    schema_version=1,
    required={"company_id", "company_name", "to_state", "outcome", "target_url"},
    description=(
        "A staff reviewer decided a submission. Delivered to the people who can act "
        "on it: 'more information needed' is a request, and without this nobody at "
        "the company learned of it until they happened to open the screen."
    ),
    audience="company_editors",
    title="{company_name} {outcome}",
    body="{reason}",
)
register(
    "company.suspended",
    schema_version=1,
    required={"company_id", "company_name", "reason"},
    description=(
        "A company was suspended by platform staff. Its editors are told why: the "
        "event had no audience, so a company found its postings gone and its team "
        "locked out with no word from anybody."
    ),
    audience="company_editors",
    title="{company_name} has been suspended",
    body="{reason}",
)
register(
    "company.reinstated",
    schema_version=1,
    required={"company_id", "company_name", "reason"},
    description="A suspended company was reinstated by platform staff.",
    audience="company_editors",
    title="{company_name} has been reinstated",
    body="{reason}",
)
register(
    "team.invited",
    schema_version=1,
    required={"company_id", "email", "role", "invited_by_id"},
    description="Someone was invited to a company.",
)
register(
    "company.member_joined",
    schema_version=1,
    required={"company_id", "user_id", "role", "recipient_ids"},
    description=(
        "An invitation was accepted. The inviter is told, if they are still on the "
        "team: an invitation otherwise vanished from the team screen without a word."
    ),
    audience="payload_users",
    title="{member_name} joined {company_name}",
    body="They accepted your invitation and joined as {role_label}.",
)
register(
    "company.member_removed",
    schema_version=1,
    required={"company_id", "user_id", "removed_by_id", "recipient_ids"},
    description=(
        "A membership was revoked. The removed person is told; before this their "
        "next click was a 404 with no explanation."
    ),
    audience="payload_users",
    title="You no longer have access to {company_name}",
    body=(
        "{removed_by_name} removed you from the team. Its postings, applicants and "
        "messages are no longer available to you."
    ),
)
register(
    "company.ownership_nominated",
    schema_version=1,
    required={"company_id", "recipient_ids"},
    description="An owner nominated a member to take over (SRS §2.6). The nominee decides.",
    audience="payload_users",
    title="{from_name} nominated you to own {company_name}",
    body="Owning the company includes billing and team control. Accept or decline on the team page.",
)
register(
    "company.ownership_nomination_closed",
    schema_version=1,
    required={"company_id", "recipient_ids", "outcome"},
    description="A nomination was declined by the nominee or withdrawn by an owner.",
    audience="payload_users",
    title="{outcome}",
    body="Ownership of {company_name} has not changed.",
)
register(
    "company.ownership_transferred",
    schema_version=1,
    required={"company_id", "from_user_id", "to_user_id", "recipient_ids"},
    description="Ownership passed to another member. Both are notified (SRS §2.6).",
    audience="payload_users",
    title="{to_name} now owns {company_name}",
    body="{from_name} transferred ownership to {to_name}. {from_name} is now a company admin.",
)
