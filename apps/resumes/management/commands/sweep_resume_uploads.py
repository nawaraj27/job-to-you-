"""
Delete guest uploads nobody claimed.

Runs on a schedule in production; available as a command so an operator can run
it after an incident without waiting for the beat.

    python manage.py sweep_resume_uploads --older-than-hours 24
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.resumes.services import claim as claim_service
from apps.resumes.services.upload import GUEST_RETENTION_HOURS


class Command(BaseCommand):
    help = "Delete unclaimed guest resume uploads past their retention window."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-hours", type=int, default=GUEST_RETENTION_HOURS)

    def handle(self, *args, **options):
        removed = claim_service.sweep_unclaimed(older_than_hours=options["older_than_hours"])
        self.stdout.write(self.style.SUCCESS(f"removed {removed} unclaimed upload(s)"))
