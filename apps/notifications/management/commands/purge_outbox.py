"""Delete delivered outbox events past the retention window (SRS §5.15).

The outbox is a queue, not a history. Without this it becomes the largest
table in the database within a year.
"""

from django.core.management.base import BaseCommand

from apps.notifications.services import outbox


class Command(BaseCommand):
    help = "Delete processed outbox events older than the retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=None,
            help="Override OUTBOX_RETENTION_HOURS for this run.",
        )

    def handle(self, *args, **options):
        deleted = outbox.purge_processed(older_than_hours=options["hours"])
        self.stdout.write(f"deleted {deleted}")
