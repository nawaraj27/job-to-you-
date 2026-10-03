"""
Plan writes and reads (SRS §5.14).

Plans are staff-managed content, like `EmailTemplate` or `ResumeTemplate`: a
row rather than a constant, so commercial terms change without a deploy. What
does *not* change without a deploy is the vocabulary of limits — `limits.py`
declares every key a plan may name, and `create`/`update` refuse a document
that names anything else.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import transaction

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotFound

from .. import limits as limit_registry
from ..models import LIMITS_SCHEMA_VERSION, BillingInterval, Plan, PlanAudience


def by_code(code: str) -> Plan:
    plan = Plan.objects.filter(code=code).first()
    if plan is None:
        raise NotFound("That plan does not exist.")
    return plan


def published(audience: str) -> list[Plan]:
    """Plans somebody may actually be put on, in display order."""
    return list(Plan.objects.filter(audience=audience, is_active=True))


@transaction.atomic
def create(
    *,
    actor: Any,
    code: str,
    name: str,
    audience: str,
    limits: dict[str, Any] | None = None,
    price: Decimal = Decimal("0"),
    currency: str = "NPR",
    interval: str = BillingInterval.MONTH,
    summary: str = "",
    is_quoted: bool = False,
    is_default: bool = False,
    order: int = 0,
) -> Plan:
    if audience not in PlanAudience.values:
        raise InvalidState(f"{audience!r} is not a plan audience.")
    if Plan.objects.filter(code=code).exists():
        raise Conflict("A plan with that code already exists.")

    document = _document(limits or {})
    if is_default:
        _clear_default(audience)

    plan = Plan.objects.create(
        code=code,
        name=name,
        audience=audience,
        summary=summary,
        price=price,
        currency=currency,
        interval=interval,
        is_quoted=is_quoted,
        limits=document,
        is_default=is_default,
        order=order,
    )
    audit.record(verb="billing.plan_created", actor=actor, target=plan, metadata={"code": code})
    return plan


@transaction.atomic
def update(*, plan: Plan, actor: Any, **fields: Any) -> Plan:
    """Change a plan's terms.

    Existing subscriptions follow the plan rather than freezing their terms.
    That is the deliberate reading of "a plan is content": one price rise
    applies to everybody on the plan, and a customer who was promised different
    terms belongs on a different plan rather than on a private copy of this
    one — which is how a billing system ends up with four hundred plans.
    """
    if "limits" in fields:
        fields["limits"] = _document(fields["limits"] or {})
    if fields.get("is_default"):
        _clear_default(plan.audience, excluding=plan.pk)

    for key, value in fields.items():
        setattr(plan, key, value)
    plan.save()

    audit.record(
        verb="billing.plan_updated",
        actor=actor,
        target=plan,
        metadata={"fields": sorted(fields)},
    )
    return plan


@transaction.atomic
def retire(*, plan: Plan, actor: Any) -> Plan:
    """Stop offering a plan without deleting it.

    Deletion is refused by the database anyway — `Subscription.plan` is
    `PROTECT` — because an invoice that references a plan nobody can look up is
    a support ticket with no answer.
    """
    if plan.is_default:
        raise InvalidState("Make another plan the default before retiring this one.")

    plan.is_active = False
    plan.save(update_fields=["is_active", "updated_at"])
    audit.record(verb="billing.plan_retired", actor=actor, target=plan)
    return plan


# --- internals ------------------------------------------------------------


def _document(limits: dict[str, Any]) -> dict[str, Any]:
    """Stamp and validate a limit document (ADR-A27, §5.16 rule 2)."""
    document = {key: value for key, value in limits.items() if key != "schema_version"}
    limit_registry.validate(document)
    return {"schema_version": LIMITS_SCHEMA_VERSION, **document}


def _clear_default(audience: str, *, excluding: int | None = None) -> None:
    """Only one default per audience, and the constraint says so too.

    Cleared before the write rather than relying on the constraint to complain:
    a staff member promoting a new default means "this one instead", and making
    them demote the old one first is a two-step for no reason.
    """
    rows = Plan.objects.filter(audience=audience, is_default=True)
    if excluding is not None:
        rows = rows.exclude(pk=excluding)
    rows.update(is_default=False)
