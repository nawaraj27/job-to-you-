"""
The job-specificity rule (`services.relevance`).

Its accuracy on hand-labelled questions is measured by `manage.py
interview_benchmark --only relevance`; these hold the mechanics that
measurement depends on, without a model.
"""

from __future__ import annotations

from django.test import SimpleTestCase, TransactionTestCase

from apps.interviews.services import relevance

FREIGHT = relevance.Posting(
    title="Freight Operations Analyst",
    description=(
        "Track late freight shipments across depots. Explain to customers what each delay cost. "
        "Join data from six tracking systems into one number."
    ),
    skills=["SQL", "Excel"],
    level="Entry level",
)
PAYMENTS = relevance.Posting(
    title="Payments Engineer",
    description="Own the settlement ledger, reconciliation and the nightly close.",
    skills=["Django", "PostgreSQL"],
)


class FakeEmbedder:
    """Maps texts to vectors by the words they share with fixed topics."""

    TOPICS = ("freight", "delay", "customer", "ledger", "leadership")

    def __call__(self, text: str) -> list[float]:
        lowered = text.lower()
        return [1.0 if topic in lowered else 0.0 for topic in self.TOPICS] + [0.1]


class StemTests(SimpleTestCase):
    def test_word_forms_meet(self):
        self.assertEqual(relevance.stem("stores"), relevance.stem("store"))
        self.assertEqual(relevance.stem("shipments"), relevance.stem("shipment"))
        self.assertEqual(relevance.stem("reconciling"), relevance.stem("reconcile"))
        self.assertEqual(relevance.stem("reconciliation"), relevance.stem("reconcile"))

    def test_stop_words_and_question_scaffolding_are_not_role_terms(self):
        stems = relevance.content_stems(
            "Tell me about a time you worked with the team on a project."
        )
        self.assertEqual(stems, set())


class LexicalTests(SimpleTestCase):
    def test_a_question_naming_the_role_is_specific(self):
        self.assertTrue(
            relevance.lexical_specific("Tell me about a delayed shipment.", FREIGHT.terms())
        )

    def test_a_listed_skill_counts(self):
        self.assertTrue(
            relevance.lexical_specific("Describe an SQL query you fixed.", FREIGHT.terms())
        )

    def test_a_generic_question_is_not(self):
        self.assertFalse(
            relevance.lexical_specific("Tell me about a time you led a project.", FREIGHT.terms())
        )


class BorrowedPhraseTests(SimpleTestCase):
    def test_a_phrase_from_another_posting_is_borrowed(self):
        borrowed = relevance.borrowed_phrases(
            "Tell me about a discrepancy in the nightly close.",
            FREIGHT.terms(),
            FREIGHT.phrases(),
            PAYMENTS.phrases(),
        )
        self.assertTrue(borrowed)

    def test_a_phrase_sharing_a_word_with_this_posting_is_not_borrowed(self):
        own = relevance.Posting(
            title="Settlement analyst", description="Reconciliation of accounts."
        )
        # "settlement ledger" comes from the payments posting, but "settlement"
        # is this posting's own word, so the phrase is not foreign to it.
        borrowed = relevance.borrowed_phrases(
            "Tell me about the settlement ledger.", own.terms(), own.phrases(), PAYMENTS.phrases()
        )
        self.assertEqual(borrowed, set())

    def test_a_borrowed_question_is_not_specific_even_if_it_names_the_role(self):
        row = relevance.score_questions(
            ["How did a late freight delay affect the nightly close?"],
            FREIGHT,
            other_phrases=PAYMENTS.phrases(),
        )[0]
        self.assertTrue(row.lexical)
        self.assertTrue(row.borrowed)
        self.assertFalse(row.specific)


class SemanticTests(SimpleTestCase):
    def test_equivalent_wording_counts_when_meaning_is_close(self):
        posting = relevance.Posting(
            title="Analyst", description="Explain each delay to customers clearly."
        )
        row = relevance.score_questions(
            ["Describe a time a customer was upset about a delay."],
            posting,
            embed=FakeEmbedder(),
            threshold=0.6,
        )[0]
        self.assertIsNotNone(row.semantic)
        self.assertTrue(row.specific)

    def test_meaning_decides_nothing_unless_a_threshold_is_given(self):
        # Calibration found embeddings added no accuracy over stems and
        # phrases, so the product rule does not use them by default.
        unused = relevance.score_questions(
            ["Something unrelated entirely."],
            FREIGHT,
            semantic_scores={"Something unrelated entirely.": 0.9},
        )[0]
        self.assertFalse(unused.specific)

        row = relevance.score_questions(
            ["Something unrelated entirely."],
            FREIGHT,
            semantic_scores={"Something unrelated entirely.": 0.9},
            threshold=0.6,
        )[0]
        self.assertTrue(row.specific)


class ServingTests(SimpleTestCase):
    def score(self, questions):
        return relevance.score_questions(questions, FREIGHT, other_phrases=PAYMENTS.phrases())

    def test_the_pool_keeps_three_when_it_can(self):
        served = relevance.choose_served(
            self.score(
                [
                    "Tell me about a time you led a project.",
                    "Describe a freight delay you explained to a customer.",
                    "Tell me about a depot that missed its dispatch times.",
                ]
            )
        )
        self.assertEqual(len(served), 3)
        self.assertEqual(served[-1], "Tell me about a time you led a project.")

    def test_borrowed_questions_are_never_served_even_to_fill_the_pool(self):
        served = relevance.choose_served(
            self.score(
                [
                    "Tell me about a discrepancy in the nightly close.",
                    "Describe a freight delay you explained to a customer.",
                ]
            )
        )
        self.assertEqual(served, ["Describe a freight delay you explained to a customer."])


class KitMeasureTests(SimpleTestCase):
    def test_coverage_counts_statements_some_question_addresses(self):
        value = relevance.coverage(
            ["Describe a freight delay you explained to a customer."],
            FREIGHT,
            embed=FakeEmbedder(),
        )
        self.assertGreater(value, 0)
        self.assertLessEqual(value, 1)

    def test_expected_overlap_falls_as_pools_grow(self):
        self.assertEqual(relevance.expected_overlap([2, 2]), 1.0)
        self.assertLess(relevance.expected_overlap([6, 6]), relevance.expected_overlap([3, 3]))

    def test_clarity_bounds(self):
        self.assertTrue(
            relevance.is_clear("Tell me about a freight delay you had to explain to a customer.")
        )
        self.assertFalse(relevance.is_clear("Delays?"))
        self.assertFalse(
            relevance.is_clear(
                "Walk me through a system (e.g., a WhatsApp group) you used for freight."
            )
        )


class EmbedderTransactionTests(TransactionTestCase):
    """`runner.embed` emits an event, and events are refused outside a transaction.

    The embedder is called from a management command and from the kit-writing
    Celery task, neither of which has one; the first real calibration run
    crashed on its first embedding.
    """

    def test_embedding_outside_a_transaction_does_not_crash(self):
        from apps.ai.services import prompts
        from apps.interviews.tests.factories import fake_ai

        prompts.load_all()
        with fake_ai():
            vector = relevance.Embedder()("Tell me about a delayed freight shipment.")

        self.assertTrue(vector)
