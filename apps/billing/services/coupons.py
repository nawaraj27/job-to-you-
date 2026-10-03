"""
Coupons (SRS §5.14).

Two functions, because there are two moments: `validate` is the one a person
triggers by typing a code, and it explains itself; `redeem` is the one
invoicing calls, and it is silent. Splitting them is what stops a checkout
error message being written inside the billing run.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import F

from apps.audit import services as audit
from apps.common.errors import InvalidState, NotFound

from ..models import Coupon, Plan


def validate(*, code: str, plan: Plan) -> Coupon:
    """The coupon, or a refusal somebody can act on."""
    coupon = Coupon.objects.filter(code__iexact=code.strip()).first()
    if coupon is None:
        raise NotFound("That code is not one we recognise.")
    if not coupon.is_active:
        raise InvalidState("That code is no longer active.")
    if not coupon.is_redeemable:
        raise InvalidState("That code has expired or has been fully redeemed.")
    if coupon.plan_id is not None and coupon.plan_id != plan.pk:
        raise InvalidState(f"That code does not apply to {plan.name}.")
    return coupon


def preview(*, coupon: Coupon, plan: Plan) -> Decimal:
    """What the first invoice would come to with this coupon applied."""
    from .invoices import discount_for

    return max(
        plan.price - discount_for(coupon, subtotal=plan.price, plan_id=plan.pk), Decimal("0")
    )


@transaction.atomic
def redeem(*, coupon: Coupon, actor: Any = None) -> Coupon:
    """Count one redemption.

    Incremented in the database rather than read-then-written: a coupon capped
    at 100 redemptions that two people redeem at the same instant must not
    become 101, which is exactly the race a read-modify-write loses.
    """
    Coupon.objects.filter(pk=coupon.pk).update(redemptions=F("redemptions") + 1)
    coupon.refresh_from_db(fields=["redemptions"])
    audit.record(verb="billing.coupon_redeemed", actor=actor, target=coupon)
    return coupon
