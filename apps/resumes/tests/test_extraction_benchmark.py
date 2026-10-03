"""
The extraction benchmark, as a test.

Running the corpus by hand finds regressions the day somebody remembers to run
it. Running it here finds them in the commit that causes them, which is the
only time the cause is still obvious.

The floor is set just under where extraction actually is. It is a floor and not
a target: raising it when the score rises is correct, lowering it to make a
change pass is the thing this file exists to prevent — a benchmark that moves
down to meet the code measures nothing.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.resumes.benchmark.corpus import CASES, MIME, render
from apps.resumes.benchmark.scoring import score_case, score_corpus
from apps.resumes.parsing import extract_text, parse

#: Where extraction stands. Every case in the corpus is currently clean.
MIN_MACRO_F1 = 0.99
MIN_MICRO_F1 = 0.99

#: Content reported that is not on the document. This one is not a floor with
#: room under it: a fabricated employer or a fabricated qualification reaches a
#: candidate's profile, and the platform has no way to know it put it there.
MAX_FABRICATIONS = 0


def _run() -> list:
    results = []
    for case in CASES:
        for fmt in case.formats:
            body = extract_text(render(case, fmt), mime=MIME[fmt])
            results.append(score_case(case, fmt, parse(body)))
    return results


class ExtractionBenchmarkTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.summary = score_corpus(_run())

    def test_the_corpus_covers_the_document_classes_the_product_claims(self):
        profiles = {case.profile for case in CASES}
        sectors = {case.sector for case in CASES}

        # A benchmark of one kind of CV measures one kind of CV. These are the
        # people the product says it is for.
        self.assertGreaterEqual(len(profiles), 8)
        self.assertGreaterEqual(len(sectors), 12)
        self.assertGreaterEqual(len(CASES), 25)

    def test_nothing_is_reported_that_is_not_on_the_document(self):
        offenders = [
            f"{result.slug} [{result.fmt}]: {name} {score.spurious}"
            for result in self.summary.results
            for name, score in result.fields.items()
            if score.spurious
        ]
        offenders += [
            f"{result.slug} [{result.fmt}]: forbidden {result.forbidden}"
            for result in self.summary.results
            if result.forbidden
        ]

        self.assertLessEqual(len(offenders), MAX_FABRICATIONS, "\n".join(offenders))

    def test_a_picture_is_reported_as_a_picture_and_a_document_is_not(self):
        offenders = [
            f"{result.slug} [{result.fmt}]: {result.picture_error}"
            for result in self.summary.results
            if result.picture_error
        ]

        self.assertEqual(offenders, [])

    def test_accuracy_holds(self):
        self.assertGreaterEqual(self.summary.macro_f1, MIN_MACRO_F1)
        self.assertGreaterEqual(self.summary.micro_f1, MIN_MICRO_F1)

    def test_a_cv_with_no_employment_never_gains_any(self):
        # The failure that matters most, isolated from the average so that it
        # cannot be paid for with a good score somewhere else.
        for result in self.summary.results:
            case = next(row for row in CASES if row.slug == result.slug)
            if case.truth.experience:
                continue
            self.assertEqual(
                result.fields["experience"].found,
                0,
                f"{result.slug} invented {result.fields['experience'].spurious}",
            )
