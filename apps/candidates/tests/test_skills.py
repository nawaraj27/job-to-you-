"""
Candidate skills — the matcher's input (SRS §5.4, ADR-A13).

One rule under test above all others: what lands on a profile is a taxonomy
row, never a string. Everything else here is about what happens when the text
someone types does not resolve, because that is the case where the tempting
shortcut — create a skill and move on — destroys the taxonomy quietly.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from apps.candidates.models import CandidateSkill, SkillLevel, SkillSource
from apps.candidates.services import skills as skill_service
from apps.common.errors import Conflict, InvalidState, NotFound
from apps.taxonomy.models import Skill

from .factories import create_profile


class AddSkillTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_free_text_resolves_to_a_taxonomy_row(self):
        record = skill_service.add_skill(profile=self.profile, actor=self.actor, text="JS")

        self.assertEqual(record.skill.slug, "javascript")
        self.assertEqual(record.evidence_ref["matched_on"], "JS")
        self.assertEqual(record.evidence_ref["method"], "alias")

    def test_a_deprecated_skill_lands_as_its_replacement(self):
        # Someone typing AngularJS gets Angular on their profile, because that
        # is the skill jobs are written against today.
        record = skill_service.add_skill(profile=self.profile, actor=self.actor, text="AngularJS")

        self.assertEqual(record.skill.slug, "angular")

    def test_an_unresolvable_skill_is_refused_with_what_we_do_have(self):
        # Creating a row per typo is how "Python", "python3" and "Pyhton"
        # become three requirements and every score silently breaks.
        with self.assertRaises(skill_service.UnknownSkill) as caught:
            skill_service.add_skill(
                profile=self.profile, actor=self.actor, text="Underwater basket weaving"
            )

        self.assertIn("suggestions", caught.exception.details)
        self.assertEqual(Skill.objects.filter(name__icontains="basket").count(), 0)

    def test_a_near_miss_comes_back_with_suggestions(self):
        with self.assertRaises(skill_service.UnknownSkill) as caught:
            skill_service.add_skill(profile=self.profile, actor=self.actor, text="postg")

        self.assertTrue(caught.exception.details["suggestions"])

    def test_an_empty_skill_is_refused(self):
        with self.assertRaises(InvalidState):
            skill_service.add_skill(profile=self.profile, actor=self.actor, text="   ")

    def test_the_same_skill_cannot_be_claimed_twice(self):
        skill_service.add_skill(profile=self.profile, actor=self.actor, text="Python")

        with self.assertRaises(Conflict):
            skill_service.add_skill(profile=self.profile, actor=self.actor, text="py")

    def test_two_spellings_of_one_skill_collapse_to_one_row(self):
        skill_service.add_skill(profile=self.profile, actor=self.actor, text="JavaScript")

        with self.assertRaises(Conflict):
            skill_service.add_skill(profile=self.profile, actor=self.actor, text="ECMAScript")

        self.assertEqual(self.profile.skills.count(), 1)

    def test_years_and_level_are_recorded(self):
        record = skill_service.add_skill(
            profile=self.profile,
            actor=self.actor,
            text="Python",
            years="3.5",
            level=SkillLevel.PROFICIENT,
        )

        self.assertEqual(record.years, Decimal("3.5"))
        self.assertEqual(record.level, SkillLevel.PROFICIENT)

    def test_an_implausible_number_of_years_is_refused(self):
        with self.assertRaises(InvalidState):
            skill_service.add_skill(
                profile=self.profile, actor=self.actor, text="Python", years="99"
            )

    def test_an_unknown_level_is_refused(self):
        with self.assertRaises(InvalidState):
            skill_service.add_skill(
                profile=self.profile, actor=self.actor, text="Python", level="9"
            )

    def test_an_unknown_source_falls_back_to_self_reported(self):
        # The source decides how much the scorer trusts the claim, so an
        # unrecognised one must not become a stronger claim than self-reported.
        record = skill_service.add_skill(
            profile=self.profile, actor=self.actor, text="Python", source="magic"
        )

        self.assertEqual(record.source, SkillSource.SELF)

    def test_a_profile_cannot_claim_unlimited_skills(self):
        names = list(
            Skill.objects.active().values_list("name", flat=True)[: skill_service.MAX_SKILLS]
        )
        for name in names:
            skill_service.add_skill(profile=self.profile, actor=self.actor, text=name)

        with self.assertRaises(InvalidState):
            skill_service.add_skill(profile=self.profile, actor=self.actor, text="Figma")

    def test_adding_a_skill_is_announced_and_audited(self):
        from apps.audit.models import AuditLog
        from apps.notifications.models import OutboxEvent

        skill_service.add_skill(profile=self.profile, actor=self.actor, text="Python")

        self.assertTrue(OutboxEvent.objects.filter(type="candidate.skills_changed").exists())
        self.assertTrue(AuditLog.objects.filter(verb="candidate.skills_changed").exists())


class RemoveAndUpdateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        self.record = skill_service.add_skill(profile=self.profile, actor=self.actor, text="Python")

    def test_removing_a_skill_leaves_the_taxonomy_alone(self):
        skill_service.remove_skill(
            profile=self.profile, actor=self.actor, skill_id=self.record.skill_id
        )

        self.assertEqual(self.profile.skills.count(), 0)
        self.assertTrue(Skill.objects.filter(slug="python").exists())

    def test_removing_a_skill_that_is_not_claimed_is_refused(self):
        with self.assertRaises(NotFound):
            skill_service.remove_skill(profile=self.profile, actor=self.actor, skill_id=999999)

    def test_a_claimed_skill_cannot_be_deleted_from_the_taxonomy(self):
        # PROTECT is what stops a curation mistake from erasing what someone
        # said about themselves.
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            Skill.objects.get(slug="python").delete()

    def test_updating_records_the_new_claim(self):
        skill_service.update_skill(
            profile=self.profile,
            actor=self.actor,
            skill_id=self.record.skill_id,
            years="5",
            level=SkillLevel.EXPERT,
        )
        self.record.refresh_from_db()

        self.assertEqual(self.record.years, Decimal("5.0"))
        self.assertEqual(self.record.level, SkillLevel.EXPERT)

    def test_updating_a_skill_not_on_the_profile_is_refused(self):
        with self.assertRaises(NotFound):
            skill_service.update_skill(
                profile=self.profile, actor=self.actor, skill_id=999999, years="1"
            )

    def test_one_profile_cannot_touch_another_profile_s_skills(self):
        other = create_profile()

        with self.assertRaises(NotFound):
            skill_service.remove_skill(
                profile=other, actor=other.user, skill_id=self.record.skill_id
            )


class OrderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        self.records = [
            skill_service.add_skill(profile=self.profile, actor=self.actor, text=name)
            for name in ("Python", "Django", "PostgreSQL")
        ]

    def test_reordering_writes_the_requested_order(self):
        wanted = [self.records[2].skill_id, self.records[0].skill_id, self.records[1].skill_id]

        skill_service.reorder_skills(profile=self.profile, actor=self.actor, skill_ids=wanted)

        got = list(
            CandidateSkill.objects.filter(profile=self.profile)
            .order_by("order")
            .values_list("skill_id", flat=True)
        )
        self.assertEqual(got, wanted)

    def test_reordering_with_a_foreign_skill_is_refused(self):
        with self.assertRaises(NotFound):
            skill_service.reorder_skills(profile=self.profile, actor=self.actor, skill_ids=[999999])


class SuggestionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_an_empty_profile_gets_no_suggestions(self):
        self.assertEqual(skill_service.suggestions_for(self.profile), [])

    def test_suggestions_come_from_co_occurrence_not_implication(self):
        # React relates to TypeScript and implies JavaScript. Suggesting
        # JavaScript is useless — the matcher already credits it.
        skill_service.add_skill(profile=self.profile, actor=self.actor, text="React")

        suggested = [skill.slug for skill in skill_service.suggestions_for(self.profile)]

        self.assertIn("typescript", suggested)
        self.assertNotIn("javascript", suggested)

    def test_a_skill_already_claimed_is_not_suggested(self):
        skill_service.add_skill(profile=self.profile, actor=self.actor, text="React")
        skill_service.add_skill(profile=self.profile, actor=self.actor, text="TypeScript")

        suggested = [skill.slug for skill in skill_service.suggestions_for(self.profile)]

        self.assertNotIn("typescript", suggested)
