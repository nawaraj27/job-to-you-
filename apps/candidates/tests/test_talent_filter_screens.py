"""
FR-9.2 (Must): "Filter by skills, experience, location, availability and salary
expectation" (Loop Closure TAL-06).

The query parser accepted skill ids and a salary ceiling, but the search form
offered neither, so an employer could not filter by skill or by salary at all —
only type skill names into the free-text box.
"""

from __future__ import annotations

from decimal import Decimal

from apps.candidates.models import CandidateSkill
from apps.candidates.search import TalentQuery
from apps.candidates.tests.test_talent import discoverable, make_searchable
from apps.candidates.tests.test_talent_screens import TalentScreenFixture
from apps.taxonomy.models import Skill, SkillAlias


class SkillFilterScreenTests(TalentScreenFixture):
    def setUp(self):
        super().setUp()
        self.django = Skill.objects.create(name="Django", slug="django")
        SkillAlias.objects.create(skill=self.python, alias="py", locale="en")
        self.other = discoverable(headline="Backend engineer, payments too")
        CandidateSkill.objects.create(profile=self.other, skill=self.django)
        make_searchable(self.other)

    def test_the_form_offers_skills_and_salary(self):
        response = self.client.get(self.search_url)

        self.assertContains(response, 'name="skills"')
        self.assertContains(response, 'name="max_salary"')

    def test_typed_skill_names_filter_the_results(self):
        response = self.client.get(self.search_url, {"skills": "Python"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["query"].skill_ids, (self.python.pk,))

    def test_an_alias_resolves_and_an_unknown_name_is_reported(self):
        query = TalentQuery.from_mapping({"skills": "py, Underwater Basketry"})
        self.assertEqual(query.skill_ids, (self.python.pk,))
        self.assertEqual(query.unknown_skills, ("Underwater Basketry",))

        response = self.client.get(self.search_url, {"skills": "py, Underwater Basketry"})
        self.assertContains(response, "Not in our skill list, so not used: Underwater Basketry")

    def test_only_unknown_skills_is_an_empty_search_not_everyone(self):
        query = TalentQuery.from_mapping({"skills": "Underwater Basketry"})
        self.assertTrue(query.is_empty)

    def test_the_salary_ceiling_is_read_from_the_form(self):
        query = TalentQuery.from_mapping({"max_salary": "1200000"})
        self.assertEqual(query.max_salary_base, Decimal("1200000"))
