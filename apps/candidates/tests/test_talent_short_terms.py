"""
Decision D-05: short talent-search terms.

Three characters or more keep substring matching. Shorter terms match a skill's
whole name or alias only, so R, C, Go and UI stay searchable while "a" no longer
matches everybody with an "a" in any skill. An unknown one-letter term is
refused with a sentence. Filters-only searches, rate limits and caps are
unchanged.
"""

from __future__ import annotations

from unittest import mock

from django.urls import reverse

from apps.candidates import search as talent_search
from apps.candidates.models import CandidateSkill
from apps.candidates.services import talent as talent_service
from apps.candidates.tests.test_talent import TalentFixture, discoverable
from apps.common import ratelimit
from apps.common.errors import InvalidState
from apps.taxonomy.models import Skill, SkillAlias


class ShortTermTests(TalentFixture):
    def setUp(self):
        super().setUp()
        self.people = {}
        for name, slug in (
            ("R", "r"),
            ("C", "c-lang"),
            ("Go", "go"),
            ("UI design", "ui-design"),
            ("React", "react"),
            ("Scala", "scala"),
        ):
            skill = Skill.objects.create(name=name, slug=slug)
            person = discoverable(headline=f"{name} specialist")
            CandidateSkill.objects.create(profile=person, skill=skill)
            self.people[name] = person
        # "UI" is the alias recruiters type for UI design.
        SkillAlias.objects.create(
            skill=Skill.objects.get(slug="ui-design"), alias="UI", normalised="ui"
        )

    def found(self, text):
        return set(self.search(text=text).profiles)

    def test_an_unknown_one_character_term_is_refused_with_a_message(self):
        with self.assertRaises(InvalidState) as caught:
            self.search(text="a")
        self.assertIn("too short", caught.exception.message)

    def test_r_matches_exactly_the_people_with_r(self):
        found = self.found("R")
        self.assertIn(self.people["R"], found)
        # React, Scala and "R specialist"-free profiles do not come back.
        self.assertNotIn(self.people["React"], found)
        self.assertNotIn(self.people["Scala"], found)
        self.assertNotIn(self.candidate, found)

    def test_c_matches_exactly_the_people_with_c(self):
        found = self.found("c")
        self.assertEqual(found, {self.people["C"]})

    def test_go_matches_the_skill_and_not_words_containing_go(self):
        found = self.found("Go")
        self.assertIn(self.people["Go"], found)
        self.assertNotIn(self.people["Scala"], found)
        self.assertNotIn(self.people["React"], found)

    def test_ui_matches_through_its_alias(self):
        found = self.found("UI")
        self.assertIn(self.people["UI design"], found)
        self.assertNotIn(self.people["Go"], found)

    def test_three_characters_keep_substring_matching(self):
        # "Pyt" is part of Python; "cal" is part of Scala.
        self.assertIn(self.candidate, self.found("Pyt"))
        self.assertIn(self.people["Scala"], self.found("cal"))

    def test_a_filters_only_search_still_works(self):
        result = talent_service.search(
            membership=self.membership,
            actor=self.owner,
            query=talent_search.TalentQuery(location_id=self.kathmandu.pk),
        )
        self.assertIn(self.candidate, result.profiles)

    def test_the_rate_limit_still_counts_refused_short_terms(self):
        rule = ratelimit.CANDIDATE_SEARCH_PER_MEMBERSHIP
        key = f"membership:{self.membership.pk}"
        with (
            mock.patch.object(ratelimit, "check", wraps=ratelimit.check) as check,
            self.assertRaises(InvalidState),
        ):
            self.search(text="a")
        check.assert_called_once_with(rule, key)

    def test_results_stay_capped(self):
        self.assertEqual(talent_search.MAX_RESULTS, 200)

    def test_the_screen_shows_the_refusal_inline(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("employer:talent", args=[self.company.slug]), {"q": "a"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "too short to search for")
        self.assertContains(page, "such as R, C, Go or UI")
