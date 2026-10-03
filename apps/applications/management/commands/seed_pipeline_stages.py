"""Seed the platform default pipeline (FR-6.3)."""

from django.core.management.base import BaseCommand

from apps.applications.services import pipeline


class Command(BaseCommand):
    help = "Create the platform default pipeline stages if they are missing."

    def handle(self, *args, **options) -> None:
        created = pipeline.ensure_default_stages()
        self.stdout.write(
            f"default stages: {created} created, {len(pipeline.DEFAULT_STAGES)} total"
        )
