"""
Run the extraction benchmark and print what it found.

    python manage.py resume_benchmark
    python manage.py resume_benchmark --verbose
    python manage.py resume_benchmark --json var/benchmark/extraction.json

Nothing here touches the database. The corpus is rendered in memory, read by
the production text extractor and the production parser, and compared with the
truth it was written from — so the number moves when extraction changes and
never when a fixture is edited.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.resumes.benchmark.corpus import CASES, MIME, render
from apps.resumes.benchmark.scoring import FIELDS, score_case, score_corpus
from apps.resumes.parsing import extract
from apps.resumes.parsing import text as text_reader


class Command(BaseCommand):
    help = "Score CV extraction against the generated benchmark corpus."

    def say(self, line: str = "") -> None:
        """Write a line that may hold any script the corpus contains.

        A Windows console is cp1252 and raises on the first Devanagari
        character. The corpus deliberately contains names in the script the
        launch market writes in, so a reporter that dies when it prints one is
        a reporter that cannot report the case it most needs to.
        """
        encoding = getattr(self.stdout, "encoding", None) or "utf-8"
        self.stdout.write(line.encode(encoding, "replace").decode(encoding))

    def add_arguments(self, parser) -> None:
        parser.add_argument("--verbose", action="store_true", help="List every miss.")
        parser.add_argument("--json", dest="json_path", default="", help="Write results here.")
        parser.add_argument("--only", default="", help="Run one case by slug.")

    def handle(self, *args, **options) -> None:
        results = []
        for case in CASES:
            if options["only"] and case.slug != options["only"]:
                continue
            for fmt in case.formats:
                data = render(case, fmt)
                started = time.perf_counter()
                try:
                    body = text_reader.extract_text(data, mime=MIME[fmt])
                    parsed = extract.parse(body)
                except Exception as exc:  # noqa: BLE001 - a crash is a result
                    self.stderr.write(f"{case.slug} [{fmt}] raised {exc!r}")
                    continue
                elapsed = int((time.perf_counter() - started) * 1000)
                results.append(score_case(case, fmt, parsed, elapsed))

        if not results:
            self.stderr.write("No cases ran.")
            return

        summary = score_corpus(results)
        self._print_fields(summary)
        self._print_cases(summary, verbose=options["verbose"])
        self._print_headline(summary)

        if options["json_path"]:
            self._write_json(summary, Path(options["json_path"]))

    # --- output -----------------------------------------------------------

    def _print_fields(self, summary) -> None:
        self.say("")
        self.say(f"{'field':<18}{'P':>7}{'R':>7}{'F1':>7}   {'want':>5}{'got':>5}{'ok':>5}")
        self.say("-" * 62)
        for name in FIELDS:
            score = summary.totals[name]
            self.say(
                f"{name:<18}{score.precision:>7.3f}{score.recall:>7.3f}{score.f1:>7.3f}"
                f"   {score.expected:>5}{score.found:>5}{score.correct:>5}"
            )

    def _print_cases(self, summary, *, verbose: bool) -> None:
        self.say("")
        self.say(f"{'case':<30}{'fmt':<6}{'F1':>7}  problems")
        self.say("-" * 90)
        for result in sorted(summary.results, key=lambda row: row.f1):
            problems = []
            if result.picture_error:
                problems.append(result.picture_error)
            if result.forbidden:
                problems.append(f"forbidden skills: {', '.join(result.forbidden)}")
            for name, score in result.fields.items():
                if score.missed:
                    problems.append(f"-{name}: {'; '.join(score.missed)[:70]}")
                if score.spurious:
                    problems.append(f"+{name}: {'; '.join(score.spurious)[:70]}")
                if score.wrong_value:
                    problems.append(f"~{name}: {'; '.join(score.wrong_value)[:70]}")
            shown = problems if verbose else problems[:2]
            self.say(
                f"{result.slug:<30}{result.fmt:<6}{result.f1:>7.3f}  {shown[0] if shown else ''}"
            )
            for line in shown[1:]:
                self.say(f"{'':<43}{line}")

    def _print_headline(self, summary) -> None:
        slowest = max(summary.results, key=lambda row: row.duration_ms)
        self.say("")
        self.say(
            f"cases {len(summary.results)}   clean {summary.clean_cases}"
            f"   macro-F1 {summary.macro_f1:.3f}   micro-F1 {summary.micro_f1:.3f}"
            f"   fabrications {summary.fabrications}"
            f"   slowest {slowest.slug} {slowest.duration_ms}ms"
        )

    def _write_json(self, summary, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "macro_f1": round(summary.macro_f1, 4),
            "micro_f1": round(summary.micro_f1, 4),
            "fabrications": summary.fabrications,
            "clean_cases": summary.clean_cases,
            "fields": {
                name: {
                    "precision": round(score.precision, 4),
                    "recall": round(score.recall, 4),
                    "f1": round(score.f1, 4),
                    "expected": score.expected,
                    "found": score.found,
                    "correct": score.correct,
                }
                for name, score in summary.totals.items()
            },
            "cases": [
                {
                    "slug": result.slug,
                    "format": result.fmt,
                    "f1": round(result.f1, 4),
                    "duration_ms": result.duration_ms,
                    "forbidden": result.forbidden,
                    "picture_error": result.picture_error,
                    "missed": {
                        name: score.missed for name, score in result.fields.items() if score.missed
                    },
                    "spurious": {
                        name: score.spurious
                        for name, score in result.fields.items()
                        if score.spurious
                    },
                    "wrong_value": {
                        name: score.wrong_value
                        for name, score in result.fields.items()
                        if score.wrong_value
                    },
                }
                for result in summary.results
            ],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.stdout.write(f"wrote {path}")
