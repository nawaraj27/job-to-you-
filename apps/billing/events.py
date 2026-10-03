"""
Billing events (SRS §13.2).

Two rules held here.

**No amounts in a notification's payload beyond what the recipient already
knows.** The outbox has a different retention from the invoice, and an outbox
row that carries a price is a price sitting in a table that gets deleted on a
different schedule from the books.

**`subscription.limit_reached` fires once per period, not once per refusal.**
It is emitted the moment a counter reaches its allowance, which happens exactly
once; emitting on every subsequent refusal would mail somebody about their
plan every time they clicked.
"""

from __future__ import annotations

from apps.notifications.events import register

register(
    "subscription.started",
    schema_version=1,
    required={"subscription_public_id", "plan_code", "owner"},
    description="Somebody moved onto a plan, whether they paid or staff assigned it.",
    preference_group="product_news",
    audience="subscription_owner",
    title="You are on the {plan_code} plan",
    body="Your plan is active. Everything it includes is available now.",
)
register(
    "subscription.changed",
    schema_version=1,
    required={"subscription_public_id", "plan_code", "previous_plan_code"},
    description=(
        "A plan change on an existing subscription. Distinct from cancel-then-start "
        "because the period and the provider reference survive a change and do not "
        "survive a restart."
    ),
    preference_group="product_news",
    audience="subscription_owner",
    title="Your plan changed to {plan_code}",
    body="You were on {previous_plan_code}. The change applies from today.",
)
register(
    "subscription.cancelled",
    schema_version=1,
    required={"subscription_public_id", "plan_code", "effective_on"},
    description=(
        "A cancellation was requested. Carries the date it takes effect, because "
        "somebody who cancels on the 2nd has paid through to the 30th and telling "
        "them otherwise is the complaint that follows."
    ),
    preference_group="product_news",
    audience="subscription_owner",
    title="Your plan ends on {effective_on}",
    body="Nothing changes until then. Your CVs and history stay on the free plan afterwards.",
)
register(
    "subscription.renewed",
    schema_version=1,
    required={"subscription_public_id", "period_end"},
    description="A period rolled over and an invoice was issued for the next one.",
)
register(
    "subscription.payment_failed",
    schema_version=1,
    required={"subscription_public_id", "invoice_number"},
    description=(
        "An invoice passed its due date unpaid. The subscription keeps working: "
        "the plan is not withdrawn until `subscription.expired`."
    ),
    preference_group="product_news",
    audience="subscription_owner",
    title="We could not take payment",
    body="Invoice {invoice_number} is unpaid. Your plan keeps working while you sort it out.",
)
register(
    "subscription.expired",
    schema_version=1,
    required={"subscription_public_id", "plan_code", "reason"},
    description="Entitlements were withdrawn — the grace period ran out, or a cancellation landed.",
)
register(
    "subscription.limit_reached",
    schema_version=1,
    required={"owner", "limit", "allowed"},
    description=(
        "A metered allowance was used up (SRS §13, platform events). Fired once "
        "when the counter reaches the limit, not on every later refusal."
    ),
)
register(
    "invoice.issued",
    schema_version=1,
    required={"invoice_number", "subscription_public_id", "due_on"},
    description="An invoice was raised for a period.",
)
register(
    "invoice.paid",
    schema_version=1,
    required={"invoice_number", "subscription_public_id", "provider"},
    description="An invoice was settled, by a provider or by a person recording a transfer.",
)
