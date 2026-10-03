"""
Load the plan catalogue.

Idempotent, and runs on every fresh environment: entitlements cannot be
resolved without a default plan per audience, so a system without this seed
cannot answer a single authorisation question about what an account may do.

Re-running updates structure and limits while leaving `name` and `summary`
alone — the two fields somebody rewrites for the pricing page.

    python manage.py seed_plans
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.billing import seeds
from apps.billing.limits import validate
from apps.billing.models import LIMITS_SCHEMA_VERSION, Plan


class Command(BaseCommand):
    help = "Load or update the plan catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet-summary",
            action="store_true",
            help="Suppress the count, for use in test setup.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        for row in seeds.PLANS:
            (
                code,
                name,
                audience,
                price,
                currency,
                interval,
                is_quoted,
                is_default,
                order,
                summary,
                limits,
            ) = row

            validate(limits)
            document = {"schema_version": LIMITS_SCHEMA_VERSION, **limits}

            plan, created = Plan.objects.get_or_create(
                code=code,
                defaults={
                    "name": name,
                    "audience": audience,
                    "price": price,
                    "currency": currency,
                    "interval": interval,
                    "is_quoted": is_quoted,
                    "is_default": is_default,
                    "order": order,
                    "summary": summary,
                    "limits": document,
                },
            )
            if not created:
                # Terms are ours; wording is the page's.
                Plan.objects.filter(pk=plan.pk).update(
                    audience=audience,
                    price=price,
                    currency=currency,
                    interval=interval,
                    is_quoted=is_quoted,
                    is_default=is_default,
                    order=order,
                    limits=document,
                )

        if not options["quiet_summary"]:
            self.stdout.write(f"plans: {Plan.objects.count()}")
