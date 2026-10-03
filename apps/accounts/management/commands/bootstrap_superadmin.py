"""
`manage.py bootstrap_superadmin <email>` — the first superadmin on a new database.

Every later staff grant needs two superadmins (SRS §2.3). The first cannot, so
this command exists for exactly one use and refuses once a superadmin exists.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.accounts.services import staff_roles
from apps.common.errors import DomainError


class Command(BaseCommand):
    help = "Make an existing account the first superadmin. Refuses if one exists."

    def add_arguments(self, parser) -> None:
        parser.add_argument("email")

    def handle(self, *args, **options) -> None:
        user = User.objects.filter(email__iexact=options["email"]).first()
        if user is None:
            raise CommandError("No account uses that email. Sign up first.")
        try:
            staff_roles.bootstrap_superadmin(user=user)
        except DomainError as exc:
            raise CommandError(exc.message) from exc
        self.stdout.write(f"{user.email} is the first superadmin. Set up two-factor now.")
