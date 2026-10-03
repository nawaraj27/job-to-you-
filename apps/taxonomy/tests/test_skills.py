"""
Skill resolution, traversal and lifecycle (ADR-A13).

The tests that matter most here are the ones asserting what must *not* happen.
A resolver that maps "Java" onto JavaScript is worse than no resolver: it is
confidently wrong on every job the candidate is scored against, and nothing in
the interface would ever show it.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.common.errors import Conflict, InvalidState
from apps.taxonomy import selectors
from apps.taxonomy.models import (
    RelationKind,
    Skill,
    SkillAlias,
    SkillRelation,
    SkillStatus,
    SkillTranslation,
)
from apps.taxonomy.services import skills as skill_service


class SeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_the_seed_loads_a_working_core(self):
        self.assertGreater(Skill.objects.active().count(), 100)
        self.assertGreater(SkillAlias.objects.count(), 50)
        self.assertGreater(SkillRelation.objects.filter(kind=RelationKind.IMPLIES).count(), 20)

    def test_the_seed_carries_hierarchy_and_translations(self):
        self.assertTrue(SkillRelation.objects.filter(kind=RelationKind.BROADER).exists())
        self.assertTrue(SkillRelation.objects.filter(kind=RelationKind.NARROWER).exists())
        self.assertTrue(SkillTranslation.objects.filter(locale="ne").exists())

    def test_seeding_twice_changes_nothing(self):
        # It runs on every fresh environment; nobody should have to remember
        # whether it already did.
        before = (
            Skill.objects.count(),
            SkillAlias.objects.count(),
            SkillRelation.objects.count(),
            SkillTranslation.objects.count(),
            Skill.objects.retired().count(),
        )

        call_command("seed_taxonomy", quiet_summary=True)

        after = (
            Skill.objects.count(),
            SkillAlias.objects.count(),
            SkillRelation.objects.count(),
            SkillTranslation.objects.count(),
            Skill.objects.retired().count(),
        )
        self.assertEqual(before, after)

    def test_angularjs_is_deprecated_rather_than_an_alias_of_angular(self):
        # They are different technologies. Aliasing one to the other would
        # rewrite what an AngularJS developer's profile claims.
        angularjs = Skill.objects.get(slug="angularjs")

        self.assertEqual(angularjs.status, SkillStatus.DEPRECATED)
        self.assertEqual(angularjs.replaced_by.slug, "angular")

    def test_no_skill_relates_to_itself(self):
        self.assertFalse(
            SkillRelation.objects.filter(from_skill=SkillRelation.objects.first().from_skill_id)
            .filter(from_skill=SkillRelation.objects.first().to_skill_id)
            .exists()
        )


class NormalisationTests(TestCase):
    def test_case_and_punctuation_are_flattened(self):
        self.assertEqual(skill_service.normalise("  React.JS  "), "react.js")
        self.assertEqual(skill_service.normalise("Node/JS"), "node js")

    def test_plus_and_hash_survive(self):
        # C++ and C# are mostly punctuation. Stripping it collapses both onto
        # "c" and merges three distinct languages.
        self.assertEqual(skill_service.normalise("C++"), "c++")
        self.assertEqual(skill_service.normalise("C#"), "c#")
        self.assertNotEqual(skill_service.normalise("C++"), skill_service.normalise("C#"))

    def test_an_alias_derives_its_own_match_column(self):
        # Set by the model, not by callers: an alias whose `normalised` differs
        # from how input is normalised matches nothing, silently.
        skill = Skill.objects.create(slug="temp", name="Temp")
        alias = SkillAlias.objects.create(skill=skill, alias="  Foo BAR  ")

        self.assertEqual(alias.normalised, "foo bar")


class ResolutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_an_alias_resolves_exactly(self):
        match = skill_service.resolve("JS")

        self.assertEqual(match.skill.slug, "javascript")
        self.assertEqual(match.method, "alias")
        self.assertEqual(match.confidence, Decimal("1.00"))

    def test_alias_resolution_is_case_insensitive(self):
        self.assertEqual(skill_service.resolve("js").skill.slug, "javascript")
        self.assertEqual(skill_service.resolve("  Js ").skill.slug, "javascript")

    def test_java_never_resolves_to_javascript(self):
        # Three characters apart, and the single most damaging false positive
        # this taxonomy can produce.
        match = skill_service.resolve("Java")

        self.assertEqual(match.skill.slug, "java")

    def test_c_sharp_and_c_plus_plus_stay_distinct(self):
        self.assertEqual(skill_service.resolve("C#").skill.slug, "c-sharp")
        self.assertEqual(skill_service.resolve("C++").skill.slug, "c-plus-plus")

    def test_a_canonical_name_resolves_without_an_alias(self):
        match = skill_service.resolve("Kubernetes")

        self.assertEqual(match.skill.slug, "kubernetes")
        self.assertEqual(match.method, "name")

    def test_a_typo_resolves_by_trigram(self):
        match = skill_service.resolve("Kubernets")

        self.assertIsNotNone(match)
        self.assertEqual(match.skill.slug, "kubernetes")
        self.assertEqual(match.method, "trigram")

    def test_an_unknown_term_resolves_to_nothing(self):
        # Returning a bad guess is worse than returning nothing: the caller can
        # offer to create a skill, but cannot un-know a wrong answer.
        self.assertIsNone(skill_service.resolve("underwater basket weaving"))

    def test_empty_input_resolves_to_nothing(self):
        self.assertIsNone(skill_service.resolve("   "))

    def test_resolution_follows_a_merge(self):
        duplicate = Skill.objects.create(slug="postgres-db", name="Postgres DB")
        skill_service.merge(source=duplicate, target=Skill.objects.get(slug="postgresql"))

        self.assertEqual(skill_service.resolve("Postgres DB").skill.slug, "postgresql")

    def test_a_deprecated_skill_resolves_to_its_replacement_and_says_so(self):
        # The candidate wrote AngularJS and meant it. The match forwards to the
        # skill in service, and `was_retired` lets the interface explain why
        # rather than silently changing what they typed.
        match = skill_service.resolve("AngularJS")

        self.assertEqual(match.skill.slug, "angular")
        self.assertTrue(match.was_retired)

    def test_a_deprecated_skill_with_no_successor_resolves_to_itself(self):
        # Nothing replaced Flash. It is still the most accurate name for what
        # someone knows, so resolution must not invent a substitute.
        match = skill_service.resolve("Adobe Flash")

        self.assertEqual(match.skill.slug, "flash")
        self.assertFalse(match.was_retired)

    def test_a_live_skill_is_not_flagged_as_retired(self):
        self.assertFalse(skill_service.resolve("Python").was_retired)

    def test_a_translated_name_resolves_in_its_locale(self):
        match = skill_service.resolve("लेखा", locale="ne")

        self.assertEqual(match.skill.slug, "accounting")
        self.assertEqual(match.method, "translation")

    def test_display_name_falls_back_to_english(self):
        accounting = Skill.objects.get(slug="accounting")

        self.assertEqual(accounting.display_name("ne"), "लेखा")
        self.assertEqual(accounting.display_name("ja"), "Accounting")

    def test_resolve_many_maps_each_input(self):
        result = skill_service.resolve_many(["JS", "Django", "nonsense-term"])

        self.assertEqual(result["JS"].skill.slug, "javascript")
        self.assertEqual(result["Django"].skill.slug, "django")
        self.assertIsNone(result["nonsense-term"])

    def test_resolve_many_answers_every_layer_in_one_pass(self):
        # One list reaching all four layers at once — alias, canonical name,
        # deprecation forwarding and trigram — because batching them is where
        # a layer could quietly stop being consulted.
        result = skill_service.resolve_many(
            ["JS", "Kubernetes", "AngularJS", "Kubernets", "underwater basket weaving", "   "]
        )

        self.assertEqual((result["JS"].skill.slug, result["JS"].method), ("javascript", "alias"))
        self.assertEqual(
            (result["Kubernetes"].skill.slug, result["Kubernetes"].method),
            ("kubernetes", "name"),
        )
        self.assertEqual(result["AngularJS"].skill.slug, "angular")
        self.assertTrue(result["AngularJS"].was_retired)
        self.assertEqual(
            (result["Kubernets"].skill.slug, result["Kubernets"].method),
            ("kubernetes", "trigram"),
        )
        self.assertIsNone(result["underwater basket weaving"])
        self.assertIsNone(result["   "])

    def test_resolve_many_agrees_with_resolving_one_at_a_time(self):
        terms = [
            "JS",
            "js",
            "Java",
            "C#",
            "C++",
            "Kubernetes",
            "Kubernets",
            "AngularJS",
            "Adobe Flash",
            "Python",
            "nonsense-term",
            "",
        ]

        batched = skill_service.resolve_many(terms)

        for term in terms:
            with self.subTest(term=term):
                one = skill_service.resolve(term)
                many = batched[term]
                if one is None:
                    self.assertIsNone(many)
                    continue
                self.assertEqual(many.skill.pk, one.skill.pk)
                self.assertEqual(many.method, one.method)
                self.assertEqual(many.confidence, one.confidence)
                self.assertEqual(many.was_retired, one.was_retired)

    def test_resolve_many_does_not_query_per_term(self):
        """The N+1 that put the CV builder screens over NFR-2 (CV-04).

        A list of terms must cost the layers it reaches, not the terms it
        contains — and the unresolvable half is included because the trigram
        pass is the branch a long list of unknown skills falls into.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        short = ["JS", "Django", "Kubernets", "nonsense-term"]
        long = short * 6

        with CaptureQueriesContext(connection) as first:
            skill_service.resolve_many(short)
        with CaptureQueriesContext(connection) as second:
            skill_service.resolve_many(long)

        self.assertLessEqual(
            len(second.captured_queries),
            len(first.captured_queries),
            f"{len(long)} terms cost {len(second.captured_queries)} queries and "
            f"{len(short)} cost {len(first.captured_queries)}.",
        )


class ImplicationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_django_implies_python(self):
        # The edge the matcher spends: a Django developer scored against a
        # Python requirement gets partial credit, not zero.
        django = Skill.objects.get(slug="django")
        python = Skill.objects.get(slug="python")

        credit = skill_service.implications(django)

        self.assertIn(python.pk, credit)
        self.assertEqual(credit[python.pk], Decimal("0.85"))

    def test_credit_multiplies_across_a_chain(self):
        typescript = Skill.objects.get(slug="typescript")
        javascript = Skill.objects.get(slug="javascript")
        html = Skill.objects.create(slug="dom", name="DOM")
        SkillRelation.objects.create(
            from_skill=javascript, to_skill=html, kind=RelationKind.IMPLIES, weight=Decimal("0.5")
        )

        credit = skill_service.implications(typescript)

        self.assertEqual(credit[javascript.pk], Decimal("0.90"))
        self.assertEqual(credit[html.pk], Decimal("0.450"))

    def test_traversal_terminates_on_a_cycle(self):
        # A implies B implies A is a plausible curation mistake and must not
        # hang the matcher.
        first = Skill.objects.create(slug="loop-a", name="Loop A")
        second = Skill.objects.create(slug="loop-b", name="Loop B")
        SkillRelation.objects.create(
            from_skill=first, to_skill=second, kind=RelationKind.IMPLIES, weight=Decimal("0.9")
        )
        SkillRelation.objects.create(
            from_skill=second, to_skill=first, kind=RelationKind.IMPLIES, weight=Decimal("0.9")
        )

        credit = skill_service.implications(first)

        self.assertIn(second.pk, credit)

    def test_a_hierarchy_cycle_terminates_too(self):
        first = Skill.objects.create(slug="cycle-parent", name="Cycle parent")
        second = Skill.objects.create(slug="cycle-child", name="Cycle child")
        SkillRelation.objects.create(from_skill=first, to_skill=second, kind=RelationKind.BROADER)
        SkillRelation.objects.create(from_skill=second, to_skill=first, kind=RelationKind.BROADER)

        self.assertIn(second.pk, skill_service.ancestors(first))

    def test_related_edges_are_not_implications(self):
        # Knowing React says nothing about knowing TypeScript; they merely
        # co-occur. Counting that as credit would inflate every score.
        react = Skill.objects.get(slug="react")
        typescript = Skill.objects.get(slug="typescript")

        credit = skill_service.implications(react)

        self.assertNotIn(typescript.pk, credit)

    def test_related_returns_the_co_occurring_skills(self):
        react = Skill.objects.get(slug="react")

        self.assertIn("typescript", [skill.slug for skill in skill_service.related(react)])

    def test_a_skill_with_no_implications_yields_nothing(self):
        self.assertEqual(skill_service.implications(Skill.objects.get(slug="git")), {})


class HierarchyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_ancestors_walk_upwards(self):
        postgresql = Skill.objects.get(slug="postgresql")
        sql = Skill.objects.get(slug="sql")

        self.assertIn(sql.pk, skill_service.ancestors(postgresql))

    def test_descendants_walk_downwards(self):
        sql = Skill.objects.get(slug="sql")
        postgresql = Skill.objects.get(slug="postgresql")

        self.assertIn(postgresql.pk, skill_service.descendants(sql))

    def test_hierarchy_does_not_leak_into_implication_credit(self):
        # Being a *kind of* SQL skill and *partly satisfying* a SQL requirement
        # are different claims, carried on different edges.
        sql = Skill.objects.get(slug="sql")

        self.assertNotIn(sql.pk, skill_service.ancestors(Skill.objects.get(slug="django")))

    def test_traversal_stops_at_the_depth_limit(self):
        chain = [Skill.objects.create(slug=f"deep-{i}", name=f"Deep {i}") for i in range(7)]
        for parent, child in zip(chain, chain[1:], strict=False):
            SkillRelation.objects.create(
                from_skill=child, to_skill=parent, kind=RelationKind.BROADER
            )

        reached = skill_service.ancestors(chain[-1], max_depth=2)

        self.assertEqual(len(reached), 2)


class AliasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_adding_an_alias_makes_it_resolve(self):
        python = Skill.objects.get(slug="python")
        skill_service.add_alias(skill=python, alias="CPython", actor=create_user())

        self.assertEqual(skill_service.resolve("cpython").skill.slug, "python")

    def test_adding_the_same_alias_twice_is_harmless(self):
        python = Skill.objects.get(slug="python")
        first = skill_service.add_alias(skill=python, alias="CPython")
        second = skill_service.add_alias(skill=python, alias="CPython")

        self.assertEqual(first.pk, second.pk)

    def test_a_differently_spelled_duplicate_is_the_same_alias(self):
        # "C Python" and "cpython" normalise identically; the second must not
        # create a second row racing the first.
        python = Skill.objects.get(slug="python")
        first = skill_service.add_alias(skill=python, alias="cpython")
        second = skill_service.add_alias(skill=python, alias="  CPython  ")

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SkillAlias.objects.filter(normalised="cpython").count(), 1)

    def test_an_alias_claimed_by_another_skill_is_refused(self):
        # Silently repointing it would change what every existing profile means.
        with self.assertRaises(Conflict):
            skill_service.add_alias(skill=Skill.objects.get(slug="java"), alias="JS")

    def test_the_collision_message_names_the_holder(self):
        with self.assertRaises(Conflict) as caught:
            skill_service.add_alias(skill=Skill.objects.get(slug="java"), alias="JS")

        self.assertIn("JavaScript", caught.exception.message)

    def test_the_database_refuses_a_duplicate_alias_outright(self):
        # The service checks first, but the constraint is what holds under two
        # curators saving at once.
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            SkillAlias.objects.create(skill=Skill.objects.get(slug="java"), alias="js")

    def test_the_same_spelling_may_mean_different_things_in_different_locales(self):
        skill_service.add_alias(
            skill=Skill.objects.get(slug="accounting"), alias="lekha", locale="ne"
        )

        self.assertEqual(skill_service.resolve("lekha", locale="ne").skill.slug, "accounting")
        self.assertIsNone(skill_service.resolve("lekha"))

    def test_an_empty_alias_is_refused(self):
        with self.assertRaises(InvalidState):
            skill_service.add_alias(skill=Skill.objects.get(slug="python"), alias="  ")


class TranslationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_setting_a_translation_twice_updates_rather_than_duplicates(self):
        skill = Skill.objects.get(slug="teamwork")
        skill_service.set_translation(skill=skill, locale="fr", name="Travail d'equipe")
        skill_service.set_translation(skill=skill, locale="fr", name="Travail d'équipe")

        self.assertEqual(skill.translations.filter(locale="fr").count(), 1)
        self.assertEqual(skill.display_name("fr"), "Travail d'équipe")

    def test_english_is_the_canonical_name_not_a_translation(self):
        with self.assertRaises(InvalidState):
            skill_service.set_translation(
                skill=Skill.objects.get(slug="teamwork"), locale="en", name="Team work"
            )

    def test_an_empty_translation_is_refused(self):
        with self.assertRaises(InvalidState):
            skill_service.set_translation(
                skill=Skill.objects.get(slug="teamwork"), locale="ne", name="   "
            )


class DeprecationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.skill = Skill.objects.get(slug="selenium")

    def test_deprecating_records_why_and_when(self):
        skill_service.deprecate(skill=self.skill, reason="Team standardised on Playwright.")
        self.skill.refresh_from_db()

        self.assertEqual(self.skill.status, SkillStatus.DEPRECATED)
        self.assertIsNotNone(self.skill.retired_at)
        self.assertIn("Playwright", self.skill.retired_reason)

    def test_a_deprecated_skill_leaves_the_offer_surface_but_keeps_its_data(self):
        skill_service.deprecate(skill=self.skill, reason="Obsolete.")

        self.assertNotIn(self.skill, Skill.objects.active())
        self.assertTrue(Skill.objects.filter(pk=self.skill.pk).exists())

    def test_deprecating_without_a_reason_is_refused(self):
        with self.assertRaises(InvalidState):
            skill_service.deprecate(skill=self.skill, reason="  ")

    def test_deprecating_twice_is_refused(self):
        skill_service.deprecate(skill=self.skill, reason="Obsolete.")

        with self.assertRaises(InvalidState):
            skill_service.deprecate(skill=self.skill, reason="Obsolete again.")

    def test_a_skill_cannot_replace_itself(self):
        with self.assertRaises(InvalidState):
            skill_service.deprecate(skill=self.skill, reason="Obsolete.", replacement=self.skill)

    def test_a_replacement_cycle_is_refused(self):
        playwright = Skill.objects.get(slug="playwright")
        skill_service.deprecate(skill=self.skill, reason="Obsolete.", replacement=playwright)

        with self.assertRaises(InvalidState):
            skill_service.deprecate(
                skill=playwright, reason="Also obsolete.", replacement=self.skill
            )

    def test_reinstating_brings_it_back(self):
        skill_service.deprecate(skill=self.skill, reason="Premature.")
        skill_service.reinstate(skill=self.skill)
        self.skill.refresh_from_db()

        self.assertTrue(self.skill.is_active)
        self.assertIsNone(self.skill.replaced_by)
        self.assertEqual(self.skill.retired_reason, "")

    def test_a_merged_skill_cannot_be_reinstated(self):
        # A merge asserts the two rows always meant the same thing. Splitting
        # them again would divide data since written against the survivor.
        duplicate = Skill.objects.create(slug="selenium-webdriver", name="Selenium WebDriver")
        skill_service.merge(source=duplicate, target=self.skill)

        with self.assertRaises(InvalidState):
            skill_service.reinstate(skill=duplicate)

    def test_an_active_skill_cannot_be_reinstated(self):
        with self.assertRaises(InvalidState):
            skill_service.reinstate(skill=self.skill)


class MergeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.target = Skill.objects.get(slug="postgresql")
        self.source = Skill.objects.create(slug="postgre-sql", name="Postgre SQL")

    def test_the_merged_skill_survives_and_forwards(self):
        # Deleting it would orphan every alias, relation and CandidateSkill
        # that pointed at it, and make the mistake unrecoverable.
        skill_service.merge(source=self.source, target=self.target)
        self.source.refresh_from_db()

        self.assertEqual(self.source.status, SkillStatus.MERGED)
        self.assertEqual(self.source.resolve_current().pk, self.target.pk)

    def test_the_merged_name_becomes_an_alias(self):
        skill_service.merge(source=self.source, target=self.target)

        self.assertEqual(skill_service.resolve("Postgre SQL").skill.pk, self.target.pk)

    def test_aliases_move_to_the_target(self):
        skill_service.add_alias(skill=self.source, alias="pg")
        skill_service.merge(source=self.source, target=self.target)

        self.assertEqual(skill_service.resolve("pg").skill.pk, self.target.pk)

    def test_the_source_name_is_not_re_aliased_if_it_already_resolves(self):
        # "Postgres DB" already means PostgreSQL. Creating a second alias row
        # for it would violate the uniqueness that makes resolution decisive.
        skill_service.add_alias(skill=self.target, alias="Postgre SQL")

        skill_service.merge(source=self.source, target=self.target)

        self.assertEqual(SkillAlias.objects.filter(normalised="postgre sql").count(), 1)

    def test_translations_move_to_the_target(self):
        skill_service.set_translation(skill=self.source, locale="ne", name="पोस्टग्रे")
        skill_service.merge(source=self.source, target=self.target)

        self.assertEqual(self.target.display_name("ne"), "पोस्टग्रे")

    def test_a_translation_the_target_already_has_is_dropped(self):
        skill_service.set_translation(skill=self.target, locale="ne", name="कायम")
        skill_service.set_translation(skill=self.source, locale="ne", name="हटाइयो")

        skill_service.merge(source=self.source, target=self.target)

        self.assertEqual(self.target.translations.filter(locale="ne").count(), 1)
        self.assertEqual(self.target.display_name("ne"), "कायम")

    def test_external_ids_move_to_the_target(self):
        from apps.taxonomy.models import SkillExternalId

        SkillExternalId.objects.create(skill=self.source, source="esco", external_id="S1.2.3")
        skill_service.merge(source=self.source, target=self.target)

        self.assertEqual(
            SkillExternalId.objects.get(source="esco", external_id="S1.2.3").skill_id,
            self.target.pk,
        )

    def test_relations_are_repointed(self):
        sql = Skill.objects.get(slug="sql")
        SkillRelation.objects.create(
            from_skill=self.source, to_skill=sql, kind=RelationKind.IMPLIES, weight=Decimal("0.8")
        )

        skill_service.merge(source=self.source, target=self.target)

        self.assertTrue(
            SkillRelation.objects.filter(
                from_skill=self.target, to_skill=sql, kind=RelationKind.IMPLIES
            ).exists()
        )

    def test_incoming_relations_are_repointed_too(self):
        etl = Skill.objects.get(slug="etl")
        SkillRelation.objects.create(
            from_skill=etl, to_skill=self.source, kind=RelationKind.IMPLIES, weight=Decimal("0.4")
        )

        skill_service.merge(source=self.source, target=self.target)

        self.assertTrue(
            SkillRelation.objects.filter(
                from_skill=etl, to_skill=self.target, kind=RelationKind.IMPLIES
            ).exists()
        )

    def test_an_edge_between_the_two_skills_is_discarded(self):
        # Repointing it would produce a self-edge, which the database refuses
        # and which would mean nothing anyway.
        SkillRelation.objects.create(
            from_skill=self.source,
            to_skill=self.target,
            kind=RelationKind.IMPLIES,
            weight=Decimal("0.9"),
        )

        skill_service.merge(source=self.source, target=self.target)

        self.assertFalse(
            SkillRelation.objects.filter(from_skill=self.target, to_skill=self.target).exists()
        )

    def test_an_edge_pointing_at_the_source_is_discarded_too(self):
        # Mirror of the outgoing case: repointing it would make the target
        # imply itself.
        SkillRelation.objects.create(
            from_skill=self.target,
            to_skill=self.source,
            kind=RelationKind.IMPLIES,
            weight=Decimal("0.9"),
        )

        skill_service.merge(source=self.source, target=self.target)

        self.assertFalse(
            SkillRelation.objects.filter(from_skill=self.target, to_skill=self.target).exists()
        )

    def test_a_duplicate_edge_collapses_instead_of_failing(self):
        sql = Skill.objects.get(slug="sql")
        SkillRelation.objects.create(
            from_skill=self.source, to_skill=sql, kind=RelationKind.IMPLIES, weight=Decimal("0.5")
        )

        skill_service.merge(source=self.source, target=self.target)

        self.assertEqual(
            SkillRelation.objects.filter(
                from_skill=self.target, to_skill=sql, kind=RelationKind.IMPLIES
            ).count(),
            1,
        )

    def test_a_merge_that_would_create_a_cycle_is_refused(self):
        skill_service.merge(source=self.source, target=self.target)

        with self.assertRaises(InvalidState):
            skill_service.merge(source=self.target, target=self.source)

    def test_a_skill_cannot_merge_into_itself(self):
        with self.assertRaises(InvalidState):
            skill_service.merge(source=self.target, target=self.target)

    def test_a_merged_skill_leaves_the_active_set(self):
        skill_service.merge(source=self.source, target=self.target)

        self.assertNotIn(self.source, Skill.objects.active())

    def test_merging_a_chain_forwards_all_the_way(self):
        middle = Skill.objects.create(slug="pg-sql", name="PG SQL")
        skill_service.merge(source=self.source, target=middle)
        skill_service.merge(source=middle, target=self.target)
        self.source.refresh_from_db()

        self.assertEqual(self.source.resolve_current().pk, self.target.pk)

    def test_the_merge_is_audited(self):
        from apps.audit.models import AuditLog

        skill_service.merge(source=self.source, target=self.target)

        self.assertTrue(AuditLog.objects.filter(verb="skill.merged").exists())


class DeletionProtectionTests(TestCase):
    """The database's half of "protect referenced skills" (the admin does the rest)."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_a_replacement_cannot_be_deleted_out_from_under_its_dependants(self):
        from django.db.models import ProtectedError

        angular = Skill.objects.get(slug="angular")

        with self.assertRaises(ProtectedError):
            angular.delete()


class SelectorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_autocomplete_matches_aliases_as_well_as_names(self):
        # Someone typing "JS" must be offered JavaScript rather than an empty
        # list that pushes them to invent a duplicate.
        results = list(selectors.search_skills("js"))

        self.assertIn("javascript", [skill.slug for skill in results])

    def test_autocomplete_excludes_merged_skills(self):
        duplicate = Skill.objects.create(slug="python-lang", name="Python Lang")
        skill_service.merge(source=duplicate, target=Skill.objects.get(slug="python"))

        self.assertNotIn("python-lang", [s.slug for s in selectors.search_skills("python")])

    def test_autocomplete_excludes_deprecated_skills(self):
        self.assertNotIn("angularjs", [s.slug for s in selectors.search_skills("angular")])

    def test_empty_search_returns_nothing(self):
        self.assertEqual(list(selectors.search_skills("  ")), [])

    def test_reference_lists_are_available_to_the_forms_that_need_them(self):
        self.assertIn("django", [skill.slug for skill in selectors.skills_in("frameworks")])
        self.assertTrue(selectors.job_categories().exists())
        self.assertTrue(selectors.industries().exists())
        self.assertTrue(selectors.languages().exists())

    def test_locations_are_scoped_by_country(self):
        nepal = selectors.locations_in("np")

        self.assertTrue(nepal.exists())
        self.assertEqual({location.country for location in nepal}, {"NP"})

    def test_skills_are_findable_by_external_id(self):
        from apps.taxonomy.models import SkillExternalId

        python = Skill.objects.get(slug="python")
        SkillExternalId.objects.create(skill=python, source="esco", external_id="S1.1.1")

        found = selectors.skills_by_external_id("esco", ["S1.1.1", "missing"])

        self.assertEqual(found["S1.1.1"].pk, python.pk)
        self.assertNotIn("missing", found)
