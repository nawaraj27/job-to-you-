"""
Run the prompt evaluation corpus (ADR-A20).

Exits non-zero when a case fails, so CI can gate a prompt change on it. That
is the whole point: a prompt is code, and code that degrades quality should not
merge quietly.
"""

import sys

from django.core.management.base import BaseCommand

from apps.ai.services import evals


class Command(BaseCommand):
    help = "Run prompt evaluation cases. Non-zero exit on failure."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--task", default="", help="Only cases for this task key.")

    def handle(self, *args, **options) -> None:
        summary = evals.run_all(task_key=options["task"])

        if not summary["total"]:
            self.stdout.write("no evaluation cases found")
            return

        for result in summary["results"]:
            mark = "PASS" if result.passed else "FAIL"
            self.stdout.write(f"{mark} {result.task_key} · {result.name}")
            for failure in result.failures:
                self.stdout.write(f"       {failure}")

        self.stdout.write(f"\n{summary['passed']}/{summary['total']} passed")
        if summary["failed"]:
            sys.exit(1)
