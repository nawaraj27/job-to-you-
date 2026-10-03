"""Delete expired idempotency claims (DEVELOPER.md §3.8)."""

from django.core.management.base import BaseCommand

from apps.common import idempotency


class Command(BaseCommand):
    help = "Delete idempotency keys past their expiry."

    def handle(self, *args: object, **options: object) -> None:
        deleted = idempotency.purge_expired()
        self.stdout.write(f"deleted {deleted}")
