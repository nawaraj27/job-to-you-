"""
Billing fixtures.

`seed()` loads the real catalogue rather than inventing plans, because the
numbers on the plans are the product decision under test: a suite that made up
its own limits would pass while the shipped free plan allowed everything.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.management import call_command

from apps.billing.models import LIMITS_SCHEMA_VERSION, Plan
from apps.billing.services import subscriptions


def seed() -> None:
    """The shipped plan catalogue."""
    call_command("seed_plans", quiet_summary=True)


def free_plan() -> Plan:
    return Plan.objects.get(code="free")


def pro_plan() -> Plan:
    return Plan.objects.get(code="pro")


def employer_plan() -> Plan:
    return Plan.objects.get(code="employer")


def make_plan(
    *, code: str, audience: str = "candidate", limits: dict | None = None, **fields: Any
) -> Plan:
    """A one-off plan for a test that needs a limit the catalogue does not have."""
    defaults = {
        "name": code.title(),
        "audience": audience,
        "price": Decimal("0"),
        "currency": "USD",
        "limits": {"schema_version": LIMITS_SCHEMA_VERSION, **(limits or {})},
    }
    defaults.update(fields)
    return Plan.objects.create(code=code, **defaults)


def put_on_pro(profile) -> Any:
    """The shortest path to an entitled candidate."""
    return subscriptions.subscribe(owner=profile, plan=pro_plan())
