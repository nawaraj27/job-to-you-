"""
Taxonomy import (SRS §5.3).

An import writes the table every match in the system reads, so the properties
under test are not "does it load rows" but "what happens when it goes wrong":
run it twice, kill it halfway, feed it a row that collides with curated data.
Each of those has one right answer and several plausible wrong ones.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.taxonomy.importers import CsvSource, SkillRecord, run_import
from apps.taxonomy.importers.base import TaxonomySource
from apps.taxonomy.importers.registry import available_formats, get_format
from apps.taxonomy.models import (
    ImportRun,
    RelationKind,
    Skill,
    SkillAlias,
    SkillExternalId,
    SkillRelation,
    SkillTranslation,
)
from apps.taxonomy.services import skills as skill_service


class ListSource(TaxonomySource):
    """A source backed by a list, for tests that are not about file parsing."""

    def __init__(self, records: list[SkillRecord], *, source: str = "test"):
        self._records = sorted(records, key=lambda r: r.external_id)
        self.source = source
        self.passes = 0

    def records(self, *, after: str = ""):
        self.passes += 1
        for record in self._records:
            if record.external_id > after:
                yield record


class ExplodingSource(ListSource):
    """Fails partway through, once."""

    def __init__(self, records, *, fail_after: str, source: str = "test"):
        super().__init__(records, source=source)
        self.fail_after = fail_after
        self.armed = True

    def records(self, *, after: str = ""):
        for record in super().records(after=after):
            if self.armed and record.external_id > self.fail_after:
                self.armed = False
                raise RuntimeError("connection reset")
            yield record


def _records(count: int, *, prefix: str = "S") -> list[SkillRecord]:
    return [
        SkillRecord(external_id=f"{prefix}{index:03d}", name=f"Imported skill {index}")
        for index in range(count)
    ]


def _csv_file(text: str) -> Path:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".csv", delete=False, encoding="utf-8", newline=""
    ) as handle:
        handle.write(text)
    return Path(handle.name)


class RecordContractTests(TestCase):
    def test_a_record_needs_an_identity(self):
        with self.assertRaises(ValueError):
            SkillRecord(external_id="  ", name="Something")

    def test_a_record_needs_a_name(self):
        with self.assertRaises(ValueError):
            SkillRecord(external_id="S1", name="  ")


class ImportTests(TestCase):
    def test_records_become_skills_with_their_external_identity(self):
        source = ListSource(
            [
                SkillRecord(
                    external_id="S1",
                    name="Welding",
                    description="Joining metal.",
                    category="Manufacturing",
                    aliases=("arc welding",),
                    translations={"ne": "वेल्डिङ"},
                    uri="http://example.invalid/S1",
                )
            ]
        )

        summary = run_import(source)

        skill = Skill.objects.get(name="Welding")
        self.assertEqual(summary.created, 1)
        self.assertEqual(skill.description, "Joining metal.")
        self.assertEqual(skill.category.name, "Manufacturing")
        self.assertEqual(skill.display_name("ne"), "वेल्डिङ")
        self.assertEqual(skill_service.resolve("arc welding").skill.pk, skill.pk)
        self.assertTrue(
            SkillExternalId.objects.filter(source="test", external_id="S1", skill=skill).exists()
        )

    def test_running_the_same_import_twice_changes_nothing(self):
        source = ListSource(_records(20))

        run_import(source)
        before = (Skill.objects.count(), SkillAlias.objects.count())
        second = run_import(ListSource(_records(20)))
        after = (Skill.objects.count(), SkillAlias.objects.count())

        self.assertEqual(before, after)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.skipped, 20)

    def test_an_upstream_rename_updates_rather_than_forks(self):
        run_import(ListSource([SkillRecord(external_id="S1", name="Welding")]))

        run_import(
            ListSource(
                [SkillRecord(external_id="S1", name="Welding (renamed)", description="Metal.")]
            )
        )

        # One skill, keyed on the external id — and still called what the
        # curator sees, because an import may fill blanks but never rename.
        self.assertEqual(Skill.objects.filter(name__startswith="Welding").count(), 1)
        self.assertEqual(Skill.objects.get(name="Welding").description, "Metal.")

    def test_an_import_adopts_an_existing_skill_instead_of_duplicating_it(self):
        call_command("seed_taxonomy", quiet_summary=True)
        python = Skill.objects.get(slug="python")

        run_import(ListSource([SkillRecord(external_id="S1", name="Python")]))

        self.assertEqual(SkillExternalId.objects.get(external_id="S1").skill_id, python.pk)
        self.assertEqual(Skill.objects.filter(name="Python").count(), 1)

    def test_an_import_matches_on_a_curated_alias(self):
        call_command("seed_taxonomy", quiet_summary=True)

        run_import(ListSource([SkillRecord(external_id="S1", name="ECMAScript")]))

        self.assertEqual(SkillExternalId.objects.get(external_id="S1").skill.slug, "javascript")

    def test_an_import_never_steals_a_claimed_alias(self):
        # "JS" means JavaScript. An export that claims it for something else
        # must not silently change what every existing profile says.
        call_command("seed_taxonomy", quiet_summary=True)

        run_import(ListSource([SkillRecord(external_id="S1", name="Jasmine", aliases=("js",))]))

        self.assertEqual(skill_service.resolve("js").skill.slug, "javascript")

    def test_an_import_never_overwrites_a_curated_description(self):
        skill = Skill.objects.create(slug="welding", name="Welding", description="Curated.")

        run_import(
            ListSource([SkillRecord(external_id="S1", name="Welding", description="From ESCO.")])
        )
        skill.refresh_from_db()

        self.assertEqual(skill.description, "Curated.")

    def test_an_id_pointing_at_a_merged_skill_follows_the_merge(self):
        run_import(ListSource([SkillRecord(external_id="S1", name="Welding")]))
        source_skill = Skill.objects.get(name="Welding")
        survivor = Skill.objects.create(slug="metal-welding", name="Metal welding")
        skill_service.merge(source=source_skill, target=survivor)

        run_import(ListSource([SkillRecord(external_id="S1", name="Welding")]))

        self.assertEqual(SkillExternalId.objects.get(external_id="S1").skill_id, survivor.pk)

    def test_colliding_names_get_distinct_slugs(self):
        Skill.objects.create(slug="welding", name="Welding (curated)")

        run_import(ListSource([SkillRecord(external_id="S1", name="Welding")]))

        self.assertEqual(Skill.objects.filter(slug__startswith="welding").count(), 2)


class HierarchyLinkingTests(TestCase):
    def test_broader_ids_become_edges_in_both_directions(self):
        source = ListSource(
            [
                SkillRecord(external_id="S1", name="Manual work"),
                SkillRecord(external_id="S2", name="Welding", broader=("S1",)),
            ]
        )

        summary = run_import(source)

        child = Skill.objects.get(name="Welding")
        parent = Skill.objects.get(name="Manual work")
        self.assertEqual(summary.linked, 2)
        self.assertIn(parent.pk, skill_service.ancestors(child))
        self.assertIn(child.pk, skill_service.descendants(parent))

    def test_a_parent_declared_after_its_child_still_links(self):
        # The reason linking is a second pass at all.
        source = ListSource(
            [
                SkillRecord(external_id="S1", name="Welding", broader=("S2",)),
                SkillRecord(external_id="S2", name="Manual work"),
            ]
        )

        run_import(source)

        self.assertIn(
            Skill.objects.get(name="Manual work").pk,
            skill_service.ancestors(Skill.objects.get(name="Welding")),
        )

    def test_a_broader_id_the_export_never_shipped_is_skipped_not_invented(self):
        run_import(ListSource([SkillRecord(external_id="S1", name="Welding", broader=("S99",))]))

        self.assertEqual(SkillRelation.objects.count(), 0)

    def test_relinking_the_same_hierarchy_adds_nothing(self):
        records = [
            SkillRecord(external_id="S1", name="Manual work"),
            SkillRecord(external_id="S2", name="Welding", broader=("S1",)),
        ]
        run_import(ListSource(records))
        before = SkillRelation.objects.count()

        run_import(ListSource(records))

        self.assertEqual(SkillRelation.objects.count(), before)


class ResumptionTests(TestCase):
    def test_a_failed_run_is_recorded_with_its_checkpoint(self):
        source = ExplodingSource(_records(30), fail_after="S009")

        with self.assertRaises(RuntimeError):
            run_import(source, batch_size=5)

        run = ImportRun.objects.get()
        self.assertEqual(run.state, ImportRun.State.FAILED)
        self.assertEqual(run.last_key, "S009")
        self.assertIn("connection reset", run.error)

    def test_a_resumed_run_continues_where_it_stopped(self):
        records = _records(30)
        with self.assertRaises(RuntimeError):
            run_import(ExplodingSource(records, fail_after="S009"), batch_size=5)

        summary = run_import(ListSource(records), batch_size=5)

        self.assertEqual(summary.resumed_from, "S009")
        self.assertEqual(ImportRun.objects.count(), 1)
        self.assertEqual(Skill.objects.count(), 30)
        # Ten committed before the failure, twenty after: no row imported twice.
        self.assertEqual(summary.created, 30)

    def test_a_resumed_run_that_finishes_is_marked_completed(self):
        records = _records(12)
        with self.assertRaises(RuntimeError):
            run_import(ExplodingSource(records, fail_after="S004"), batch_size=3)

        run_import(ListSource(records), batch_size=3)

        self.assertEqual(ImportRun.objects.get().state, ImportRun.State.COMPLETED)

    def test_restarting_ignores_the_checkpoint_and_stays_idempotent(self):
        records = _records(12)
        with self.assertRaises(RuntimeError):
            run_import(ExplodingSource(records, fail_after="S004"), batch_size=3)

        summary = run_import(ListSource(records), resume=False, batch_size=3)

        self.assertEqual(summary.resumed_from, "")
        self.assertEqual(ImportRun.objects.count(), 2)
        self.assertEqual(Skill.objects.count(), 12)

    def test_a_second_import_of_the_same_source_starts_a_new_run(self):
        run_import(ListSource(_records(3)))
        run_import(ListSource(_records(3)))

        self.assertEqual(ImportRun.objects.count(), 2)

    def test_a_source_that_ignores_the_resume_hint_is_filtered_anyway(self):
        class ForgetfulSource(ListSource):
            def records(self, *, after: str = ""):
                yield from self._records

        records = _records(10)
        with self.assertRaises(RuntimeError):
            run_import(ExplodingSource(records, fail_after="S003"), batch_size=2)

        summary = run_import(ForgetfulSource(records), batch_size=2)

        self.assertEqual(Skill.objects.count(), 10)
        self.assertEqual(summary.processed, 10)

    def test_the_relation_phase_resumes_separately_from_the_record_phase(self):
        source = ListSource(_records(6))
        run_import(source)

        # Two passes over the source: skills, then hierarchy.
        self.assertEqual(source.passes, 2)
        self.assertEqual(ImportRun.objects.get().phase, ImportRun.Phase.RELATIONS)


class DryRunTests(TestCase):
    def test_a_dry_run_reports_without_writing(self):
        summary = run_import(ListSource(_records(5)), dry_run=True)

        self.assertEqual(summary.created, 5)
        self.assertTrue(summary.dry_run)
        self.assertIsNone(summary.run_id)
        self.assertEqual(Skill.objects.count(), 0)
        self.assertEqual(ImportRun.objects.count(), 0)


class CsvSourceTests(TestCase):
    def _write(self, text: str) -> Path:
        path = _csv_file(text)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_an_esco_shaped_export_loads_unedited(self):
        path = self._write(
            "conceptUri,preferredLabel,altLabels,description\n"
            "http://esco/1,Welding,arc welding|gas welding,Joining metal.\n"
        )

        run_import(CsvSource(path=path, source="esco"))

        skill = Skill.objects.get(name="Welding")
        self.assertEqual(skill.description, "Joining metal.")
        self.assertEqual(skill.aliases.count(), 2)

    def test_rows_are_ordered_by_external_id(self):
        path = self._write("external_id,name\nS003,Third\nS001,First\nS002,Second\n")

        ids = [record.external_id for record in CsvSource(path=path, source="x").records()]

        self.assertEqual(ids, ["S001", "S002", "S003"])

    def test_a_presorted_file_streams_in_file_order(self):
        path = self._write("external_id,name\nS001,First\nS002,Second\n")

        source = CsvSource(path=path, source="x", sort_in_memory=False)

        self.assertEqual([r.external_id for r in source.records()], ["S001", "S002"])

    def test_translations_and_hierarchy_parse_from_pipes(self):
        path = self._write(
            "external_id,name,translations,broader\n"
            "S001,Manual work,,\n"
            "S002,Welding,ne=वेल्डिङ|hi=वेल्डिंग,S001\n"
        )

        run_import(CsvSource(path=path, source="x"))

        self.assertEqual(SkillTranslation.objects.filter(skill__name="Welding").count(), 2)
        self.assertTrue(SkillRelation.objects.filter(kind=RelationKind.BROADER).exists())

    def test_a_row_without_an_identity_is_skipped_not_fatal(self):
        path = self._write("external_id,name\n,Nameless\nS001,Welding\n")

        summary = run_import(CsvSource(path=path, source="x"))

        self.assertEqual(summary.created, 1)

    def test_a_file_without_the_required_columns_is_refused(self):
        path = self._write("something,else\n1,2\n")

        with self.assertRaises(ValueError) as caught:
            list(CsvSource(path=path, source="x").records())

        self.assertIn("external_id", str(caught.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(ValueError):
            list(CsvSource(path="does-not-exist.csv", source="x").records())


class RegistryTests(TestCase):
    def test_csv_is_registered(self):
        self.assertIn("csv", available_formats())

    def test_an_unknown_format_names_what_is_available(self):
        with self.assertRaises(ValueError) as caught:
            get_format("parquet", {})

        self.assertIn("csv", str(caught.exception))

    def test_the_csv_format_needs_a_path(self):
        with self.assertRaises(ValueError):
            get_format("csv", {"source": "esco"})


class CommandTests(TestCase):
    def _write(self, text: str) -> Path:
        path = _csv_file(text)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_the_command_imports_a_file(self):
        path = self._write("external_id,name\nS001,Welding\n")

        call_command("import_taxonomy", "--source", "esco", "--path", str(path))

        self.assertTrue(Skill.objects.filter(name="Welding").exists())
        self.assertEqual(ImportRun.objects.get().source, "esco")

    def test_a_dry_run_writes_nothing(self):
        path = self._write("external_id,name\nS001,Welding\n")

        call_command("import_taxonomy", "--source", "esco", "--path", str(path), "--dry-run")

        self.assertFalse(Skill.objects.exists())

    def test_a_missing_path_fails_the_command(self):
        with self.assertRaises(CommandError):
            call_command("import_taxonomy", "--source", "esco")

    def test_a_missing_file_fails_the_command(self):
        with self.assertRaises(CommandError):
            call_command("import_taxonomy", "--source", "esco", "--path", "nope.csv")
