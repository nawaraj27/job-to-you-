"""
Load the resume template catalogue.

Idempotent: runs on every fresh environment, and re-running updates structure
while leaving a curator's edits to `notes` and `suits` in place — the two
fields staff actually rewrite.

    python manage.py seed_resume_templates
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.resumes import seeds
from apps.resumes.models import ResumeTemplate


class Command(BaseCommand):
    help = "Load or update the resume template catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet-summary",
            action="store_true",
            help="Suppress the count, for use in test setup.",
        )
        parser.add_argument(
            "--rewrite-copy",
            action="store_true",
            help=(
                "Also overwrite `suits` and `notes` on rows that already exist. "
                "For when a template's structure has changed and its description "
                "no longer describes it."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        for order, row in enumerate(seeds.TEMPLATES):
            slug, name, family, layout, accent, font, density, tier, suits, notes = row
            template, created = ResumeTemplate.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "family": family,
                    "layout": layout,
                    "accent": accent,
                    "font": font,
                    "density": density,
                    "tier": tier,
                    "suits": suits,
                    "notes": notes,
                    "order": order,
                },
            )
            if not created:
                # Structure is ours; wording is normally the curator's. A
                # structure change is the exception: a description of the old
                # layout left beside the new one is worse than no description.
                fields = {
                    "name": name,
                    "family": family,
                    "layout": layout,
                    "accent": accent,
                    "font": font,
                    "density": density,
                    "tier": tier,
                    "order": order,
                }
                if options["rewrite_copy"]:
                    fields |= {"suits": suits, "notes": notes}
                ResumeTemplate.objects.filter(pk=template.pk).update(**fields)

        if not options["quiet_summary"]:
            self.stdout.write(f"templates: {ResumeTemplate.objects.count()}")
