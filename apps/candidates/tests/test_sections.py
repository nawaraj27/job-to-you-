"""
Profile sections, policies and erasure.

Sections are where a check constraint and a form disagree most easily — "I still
work here" plus an end date is the obvious case — so the tests concentrate on
the pairs of fields that have to stay coherent.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.candidates import policies, selectors
from apps.candidates.models import Visibility
from apps.candidates.services import erasure as erasure_service
from apps.candidates.services import sections as section_service
from apps.candidates.services import skills as skill_service
from apps.common.errors import Conflict, InvalidState, NotAllowed, NotFound
from apps.taxonomy.models import Language

from .factories import create_profile


class ExperienceTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def _add(self, **overrides):
        values = {
            "title": "Backend Engineer",
            "org_name": "Nexa Payments",
            "start_date": "2023-08-01",
        }
        values.update(overrides)
        return section_service.add_entry(
            profile=self.profile, actor=self.actor, section="experience", **values
        )

    def test_an_entry_is_added_and_dated(self):
        entry = self._add()

        self.assertEqual(entry.start_date, dt.date(2023, 8, 1))
        self.assertEqual(self.profile.experiences.count(), 1)

    def test_a_current_role_has_its_end_date_cleared(self):
        # The check constraint would otherwise reject the row with a message
        # nobody can act on.
        entry = self._add(is_current="on", end_date="2024-01-01")

        self.assertTrue(entry.is_current)
        self.assertIsNone(entry.end_date)

    def test_an_end_before_the_start_is_refused(self):
        with self.assertRaises(InvalidState):
            self._add(start_date="2024-01-01", end_date="2023-01-01")

    def test_a_role_starting_in_the_future_is_refused(self):
        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()

        with self.assertRaises(InvalidState):
            self._add(start_date=tomorrow)

    def test_a_missing_required_field_names_it(self):
        with self.assertRaises(InvalidState) as caught:
            section_service.add_entry(
                profile=self.profile, actor=self.actor, section="experience", title="Engineer"
            )

        self.assertIn("org name", caught.exception.message)

    def test_a_malformed_date_is_refused_in_words(self):
        with self.assertRaises(InvalidState) as caught:
            self._add(start_date="last summer")

        self.assertIn("date", caught.exception.message)

    def test_editing_keeps_the_row_and_changes_the_field(self):
        entry = self._add()

        section_service.update_entry(
            profile=self.profile,
            actor=self.actor,
            section="experience",
            entry_id=entry.pk,
            title="Senior Backend Engineer",
        )
        entry.refresh_from_db()

        self.assertEqual(entry.title, "Senior Backend Engineer")
        self.assertEqual(self.profile.experiences.count(), 1)

    def test_removing_an_entry_removes_only_that_row(self):
        first = self._add()
        self._add(title="Junior Developer", start_date="2022-06-01", end_date="2023-07-01")

        section_service.remove_entry(
            profile=self.profile, actor=self.actor, section="experience", entry_id=first.pk
        )

        self.assertEqual(self.profile.experiences.count(), 1)

    def test_another_profile_s_entry_is_invisible(self):
        entry = self._add()
        other = create_profile()

        with self.assertRaises(NotFound):
            section_service.remove_entry(
                profile=other, actor=other.user, section="experience", entry_id=entry.pk
            )

    def test_an_unknown_section_is_refused(self):
        with self.assertRaises(InvalidState):
            section_service.add_entry(
                profile=self.profile, actor=self.actor, section="hobbies", name="Chess"
            )

    def test_a_field_outside_the_section_is_dropped(self):
        entry = self._add(institution="Not an experience field")

        self.assertFalse(hasattr(entry, "institution"))


class LinkTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_a_bare_domain_gets_a_scheme(self):
        entry = section_service.add_entry(
            profile=self.profile, actor=self.actor, section="link", url="github.com/priya"
        )

        self.assertEqual(entry.url, "https://github.com/priya")

    def test_something_that_is_not_an_address_is_refused(self):
        with self.assertRaises(InvalidState):
            section_service.add_entry(
                profile=self.profile, actor=self.actor, section="link", url="my portfolio"
            )

    def test_the_same_link_twice_is_refused_in_words(self):
        section_service.add_entry(
            profile=self.profile, actor=self.actor, section="link", url="https://example.com"
        )

        with self.assertRaises(Conflict):
            section_service.add_entry(
                profile=self.profile, actor=self.actor, section="link", url="https://example.com"
            )

    def test_an_unknown_kind_falls_back_to_other(self):
        entry = section_service.add_entry(
            profile=self.profile,
            actor=self.actor,
            section="link",
            url="https://example.com",
            kind="myspace",
        )

        self.assertEqual(entry.kind, "other")


class LanguageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        self.nepali = Language.objects.get(code="ne")

    def test_a_language_is_recorded_at_a_cefr_level(self):
        entry = section_service.add_entry(
            profile=self.profile,
            actor=self.actor,
            section="language",
            language_id=self.nepali.pk,
            proficiency="native",
        )

        self.assertEqual(entry.language_id, self.nepali.pk)

    def test_the_same_language_twice_is_refused(self):
        section_service.add_entry(
            profile=self.profile,
            actor=self.actor,
            section="language",
            language_id=self.nepali.pk,
            proficiency="native",
        )

        with self.assertRaises(Conflict):
            section_service.add_entry(
                profile=self.profile,
                actor=self.actor,
                section="language",
                language_id=self.nepali.pk,
                proficiency="c1",
            )

    def test_an_unknown_proficiency_is_refused(self):
        with self.assertRaises(InvalidState):
            section_service.add_entry(
                profile=self.profile,
                actor=self.actor,
                section="language",
                language_id=self.nepali.pk,
                proficiency="fluent-ish",
            )

    def test_an_unknown_language_is_refused(self):
        with self.assertRaises(InvalidState):
            section_service.add_entry(
                profile=self.profile,
                actor=self.actor,
                section="language",
                language_id=999999,
                proficiency="b2",
            )


class PolicyTests(TestCase):
    """Every rule with a passing negative case (DEVELOPER.md §8.1)."""

    def setUp(self):
        self.profile = create_profile()
        self.owner = self.profile.user
        self.stranger = create_user()

    def test_the_owner_may_write(self):
        self.assertEqual(policies.require_owner(self.profile, self.owner), self.profile)

    def test_a_stranger_may_not_write(self):
        with self.assertRaises(NotFound):
            policies.require_owner(self.profile, self.stranger)

    def test_an_anonymous_visitor_may_not_write(self):
        with self.assertRaises(NotFound):
            policies.require_owner(self.profile, None)

    def test_a_private_profile_is_not_readable_by_a_stranger(self):
        with self.assertRaises(NotFound):
            policies.require_can_view(self.profile, self.stranger)

    def test_an_employers_only_profile_is_refused_until_grants_exist(self):
        # Defaulting to permissive while the check is unfinished is how a
        # private profile becomes a public one for a release.
        self.profile.visibility = Visibility.EMPLOYERS
        self.profile.save(update_fields=["visibility"])

        with self.assertRaises(NotFound):
            policies.require_can_view(self.profile, self.stranger)

    def test_a_public_profile_is_readable(self):
        self.profile.visibility = Visibility.EVERYONE
        self.profile.save(update_fields=["visibility"])

        self.assertEqual(policies.require_can_view(self.profile, self.stranger), self.profile)

    def test_indexing_needs_both_switches(self):
        self.profile.visibility = Visibility.EVERYONE
        self.profile.save(update_fields=["visibility"])

        with self.assertRaises(NotAllowed):
            policies.require_indexable(self.profile)

    def test_a_public_indexed_profile_is_indexable(self):
        self.profile.visibility = Visibility.EVERYONE
        self.profile.search_indexing = True
        self.profile.save(update_fields=["visibility", "search_indexing"])

        self.assertIsNone(policies.require_indexable(self.profile))

    def test_a_lookup_by_public_id_respects_visibility(self):
        self.assertIsNone(
            selectors.visible_profile(str(self.profile.public_id), viewer=self.stranger)
        )
        self.assertIsNotNone(
            selectors.visible_profile(str(self.profile.public_id), viewer=self.owner)
        )


class ErasureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile(headline="Backend Engineer", phone="+977 9812345678")
        self.actor = self.profile.user
        skill_service.add_skill(profile=self.profile, actor=self.actor, text="Python")
        section_service.add_entry(
            profile=self.profile,
            actor=self.actor,
            section="experience",
            title="Engineer",
            org_name="Nexa",
            start_date="2023-01-01",
        )

    def test_erasure_needs_the_confirmation_word(self):
        with self.assertRaises(InvalidState):
            erasure_service.erase_profile_data(
                profile=self.profile, actor=self.actor, confirmation="yes"
            )

        self.assertEqual(self.profile.skills.count(), 1)

    def test_erasure_empties_the_profile_but_keeps_the_account(self):
        from apps.accounts.models import User

        erasure_service.erase_profile_data(
            profile=self.profile, actor=self.actor, confirmation="delete"
        )
        self.profile.refresh_from_db()

        self.assertEqual(self.profile.skills.count(), 0)
        self.assertEqual(self.profile.experiences.count(), 0)
        self.assertEqual(self.profile.headline, "")
        self.assertEqual(self.profile.phone, "")
        self.assertEqual(self.profile.completeness, 0)
        self.assertTrue(User.objects.filter(pk=self.actor.pk).exists())

    def test_erasure_is_announced_and_audited(self):
        from apps.audit.models import AuditLog
        from apps.notifications.models import OutboxEvent

        erasure_service.erase_profile_data(
            profile=self.profile, actor=self.actor, confirmation="DELETE"
        )

        self.assertTrue(OutboxEvent.objects.filter(type="candidate.data_erased").exists())
        self.assertTrue(AuditLog.objects.filter(verb="candidate.data_erased").exists())

    def test_the_export_carries_the_profile_without_internals(self):
        export = erasure_service.export_profile(self.profile)

        self.assertEqual(export["profile"]["headline"], "Backend Engineer")
        self.assertEqual(export["skills"][0]["skill"], "Python")
        self.assertNotIn("row_version", export["profile"])
        self.assertNotIn("fx_rate_id", export["profile"])

    def test_the_export_includes_the_phone_number_it_holds(self):
        # Portability means everything held about the person, including the
        # field that is encrypted at rest.
        export = erasure_service.export_profile(self.profile)

        self.assertEqual(export["profile"]["phone"], "+977 9812345678")
