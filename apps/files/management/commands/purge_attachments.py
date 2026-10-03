"""Remove bytes for soft-deleted and expired attachments (DEVELOPER.md §2.12).

Runnable by hand during an incident, and scheduled daily in production.
"""

from django.core.management.base import BaseCommand

from apps.files import services


class Command(BaseCommand):
    help = "Delete stored bytes for soft-deleted and expired attachments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be purged without deleting anything.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            from django.utils import timezone

            from apps.files.models import Attachment

            now = timezone.now()
            count = (
                Attachment.objects.filter(deleted_at__lte=now).count()
                + Attachment.objects.alive().filter(expires_at__lte=now).count()
            )
            self.stdout.write(f"would purge {count}")
            return

        purged = services.purge()
        self.stdout.write(f"purged {purged}")
