"""Application events (SRS §13.2).

Everything downstream of an application — notifying the recruiter, incrementing
the counter, opening a conversation, re-scoring — subscribes to these. The
services that emit them do not know any subscriber exists, so a new reaction is
a new subscriber rather than an edit to the apply path.
"""

from __future__ import annotations

from apps.notifications.events import register

register(
    "application.submitted",
    schema_version=1,
    required={"application_public_id", "job_public_id", "company_id", "job_title"},
    description="A candidate applied. The recruiter notification and the counter read this.",
    preference_group="application_updates",
    audience="application_company_team",
    title="New application for {job_title}",
    body="Somebody applied. Open the pipeline to review them.",
    # Forty applications to one posting in an hour is one line saying forty.
    dedupe="application.submitted:{job_public_id}",
)
register(
    "application.stage_changed",
    schema_version=1,
    required={"application_public_id", "company_id", "from_stage", "to_stage"},
    description="An application moved. Carries stage keys, never internal labels.",
    preference_group="application_updates",
    audience="application_candidate",
    # Neutral: the same event carries a move backwards and a move to a closing
    # stage, and "moved you on" told somebody who had just been turned down
    # that they had progressed.
    title="Update on your application for {job_title}",
    body="{company_name} moved your application to: {stage_label}.{reason_line}",
)
register(
    "application.withdrawn",
    schema_version=1,
    required={"application_public_id", "company_id"},
    description="The candidate pulled out. Distinct from a rejection, and never silent.",
    preference_group="application_updates",
    audience="application_company_team",
    title="A candidate withdrew from {job_title}",
    body="They pulled out of {job_title}. Their application stays in the pipeline, marked withdrawn.",
)
register(
    "application.closed",
    schema_version=1,
    required={"application_public_id", "company_id", "reason"},
    description=(
        "The employer closed it. A rejection is this plus a terminal stage — there is "
        "no separate rejection event, because there is no separate rejection record."
    ),
)
# No candidate notification of its own. Every close is also a stage change, and
# the candidate was sent both: "Fonepay closed your application" beside "moved
# your application to: Hired" — for somebody who had just been hired. The stage
# change now carries the reason, so one notification says everything. The event
# itself stays, for subscribers and webhooks that act on a close.
register(
    "application.contact_revealed",
    schema_version=1,
    required={"application_public_id", "company_id", "basis"},
    description="An employer earned the candidate's contact details (ADR-A34).",
    preference_group="application_updates",
    audience="application_candidate",
    title="An employer can now see your contact details",
    body="Unlocked because your application reached a stage that allows it.",
)
register(
    "offer.sent",
    schema_version=1,
    required={"application_public_id", "company_id", "offer_id"},
    description="An offer went to the candidate.",
    preference_group="application_updates",
    audience="application_candidate",
    title="You have an offer",
    body="Open the application to read the terms and answer.",
)
register(
    "offer.decided",
    schema_version=1,
    required={"application_public_id", "company_id", "offer_id", "state"},
    description="The candidate accepted or declined.",
    preference_group="application_updates",
    audience="application_company_team",
    title="Offer {answer}: {job_title}",
    body="The candidate {answer} your offer for {job_title}. Open the application to see what happens next.",
)
