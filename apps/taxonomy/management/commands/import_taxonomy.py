"""
Import a skill taxonomy from an external source.

The command knows nothing about ESCO or O*NET; it names a *format* and a
*source label*. The format decides how records are read, the label is what gets
recorded on every imported skill so attribution survives (both ESCO and O*NET
are CC-BY and require it).

    python manage.py import_taxonomy --format csv --path esco_skills.csv --source esco
    python manage.py import_taxonomy --format csv --path esco_skills.csv --source esco --dry-run

A run that fails partway is resumed by repeating the same command: it picks up
the last unfinished run for that source and continues after its checkpoint.
Pass --restart to ignore the checkpoint and walk the whole source again — safe,
because the writes are idempotent, merely slower.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.taxonomy.importers import available_formats, get_format, run_import


class Command(BaseCommand):
    help = "Import skills from an external taxonomy source."

    def add_arguments(self, parser):
        parser.add_argument(
            "--format",
            default="csv",
            choices=available_formats(),
            help="How the records are read.",
        )
        parser.add_argument(
            "--source",
            required=True,
            help="Label recorded on every imported skill: esco, onet, ...",
        )
        parser.add_argument("--path", help="File path, for file-backed formats.")
        parser.add_argument("--delimiter", default=",")
        parser.add_argument(
            "--presorted",
            action="store_true",
            help="The file is already sorted by external id; stream it instead of sorting.",
        )
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument(
            "--restart",
            action="store_true",
            help="Ignore any unfinished run and import from the beginning.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change, then roll everything back.",
        )

    def handle(self, *args, **options):
        try:
            source = get_format(options["format"], options)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        try:
            summary = run_import(
                source,
                batch_size=options["batch_size"],
                resume=not options["restart"],
                dry_run=options["dry_run"],
                on_progress=lambda message: self.stdout.write(message),
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if summary.resumed_from:
            self.stdout.write(f"resumed after {summary.resumed_from}")
        self.stdout.write(self.style.SUCCESS(str(summary)))
