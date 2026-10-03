"""Delete spent single-use tokens past their expiry (ADR-A38).

Safe to run at any time: expiry is enforced independently by each token's own
signature, so forgetting that a token was used costs nothing once it can no
longer be presented.
"""

from django.core.management.base import BaseCommand

from apps.common import tokens


class Command(BaseCommand):
    help = "Delete consumed single-use tokens whose expiry has passed."

    def handle(self, *args: object, **options: object) -> None:
        deleted = tokens.purge_expired()
        self.stdout.write(f"deleted {deleted}")
