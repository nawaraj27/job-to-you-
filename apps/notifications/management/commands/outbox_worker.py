"""
Long-running outbox drain (SRS §13.4).

Runs as its own process in docker-compose. A management command rather than a
Celery task loop, because during an incident an operator needs to be able to
run the drain by hand and watch it (DEVELOPER.md §6.5 rule 3).

Sleeps only when a batch comes back empty: under load it drains continuously,
and when idle it polls at OUTBOX_POLL_SECONDS.
"""

from __future__ import annotations

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.notifications.services import outbox


class Command(BaseCommand):
    help = "Continuously drain the transactional outbox."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain a single batch and exit. Used by tests and by cron-style deployments.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="Override OUTBOX_BATCH_SIZE for this run.",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]

        if options["once"]:
            completed = outbox.drain(batch_size)
            self.stdout.write(f"drained {completed}")
            return

        self._running = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._stop)

        self.stdout.write("outbox worker started")
        while self._running:
            completed = outbox.drain(batch_size)
            if completed == 0:
                time.sleep(settings.OUTBOX_POLL_SECONDS)
        self.stdout.write("outbox worker stopped")

    def _stop(self, signum, frame):
        # Finish the batch in flight, then exit. Killing mid-batch would be
        # safe — the transaction rolls back — but a clean stop keeps logs clean.
        self._running = False
