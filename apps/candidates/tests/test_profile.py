"""
Candidate profile services (SRS §5.4, ADR-A22, ADR-A39).

The tests that matter here are the ones about data that changes shape after it
is written: a completeness meter that goes stale, a salary that stops being
comparable, and a second tab that silently discards the first tab's save.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.candidates import completeness as completeness_rules
from apps.candidates.models import CandidateProfile, Visibility
from apps.candidates.services import profile as profile_service
from apps.common.errors import InvalidState, StaleWrite
from apps.taxonomy.models import FxRate, Location

from .factories import create_profile


class CreationTests(TestCase):
    def test_a_profile_is_created_once_per_user(self):
        user = create_user()

        first = profile_service.get_or_create_profile(user=user)
        second = profile_service.get_or_create_profile(user=user)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(CandidateProfile.objects.filter(user=user).count(), 1)

    def test_finding_an_existing_profile_opens_no_transaction(self):
        """Called on every render of every candidate screen (CV-04).

        The lookup is the overwhelmingly common path and it writes nothing, so
        it must not open a transaction to discover that. Under a `TestCase`
        each one shows up as a savepoint pair, which is two of the
        twenty-five queries NFR-2 allows a page.
        """
        user = create_user()
        profile_service.get_or_create_profile(user=user)

        with self.assertNumQueries(1):
            profile_service.get_or_create_profile(user=user)


class RequestScopedProfileTests(TestCase):
    """One lookup per request, shared by the view and the shell (CV-04)."""

    def setUp(self):
        from django.test import RequestFactory

        self.request = RequestFactory().get("/")
        self.user = create_user()
        self.request.user = self.user

    def test_the_profile_is_looked_up_once_per_request(self):
        from apps.candidates import selectors

        create_profile(user=self.user)

        with self.assertNumQueries(1):
            first = selectors.profile_for_request(self.request)
            second = selectors.profile_for_request(self.request)

        self.assertEqual(first.pk, second.pk)

    def test_an_account_with_no_profile_is_not_looked_up_twice(self):
        # "There is none" has to be remembered as firmly as a row, or an
        # employer account pays the query on every render of every page.
        from apps.candidates import selectors

        with self.assertNumQueries(1):
            self.assertIsNone(selectors.profile_for_request(self.request))
            self.assertIsNone(selectors.profile_for_request(self.request))

    def test_an_anonymous_visitor_is_not_looked_up_at_all(self):
        from django.contrib.auth.models import AnonymousUser

        from apps.candidates import selectors

        self.request.user = AnonymousUser()

        with self.assertNumQueries(0):
            self.assertIsNone(selectors.profile_for_request(self.request))

    def test_a_view_can_hand_over_the_profile_it_already_resolved(self):
        from apps.candidates import selectors

        profile = create_profile(user=self.user)
        selectors.remember_profile(self.request, profile)

        with self.assertNumQueries(0):
            self.assertEqual(selectors.profile_for_request(self.request).pk, profile.pk)

    def test_creation_is_announced(self):
        from apps.notifications.models import OutboxEvent

        profile_service.get_or_create_profile(user=create_user())

        self.assertTrue(OutboxEvent.objects.filter(type="candidate.profile_created").exists())

    def test_a_new_profile_is_empty_and_not_searchable(self):
        profile = profile_service.get_or_create_profile(user=create_user())

        self.assertEqual(profile.completeness, 0)
        self.assertFalse(profile.is_searchable)


class BasicsTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_saving_basics_updates_the_record_and_the_meter(self):
        profile_service.update_basics(
            profile=self.profile, actor=self.actor, headline="Backend Engineer"
        )
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.headline, "Backend Engineer")
        self.assertGreater(self.profile.completeness, 0)

    def test_a_field_that_is_not_editable_is_dropped_not_written(self):
        # A form post naming `completeness` must not be able to set it.
        profile_service.update_basics(
            profile=self.profile, actor=self.actor, headline="Engineer", completeness=99
        )
        self.profile.refresh_from_db()

        self.assertNotEqual(self.profile.completeness, 99)

    def test_saving_nothing_is_refused(self):
        with self.assertRaises(InvalidState):
            profile_service.update_basics(profile=self.profile, actor=self.actor, nonsense="x")

    def test_the_phone_number_is_encrypted_in_the_database(self):
        from django.db import connection

        profile_service.update_basics(
            profile=self.profile, actor=self.actor, phone="+977 9812345678"
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT phone FROM candidates_candidateprofile WHERE id = %s", [self.profile.pk]
            )
            stored = cursor.fetchone()[0]

        self.assertNotIn("9812345678", stored)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone, "+977 9812345678")

    def test_an_implausible_phone_number_is_refused(self):
        with self.assertRaises(InvalidState):
            profile_service.update_basics(profile=self.profile, actor=self.actor, phone="12")

    def test_an_empty_phone_number_stays_empty(self):
        # Encrypting "" would produce a non-empty ciphertext and every
        # "has the candidate given a number" check would quietly become true.
        profile_service.update_basics(profile=self.profile, actor=self.actor, phone="")
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.phone, "")

    def test_an_unknown_location_is_refused(self):
        with self.assertRaises(InvalidState):
            profile_service.update_basics(
                profile=self.profile, actor=self.actor, location_id=999999
            )

    def test_a_known_location_is_accepted(self):
        call_command("seed_taxonomy", quiet_summary=True)
        kathmandu = Location.objects.get(city="Kathmandu")

        profile_service.update_basics(
            profile=self.profile, actor=self.actor, location_id=kathmandu.pk
        )
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.location_id, kathmandu.pk)


class OptimisticLockTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_every_write_moves_the_version(self):
        before = self.profile.row_version

        profile_service.update_basics(profile=self.profile, actor=self.actor, headline="One")

        self.assertEqual(self.profile.row_version, before + 1)

    def test_a_stale_version_is_refused(self):
        # Two tabs on one profile is ordinary behaviour, and the loser of a
        # silent race never finds out.
        stale = self.profile.row_version
        profile_service.update_basics(profile=self.profile, actor=self.actor, headline="First")

        with self.assertRaises(StaleWrite):
            profile_service.update_basics(
                profile=CandidateProfile.objects.get(pk=self.profile.pk),
                actor=self.actor,
                expected_version=stale,
                headline="Second",
            )

    def test_omitting_the_version_skips_the_check(self):
        # Switch-shaped controls post one field and have no version to send.
        profile_service.update_basics(profile=self.profile, actor=self.actor, headline="One")
        profile_service.update_basics(profile=self.profile, actor=self.actor, headline="Two")

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.headline, "Two")


class SeekingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        FxRate.objects.create(
            base="USD",
            quote="NPR",
            rate=Decimal("133.25"),
            as_of=dt.date.today(),
            source="test",
        )

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_a_salary_is_annualised_into_the_base_currency_with_its_rate(self):
        profile_service.update_seeking(
            profile=self.profile,
            actor=self.actor,
            salary_min=140000,
            salary_max=180000,
            currency="NPR",
            period="month",
        )
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.salary_min_base, Decimal("1680000.00"))
        self.assertEqual(self.profile.salary_max_base, Decimal("2160000.00"))
        self.assertEqual(self.profile.fx_source, "identity")

    def test_a_foreign_salary_records_the_rate_that_converted_it(self):
        profile_service.update_seeking(
            profile=self.profile,
            actor=self.actor,
            salary_min=45000,
            currency="USD",
            period="year",
        )
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.fx_rate_value, Decimal("133.25"))
        self.assertEqual(self.profile.fx_source, "test")
        # The figures the candidate typed are untouched.
        self.assertEqual(self.profile.salary_min, Decimal("45000.00"))
        self.assertEqual(self.profile.currency, "USD")

    def test_a_missing_rate_keeps_the_original_and_clears_the_comparison(self):
        # Refusing the save would lose the candidate's own expectation over a
        # rate table they have no control over.
        profile_service.update_seeking(
            profile=self.profile,
            actor=self.actor,
            salary_min=1000,
            currency="EUR",
            period="month",
        )
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.salary_min, Decimal("1000.00"))
        self.assertIsNone(self.profile.salary_min_base)
        self.assertEqual(self.profile.fx_source, "")

    def test_an_inverted_range_is_refused(self):
        with self.assertRaises(InvalidState):
            profile_service.update_seeking(
                profile=self.profile,
                actor=self.actor,
                salary_min=200000,
                salary_max=100000,
                currency="NPR",
                period="month",
            )

    def test_a_salary_that_is_not_a_number_is_refused(self):
        with self.assertRaises(InvalidState):
            profile_service.update_seeking(
                profile=self.profile, actor=self.actor, salary_min="a lot"
            )

    def test_a_negative_salary_is_refused(self):
        with self.assertRaises(InvalidState):
            profile_service.update_seeking(profile=self.profile, actor=self.actor, salary_min=-5)

    def test_work_types_are_filtered_to_the_known_set(self):
        profile_service.update_seeking(
            profile=self.profile,
            actor=self.actor,
            open_to=["remote", "moon_base", "remote", "contract"],
        )
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.open_to, ["remote", "contract"])

    def test_an_unknown_availability_is_refused(self):
        with self.assertRaises(InvalidState):
            profile_service.update_seeking(profile=self.profile, actor=self.actor, seeking="maybe")

    def test_a_notice_period_outside_a_year_is_refused(self):
        with self.assertRaises(InvalidState):
            profile_service.update_seeking(
                profile=self.profile, actor=self.actor, notice_period_days=400
            )


class PrivacyTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_changing_visibility_is_announced_and_audited(self):
        from apps.audit.models import AuditLog
        from apps.notifications.models import OutboxEvent

        profile_service.update_privacy(
            profile=self.profile, actor=self.actor, visibility=Visibility.EVERYONE
        )

        self.assertTrue(OutboxEvent.objects.filter(type="candidate.visibility_changed").exists())
        self.assertTrue(AuditLog.objects.filter(verb="candidate.visibility_changed").exists())

    def test_going_private_switches_indexing_off(self):
        # Leaving it set is how a profile that was made private reappears in a
        # search engine the next time it goes public.
        profile_service.update_privacy(
            profile=self.profile,
            actor=self.actor,
            visibility=Visibility.EVERYONE,
            search_indexing=True,
        )

        profile_service.update_privacy(
            profile=self.profile, actor=self.actor, visibility=Visibility.NOBODY
        )
        self.profile.refresh_from_db()

        self.assertFalse(self.profile.search_indexing)

    def test_an_unknown_visibility_is_refused(self):
        with self.assertRaises(InvalidState):
            profile_service.update_privacy(
                profile=self.profile, actor=self.actor, visibility="friends"
            )

    def test_a_hidden_profile_is_never_searchable(self):
        _fill(self.profile)
        profile_service.update_privacy(
            profile=self.profile, actor=self.actor, visibility=Visibility.NOBODY
        )
        self.profile.refresh_from_db()

        self.assertFalse(self.profile.is_searchable)
        self.assertNotIn(self.profile, CandidateProfile.objects.searchable())


class CompletenessTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_an_empty_profile_scores_zero_and_lists_everything(self):
        score, gaps = completeness_rules.evaluate(self.profile)

        self.assertEqual(score, 0)
        self.assertEqual(len(gaps), len(completeness_rules.FIELDS))

    def test_the_most_valuable_gap_is_named_first(self):
        # Weights describe how much a field affects matching, so the prompt
        # leads with the one that moves the meter most.
        _score, gaps = completeness_rules.evaluate(self.profile)

        self.assertTrue(gaps[0].startswith("at least"))

    def test_two_skills_are_reported_as_one_short_rather_than_missing(self):
        call_command("seed_taxonomy", quiet_summary=True)
        from apps.candidates.services import skills as skill_service

        skill_service.add_skill(profile=self.profile, actor=self.actor, text="Python")
        skill_service.add_skill(profile=self.profile, actor=self.actor, text="Django")

        _score, gaps = completeness_rules.evaluate(self.profile)

        self.assertIn("1 more skill", gaps[0])

    def test_a_filled_profile_becomes_searchable(self):
        _fill(self.profile)
        self.profile.refresh_from_db()

        self.assertGreaterEqual(self.profile.completeness, completeness_rules.SEARCHABLE_THRESHOLD)
        self.assertTrue(self.profile.is_searchable)

    def test_the_meter_is_recomputed_when_a_child_row_changes(self):
        call_command("seed_taxonomy", quiet_summary=True)
        from apps.candidates.services import skills as skill_service

        before = self.profile.completeness
        for name in ("Python", "Django", "PostgreSQL"):
            skill_service.add_skill(profile=self.profile, actor=self.actor, text=name)
        self.profile.refresh_from_db()

        self.assertGreater(self.profile.completeness, before)


def _fill(profile) -> None:
    """A profile complete enough to be searchable."""
    from apps.candidates.services import sections as section_service
    from apps.candidates.services import skills as skill_service

    call_command("seed_taxonomy", quiet_summary=True)
    actor = profile.user

    profile_service.update_basics(profile=profile, actor=actor, headline="Backend Engineer")
    profile_service.update_seeking(
        profile=profile,
        actor=actor,
        seeking="active",
        notice_period_days=30,
        open_to=["remote"],
        salary_min=140000,
        currency="NPR",
        period="month",
    )
    for name in ("Python", "Django", "PostgreSQL"):
        skill_service.add_skill(profile=profile, actor=actor, text=name)
    section_service.add_entry(
        profile=profile,
        actor=actor,
        section="experience",
        title="Backend Engineer",
        org_name="Nexa",
        start_date="2023-08-01",
        is_current="on",
    )
