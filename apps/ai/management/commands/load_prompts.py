"""Load prompt files into `PromptTemplate` (ADR-A20). Runs at deploy."""

from django.core.management.base import BaseCommand, CommandError

from apps.ai.services import prompts


class Command(BaseCommand):
    help = "Load prompts from version control, recording each body's hash."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="Report prompt files the database does not match, without writing; "
            "exits non-zero when there are any.",
        )

    def handle(self, *args, **options) -> None:
        if options["check"]:
            # This used to list the file names and nothing else, so "--check"
            # could not say whether a deploy had skipped loading them.
            differing = prompts.stale()
            for task_key, why in differing:
                self.stdout.write(f"  {task_key}: {why}")
            if differing:
                raise CommandError(
                    f"{len(differing)} prompt(s) not loaded; run `manage.py load_prompts`."
                )
            self.stdout.write("prompts: every file matches its loaded version")
            return

        result = prompts.load_all()
        self.stdout.write(
            f"prompts: {result['files']} file(s), {result['created']} new version(s), "
            f"{result['unchanged']} unchanged"
        )
