"""
Load exchange rates (ADR-A39).

No live provider is wired: that is a P17 concern, and until then rates are
loaded deliberately from a file or the command line. `FxRate` is append-only, so
loading a rate for a date that already has one is refused rather than silently
overwriting the figure that historical conversions were made with.

    python manage.py load_fx_rates --pair USD:NPR --rate 133.25 --as-of 2026-08-15
    python manage.py load_fx_rates --file rates.csv --source "central-bank"
"""

from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from apps.taxonomy.models import FxRate


class Command(BaseCommand):
    help = "Load exchange rates from arguments or a CSV file."

    def add_arguments(self, parser):
        parser.add_argument("--pair", help="BASE:QUOTE, e.g. USD:NPR")
        parser.add_argument("--rate", help="1 base = this many quote")
        parser.add_argument("--as-of", help="YYYY-MM-DD, defaults to today")
        parser.add_argument("--file", help="CSV with base,quote,rate,as_of")
        parser.add_argument("--source", default="manual")

    def handle(self, *args, **options):
        if options["file"]:
            rows = self._from_file(Path(options["file"]))
        elif options["pair"] and options["rate"]:
            base, _, quote = options["pair"].partition(":")
            if not quote:
                raise CommandError("--pair must look like USD:NPR")
            as_of = dt.date.fromisoformat(options["as_of"]) if options["as_of"] else dt.date.today()
            rows = [(base, quote, Decimal(options["rate"]), as_of)]
        else:
            raise CommandError("Give either --file or both --pair and --rate.")

        loaded = skipped = 0
        for base, quote, rate, as_of in rows:
            try:
                # A savepoint per row: without it a duplicate poisons the whole
                # transaction and the rows after it are lost too.
                with transaction.atomic():
                    FxRate.objects.create(
                        base=base,
                        quote=quote,
                        rate=rate,
                        as_of=as_of,
                        source=options["source"],
                    )
                loaded += 1
            except IntegrityError:
                # A rate already exists for that pair and date. Refusing keeps
                # every conversion made with it reproducible.
                skipped += 1

        self.stdout.write(f"loaded {loaded}, skipped {skipped}")

    def _from_file(self, path: Path) -> list[tuple[str, str, Decimal, dt.date]]:
        if not path.exists():
            raise CommandError(f"No such file: {path}")
        with path.open(encoding="utf-8", newline="") as handle:
            return [
                (
                    row["base"],
                    row["quote"],
                    Decimal(row["rate"]),
                    dt.date.fromisoformat(row["as_of"]),
                )
                for row in csv.DictReader(handle)
            ]
