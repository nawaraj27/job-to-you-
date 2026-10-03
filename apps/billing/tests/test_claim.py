"""
`claim` — the metered check a double-click cannot beat (P19 audit).

`require()` reads the counter and `consume()` increments it, with the work in
between. Two requests arriving together both pass the read, both do the work,
and a free account gets two of something it is allowed one of. A double-click
is enough to produce that; nothing in the suite noticed, because every test
called the pair in sequence from one thread.

`claim` replaces the pair with one locked increment, and `release` gives the
unit back when the work it paid for fails — so both properties hold: nobody
gets two analyses from one allowance, and nobody pays for the platform's own
outage.

The concurrency itself is asserted in `TransactionTestCase`, with two real
connections. A `TestCase` wraps everything in one transaction, where a second
"thread" would deadlock against itself and prove nothing.
"""

from __future__ import annotations

import threading
import time

from django.core.management import call_command
from django.db import connections
from django.test import TestCase, TransactionTestCase

from apps.billing.services import entitlements, usage
from apps.billing.services.entitlements import PlanLimitExceeded
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile


class ClaimTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        factories.seed()

    def setUp(self):
        self.profile = create_profile()

    def test_a_claim_grants_and_counts(self):
        entitlements.claim(self.profile, "resume_analyses")
        self.assertEqual(entitlements.remaining(self.profile, "resume_analyses"), 0)

    def test_a_second_claim_is_refused(self):
        entitlements.claim(self.profile, "resume_analyses")
        with self.assertRaises(PlanLimitExceeded):
            entitlements.claim(self.profile, "resume_analyses")

    def test_a_refused_claim_does_not_count(self):
        # The counter must not creep past the allowance on refusals, or the
        # usage screen reads 4/1.
        entitlements.claim(self.profile, "resume_analyses")
        for _ in range(3):
            with self.assertRaises(PlanLimitExceeded):
                entitlements.claim(self.profile, "resume_analyses")

        self.assertEqual(usage.used(owner=self.profile, metric="resume_analyses"), 1)

    def test_an_unlimited_plan_still_counts(self):
        factories.put_on_pro(self.profile)
        entitlements.claim(self.profile, "resume_analyses")
        entitlements.claim(self.profile, "resume_analyses")

        self.assertEqual(usage.used(owner=self.profile, metric="resume_analyses"), 2)

    def test_releasing_gives_the_unit_back(self):
        entitlements.claim(self.profile, "resume_analyses")
        entitlements.release(self.profile, "resume_analyses")

        self.assertEqual(entitlements.remaining(self.profile, "resume_analyses"), 1)
        entitlements.claim(self.profile, "resume_analyses")  # usable again

    def test_a_release_cannot_go_below_zero(self):
        # A refund arriving twice must not hand out free allowance.
        for _ in range(3):
            entitlements.release(self.profile, "resume_analyses")

        self.assertEqual(usage.used(owner=self.profile, metric="resume_analyses"), 0)

    def test_crossing_the_limit_still_emits_once(self):
        from apps.notifications.models import OutboxEvent

        entitlements.claim(self.profile, "resume_analyses")
        for _ in range(2):
            with self.assertRaises(PlanLimitExceeded):
                entitlements.claim(self.profile, "resume_analyses")

        self.assertEqual(OutboxEvent.objects.filter(type="subscription.limit_reached").count(), 1)

    def test_claiming_something_unmetered_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            entitlements.claim(self.profile, "pro_templates")


class AnalysisMeterTests(TestCase):
    """The caller, not just the primitive."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        factories.seed()

    def setUp(self):
        self.profile = create_profile()

    def test_a_failed_analysis_returns_the_allowance(self):
        from unittest import mock

        from apps.resumes.services import analysis, documents

        document = documents.create_document(profile=self.profile, actor=self.profile.user)
        with (
            mock.patch.object(analysis, "analyse_version", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            analysis.analyse(document=document, actor=self.profile.user, use_ai=False)

        self.assertEqual(entitlements.remaining(self.profile, "resume_analyses"), 1)

    def test_the_allowance_is_spent_before_the_work_not_after(self):
        # The property that closes the race: by the time the report is being
        # produced, the unit is already gone, so a second request racing this
        # one is refused rather than granted.
        from unittest import mock

        from apps.resumes.services import analysis, documents

        document = documents.create_document(profile=self.profile, actor=self.profile.user)
        observed = {}
        real = analysis.analyse_version

        def spy(**kwargs):
            observed["remaining"] = entitlements.remaining(self.profile, "resume_analyses")
            return real(**kwargs)

        with mock.patch.object(analysis, "analyse_version", side_effect=spy):
            analysis.analyse(document=document, actor=self.profile.user, use_ai=False)

        self.assertEqual(observed["remaining"], 0)


class ConcurrentClaimTests(TransactionTestCase):
    """Two real connections, both claiming the same single allowance.

    This is the test the old `require` + `consume` pair fails. It needs
    `TransactionTestCase` because the defence is a row lock, and a row lock
    cannot be observed from inside one shared transaction.
    """

    reset_sequences = True

    def setUp(self):
        call_command("seed_resume_templates", quiet_summary=True)
        factories.seed()
        self.profile = create_profile()

        # The counter row must already exist. On the very first claim of a
        # period `get_or_create` inserts it, and the unique constraint
        # serialises the two threads on its own — which hides the race being
        # tested. Every claim after the first takes the plain SELECT path,
        # which is where the read-decide-write window actually lives.
        usage.current(owner=self.profile, metric="resume_analyses")

    def test_only_one_of_two_simultaneous_claims_succeeds(self):
        """Both threads inside the same read-decide-write window.

        Two things are needed to reproduce it, and both were learned the hard
        way — this test passed against the *unlocked* implementation twice
        before it was worth anything:

          * the counter row must exist already (see `setUp`), or the insert
            serialises the threads and the lock is never exercised;
          * the window must be widened, because in real use it is microseconds.

        With both, the unlocked version grants two claims from one allowance
        and this fails. The delay creates the overlap; the row lock is what
        makes only one of them win.
        """
        from unittest import mock

        real_current = usage.current

        def slow_current(**kwargs):
            counter = real_current(**kwargs)
            time.sleep(0.4)  # both threads are now inside the window
            return counter

        granted: list[bool] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def attempt():
            try:
                barrier.wait(timeout=5)
                entitlements.claim(self.profile, "resume_analyses")
                granted.append(True)
            except PlanLimitExceeded:
                granted.append(False)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)
            finally:
                connections.close_all()

        with mock.patch.object(usage, "current", side_effect=slow_current):
            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(granted),
            [False, True],
            "both simultaneous claims were granted — one allowance produced two",
        )
        self.assertEqual(usage.used(owner=self.profile, metric="resume_analyses"), 1)
