"""
Publish the shipped legal documents.

Idempotent, and deliberately **never updates an existing version**: a published
version is what somebody agreed to, and editing it in place would rewrite the
text behind their consent. Changing the wording means publishing a new version.

    python manage.py seed_legal
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core import legal, legal_seeds
from apps.core.models import LegalDocument


class Command(BaseCommand):
    help = "Publish the legal documents that ship with this build."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet-summary",
            action="store_true",
            help="Suppress the count, for use in test setup.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        published = 0
        for slug, version, title, summary, body in legal_seeds.DOCUMENTS:
            if LegalDocument.objects.filter(slug=slug, version=version).exists():
                continue
            legal.publish(
                actor=None,
                slug=slug,
                version=version,
                title=title,
                body=body,
                summary=summary,
                is_draft=True,
            )
            published += 1

        if not options["quiet_summary"]:
            self.stdout.write(f"legal documents: {LegalDocument.objects.count()} ({published} new)")
