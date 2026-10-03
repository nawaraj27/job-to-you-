"""
The unresolved-term ledger, provenance, and skill kinds (ADR-A46/A47/A48).

All three exist for the same reason: information that is cheap to record now
and impossible to reconstruct later. The tests are written against that — what
survives, what is distinguishable afterwards, and what an undo may touch.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.tests.factories import create_staff
from apps.common.errors import InvalidState
from apps.taxonomy.models import (
    Provenance,
    RelationKind,
    Skill,
    SkillAlias,
    SkillKind,
    SkillRelation,
    TermSource,
    TermStatus,
    UnresolvedTerm,
)
from apps.taxonomy.services import skills as skill_service
from apps.taxonomy.services import terms as term_service


class RecordingTests(TestCase):
    def test_an_unknown_term_is_kept_with_its_spelling(self):
        row = term_service.record("Kubeflow", source=TermSource.PROFILE)

        self.assertEqual(row.term, "Kubeflow")
        self.assertEqual(row.normalised, "kubeflow")
        self.assertEqual(row.occurrences, 1)
        self.assertEqual(row.status, TermStatus.PENDING)

    def test_the_same_term_twice_counts_rather_than_duplicates(self):
        term_service.record("Kubeflow", source=TermSource.PROFILE)
        row = term_service.record("  kubeflow  ", source=TermSource.RESUME)

        self.assertEqual(UnresolvedTerm.objects.count(), 1)
        self.assertEqual(row.occurrences, 2)

    def test_per_channel_evidence_is_kept_separately(self):
        # Fifty candidates typing it and one model extracting it are not
        # equally strong arguments for adding a skill.
        term_service.record("Kubeflow", source=TermSource.PROFILE)
        term_service.record("Kubeflow", source=TermSource.PROFILE)
        row = term_service.record("Kubeflow", source=TermSource.AI_EXTRACTION)

        self.assertEqual(row.source_counts, {"profile": 2, "ai_extraction": 1})
        self.assertEqual(row.source, TermSource.AI_EXTRACTION)

    def test_the_same_spelling_in_two_locales_is_two_gaps(self):
        term_service.record("लेखा परीक्षण", source=TermSource.PROFILE, locale="ne")
        term_service.record("audit trail thing", source=TermSource.PROFILE, locale="en")

        self.assertEqual(UnresolvedTerm.objects.count(), 2)

    def test_empty_input_is_not_recorded(self):
        self.assertIsNone(term_service.record("   ", source=TermSource.PROFILE))

    def test_a_whole_sentence_is_not_recorded(self):
        # Resume parsing will hand over bullet points; a queue full of
        # paragraphs is a queue nobody reads.
        long_text = "responsible for " + ("delivery " * 40)

        self.assertIsNone(term_service.record(long_text, source=TermSource.RESUME))

    def test_an_extractor_confidence_is_kept(self):
        row = term_service.record("Kubeflow", source=TermSource.AI_EXTRACTION, confidence=0.42)

        self.assertEqual(row.confidence, Decimal("0.42"))

    def test_the_queue_is_ordered_by_demand(self):
        term_service.record("rare thing", source=TermSource.PROFILE)
        for _ in range(3):
            term_service.record("common thing", source=TermSource.PROFILE)

        self.assertEqual(term_service.pending()[0].term, "common thing")


class ResolutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)
        cls.curator = create_staff()

    def setUp(self):
        self.term = term_service.record("Postgres DB", source=TermSource.PROFILE)
        self.skill = Skill.objects.get(slug="postgresql")

    def test_resolving_maps_the_term_and_teaches_resolution(self):
        term_service.resolve(term=self.term, skill=self.skill, actor=self.curator)
        self.term.refresh_from_db()

        self.assertEqual(self.term.status, TermStatus.RESOLVED)
        self.assertEqual(self.term.resolved_skill_id, self.skill.pk)
        # The point of the queue: the next person who types it now lands.
        self.assertEqual(skill_service.resolve("Postgres DB").skill.pk, self.skill.pk)

    def test_the_alias_is_attributed_to_the_curator(self):
        term_service.resolve(term=self.term, skill=self.skill, actor=self.curator)

        alias = SkillAlias.objects.get(normalised="postgres db")
        self.assertEqual(alias.source, Provenance.CURATED)
        self.assertEqual(alias.created_by_id, self.curator.pk)
        self.assertIn("unknown-term queue", alias.created_reason)

    def test_resolving_without_the_alias_leaves_resolution_alone(self):
        term_service.resolve(term=self.term, skill=self.skill, actor=self.curator, add_alias=False)

        self.assertIsNone(skill_service.resolve("Postgres DB"))

    def test_resolving_to_a_merged_skill_follows_the_merge(self):
        duplicate = Skill.objects.create(slug="pg", name="PG")
        skill_service.merge(source=duplicate, target=self.skill)

        term_service.resolve(term=self.term, skill=duplicate, actor=self.curator)
        self.term.refresh_from_db()

        self.assertEqual(self.term.resolved_skill_id, self.skill.pk)

    def test_a_term_whose_alias_is_taken_still_resolves_and_says_so(self):
        # "JS" belongs to JavaScript. Resolving a term onto Java must not steal
        # it, and must not leave the curator wondering what happened.
        taken = term_service.record("JS", source=TermSource.PROFILE)

        term_service.resolve(term=taken, skill=Skill.objects.get(slug="java"), actor=self.curator)
        taken.refresh_from_db()

        self.assertEqual(taken.status, TermStatus.RESOLVED)
        self.assertIn("alias not added", taken.resolution_note)
        self.assertEqual(skill_service.resolve("JS").skill.slug, "javascript")

    def test_resolving_twice_is_refused(self):
        term_service.resolve(term=self.term, skill=self.skill, actor=self.curator)

        with self.assertRaises(InvalidState):
            term_service.resolve(term=self.term, skill=self.skill, actor=self.curator)

    def test_rejecting_keeps_the_row_so_the_judgement_is_not_remade(self):
        # "asap" and "team player" arrive thousands of times.
        term_service.reject(term=self.term, actor=self.curator, note="Not a skill.")
        self.term.refresh_from_db()

        self.assertEqual(self.term.status, TermStatus.REJECTED)
        self.assertTrue(UnresolvedTerm.objects.filter(pk=self.term.pk).exists())
        self.assertNotIn(self.term, term_service.pending())

    def test_a_resolved_term_cannot_be_rejected_without_reopening(self):
        term_service.resolve(term=self.term, skill=self.skill, actor=self.curator)

        with self.assertRaises(InvalidState):
            term_service.reject(term=self.term, actor=self.curator)

    def test_reopening_returns_it_to_the_queue(self):
        term_service.reject(term=self.term, actor=self.curator)
        term_service.reopen(term=self.term, actor=self.curator)
        self.term.refresh_from_db()

        self.assertEqual(self.term.status, TermStatus.PENDING)
        self.assertIsNone(self.term.resolved_skill_id)

    def test_reopening_something_already_pending_is_refused(self):
        with self.assertRaises(InvalidState):
            term_service.reopen(term=self.term, actor=self.curator)

    def test_resolution_is_audited(self):
        from apps.audit.models import AuditLog

        term_service.resolve(term=self.term, skill=self.skill, actor=self.curator)

        self.assertTrue(AuditLog.objects.filter(verb="taxonomy.term_resolved").exists())


class ProfileIntegrationTests(TestCase):
    """The path that produces most of the queue (ADR-A46)."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_a_refused_profile_skill_lands_in_the_queue(self):
        from apps.candidates.services import skills as candidate_skills
        from apps.candidates.tests.factories import create_profile

        profile = create_profile()
        with self.assertRaises(candidate_skills.UnknownSkill):
            candidate_skills.add_skill(
                profile=profile, actor=profile.user, text="Kubeflow Pipelines"
            )

        row = UnresolvedTerm.objects.get(normalised="kubeflow pipelines")
        self.assertEqual(row.source, TermSource.PROFILE)

    def test_once_a_curator_resolves_it_the_candidate_can_add_it(self):
        from apps.candidates.services import skills as candidate_skills
        from apps.candidates.tests.factories import create_profile

        profile = create_profile()
        with self.assertRaises(candidate_skills.UnknownSkill):
            candidate_skills.add_skill(profile=profile, actor=profile.user, text="Postgres DB")

        term_service.resolve(
            term=UnresolvedTerm.objects.get(normalised="postgres db"),
            skill=Skill.objects.get(slug="postgresql"),
            actor=create_staff(),
        )

        record = candidate_skills.add_skill(profile=profile, actor=profile.user, text="Postgres DB")
        self.assertEqual(record.skill.slug, "postgresql")


class ProvenanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)
        cls.curator = create_staff()

    def test_the_seed_is_marked_curated(self):
        self.assertFalse(SkillAlias.objects.exclude(source=Provenance.CURATED).exists())
        self.assertFalse(SkillRelation.objects.exclude(source=Provenance.CURATED).exists())

    def test_an_alias_records_who_asserted_it_and_why(self):
        alias = skill_service.add_alias(
            skill=Skill.objects.get(slug="python"),
            alias="CPython",
            actor=self.curator,
            reason="Common spelling in job ads.",
        )

        self.assertEqual(alias.source, Provenance.CURATED)
        self.assertEqual(alias.created_by_id, self.curator.pk)
        self.assertEqual(alias.created_reason, "Common spelling in job ads.")
        self.assertFalse(alias.is_machine_written)

    def test_an_inferred_relation_is_distinguishable_from_a_curated_one(self):
        relation = skill_service.add_relation(
            from_skill=Skill.objects.get(slug="redis"),
            to_skill=Skill.objects.get(slug="python"),
            kind=RelationKind.RELATED,
            source=Provenance.INFERRED,
            confidence=Decimal("0.61"),
            reason="Co-occurrence, 2026-08 batch.",
            batch="infer-2026-08",
        )

        self.assertTrue(relation.is_machine_written)
        self.assertEqual(relation.confidence, Decimal("0.61"))

    def test_a_self_edge_is_refused(self):
        python = Skill.objects.get(slug="python")

        with self.assertRaises(InvalidState):
            skill_service.add_relation(
                from_skill=python, to_skill=python, kind=RelationKind.RELATED
            )

    def test_an_unknown_relation_kind_is_refused(self):
        with self.assertRaises(InvalidState):
            skill_service.add_relation(
                from_skill=Skill.objects.get(slug="redis"),
                to_skill=Skill.objects.get(slug="python"),
                kind="reminds_me_of",
            )

    def test_adding_the_same_relation_twice_keeps_the_first_provenance(self):
        first = skill_service.add_relation(
            from_skill=Skill.objects.get(slug="redis"),
            to_skill=Skill.objects.get(slug="python"),
            kind=RelationKind.RELATED,
            source=Provenance.CURATED,
        )
        second = skill_service.add_relation(
            from_skill=Skill.objects.get(slug="redis"),
            to_skill=Skill.objects.get(slug="python"),
            kind=RelationKind.RELATED,
            source=Provenance.INFERRED,
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.source, Provenance.CURATED)


class BatchRevertTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)
        cls.curator = create_staff()

    def setUp(self):
        self.batch = "infer-2026-08"
        skill_service.add_relation(
            from_skill=Skill.objects.get(slug="redis"),
            to_skill=Skill.objects.get(slug="python"),
            kind=RelationKind.RELATED,
            source=Provenance.INFERRED,
            batch=self.batch,
        )
        skill_service.add_alias(
            skill=Skill.objects.get(slug="redis"),
            alias="redis cache",
            source=Provenance.AI_GENERATED,
            batch=self.batch,
        )

    def test_a_batch_can_be_undone_whole(self):
        counts = skill_service.revert_batch(batch=self.batch, actor=self.curator)

        self.assertEqual(counts, {"aliases": 1, "relations": 1, "kept": 0})
        self.assertFalse(SkillAlias.objects.filter(batch=self.batch).exists())
        self.assertFalse(SkillRelation.objects.filter(batch=self.batch).exists())

    def test_curated_rows_in_the_same_batch_survive(self):
        # An undo that deletes someone's deliberate work is worse than no undo.
        skill_service.add_alias(
            skill=Skill.objects.get(slug="redis"),
            alias="redis store",
            actor=self.curator,
            source=Provenance.CURATED,
            batch=self.batch,
        )

        counts = skill_service.revert_batch(batch=self.batch, actor=self.curator)

        self.assertEqual(counts["kept"], 1)
        self.assertTrue(SkillAlias.objects.filter(normalised="redis store").exists())

    def test_another_batch_is_untouched(self):
        skill_service.add_alias(
            skill=Skill.objects.get(slug="redis"),
            alias="redis db",
            source=Provenance.INFERRED,
            batch="infer-2026-09",
        )

        skill_service.revert_batch(batch=self.batch, actor=self.curator)

        self.assertTrue(SkillAlias.objects.filter(batch="infer-2026-09").exists())

    def test_reverting_nothing_is_refused(self):
        with self.assertRaises(InvalidState):
            skill_service.revert_batch(batch="  ", actor=self.curator)

    def test_the_revert_is_audited(self):
        from apps.audit.models import AuditLog

        skill_service.revert_batch(batch=self.batch, actor=self.curator)

        self.assertTrue(AuditLog.objects.filter(verb="taxonomy.batch_reverted").exists())


class SkillKindTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def test_the_seed_classifies_every_skill(self):
        self.assertFalse(Skill.objects.filter(kind=SkillKind.UNKNOWN).exists())

    def test_kind_is_independent_of_category(self):
        # Python and Django are both engineering; one is a language and the
        # other a framework, and the matcher will weight them differently.
        python = Skill.objects.get(slug="python")
        django = Skill.objects.get(slug="django")

        self.assertEqual(python.kind, SkillKind.PROGRAMMING_LANGUAGE)
        self.assertEqual(django.kind, SkillKind.FRAMEWORK)

    def test_an_override_beats_its_category_default(self):
        # Docker sits in infrastructure, whose default is "platform".
        self.assertEqual(Skill.objects.get(slug="docker").kind, SkillKind.TOOL)

    def test_soft_skills_are_classified_as_such(self):
        self.assertEqual(Skill.objects.get(slug="teamwork").kind, SkillKind.SOFT_SKILL)

    def test_an_imported_skill_starts_unclassified_rather_than_guessed(self):
        from apps.taxonomy.importers import SkillRecord, run_import
        from apps.taxonomy.tests.test_importers import ListSource

        run_import(ListSource([SkillRecord(external_id="S1", name="Welding")]))

        self.assertEqual(Skill.objects.get(name="Welding").kind, SkillKind.UNKNOWN)

    def test_an_import_may_classify_but_never_reclassify(self):
        from apps.taxonomy.importers import SkillRecord, run_import
        from apps.taxonomy.tests.test_importers import ListSource

        run_import(ListSource([SkillRecord(external_id="S1", name="Welding", kind="hard_skill")]))
        self.assertEqual(Skill.objects.get(name="Welding").kind, SkillKind.HARD_SKILL)

        run_import(ListSource([SkillRecord(external_id="S1", name="Welding", kind="soft_skill")]))
        self.assertEqual(Skill.objects.get(name="Welding").kind, SkillKind.HARD_SKILL)

    def test_an_import_marks_its_aliases_as_imported(self):
        from apps.taxonomy.importers import SkillRecord, run_import
        from apps.taxonomy.tests.test_importers import ListSource

        run_import(
            ListSource([SkillRecord(external_id="S1", name="Welding", aliases=("arc welding",))])
        )

        alias = SkillAlias.objects.get(normalised="arc welding")
        self.assertEqual(alias.source, Provenance.IMPORTED)
        self.assertEqual(alias.batch, "import:test")
