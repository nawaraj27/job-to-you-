"""
Schema validation, prompt loading, budgets, feedback and the eval harness.

The four things the runner leans on, each tested where it lives rather than
through the pipeline — a schema bug found through a run tells you a run failed,
and a schema bug found here tells you which rule is wrong.
"""

from __future__ import annotations

import json
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.ai.models import AIFeedback, AIRun, AIUsageQuota, PromptTemplate, RunStatus, Verdict
from apps.ai.services import evals, feedback, prompts, quotas, schema
from apps.common.errors import InvalidState, NotFound
from apps.companies.tests.factories import company_with_owner

ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string"},
        "findings": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["title", "severity"],
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "score": {"type": "integer", "minimum": 0, "maximum": 10},
                },
            },
        },
    },
}


class SchemaTests(TestCase):
    def check(self, payload, output_schema=ANALYSIS_SCHEMA):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return schema.validate(text, output_schema)

    def test_a_valid_document_passes(self):
        ok, parsed, problem = self.check(
            {"verdict": "Fine", "findings": [{"title": "x", "severity": "low"}]}
        )
        self.assertTrue(ok)
        self.assertEqual(problem, "")
        self.assertEqual(parsed["verdict"], "Fine")

    def test_no_schema_means_the_task_wants_prose(self):
        ok, parsed, _ = schema.validate("Just some words.", None)
        self.assertTrue(ok)
        # Wrapped so every caller reads `run.output` the same way.
        self.assertEqual(parsed, {"text": "Just some words."})

    def test_a_fenced_block_is_unwrapped(self):
        # Models wrap JSON in fences however firmly they are asked not to.
        body = json.dumps({"verdict": "Fine", "findings": [{"title": "x", "severity": "low"}]})
        ok, parsed, _ = self.check(f"Here you go:\n```json\n{body}\n```")
        self.assertTrue(ok)
        self.assertEqual(parsed["verdict"], "Fine")

    def test_a_preamble_before_the_object_is_tolerated(self):
        body = json.dumps({"verdict": "Fine", "findings": [{"title": "x", "severity": "low"}]})
        ok, _, _ = self.check(f"Certainly! {body}")
        self.assertTrue(ok)

    def test_empty_output_is_a_problem(self):
        ok, _, problem = self.check("   ")
        self.assertFalse(ok)
        self.assertIn("nothing", problem)

    def test_broken_json_is_a_problem(self):
        ok, _, problem = self.check("{not json")
        self.assertFalse(ok)
        self.assertIn("Not valid JSON", problem)

    def test_a_missing_required_field_is_named(self):
        ok, _, problem = self.check({"findings": [{"title": "x", "severity": "low"}]})
        self.assertFalse(ok)
        self.assertIn("verdict", problem)

    def test_a_wrong_type_names_the_path(self):
        # "expected an array at findings" is actionable in a prompt review;
        # "invalid output" is not.
        ok, _, problem = self.check({"verdict": "Fine", "findings": "lots"})
        self.assertFalse(ok)
        self.assertIn("findings", problem)
        self.assertIn("array", problem)

    def test_a_nested_problem_names_its_index(self):
        ok, _, problem = self.check(
            {
                "verdict": "Fine",
                "findings": [{"title": "x", "severity": "low"}, {"title": "y"}],
            }
        )
        self.assertFalse(ok)
        self.assertIn("findings[1]", problem)

    def test_an_empty_array_fails_min_items(self):
        ok, _, problem = self.check({"verdict": "Fine", "findings": []})
        self.assertFalse(ok)
        self.assertIn("at least 1", problem)

    def test_a_value_outside_the_enum_is_refused(self):
        ok, _, problem = self.check(
            {"verdict": "Fine", "findings": [{"title": "x", "severity": "catastrophic"}]}
        )
        self.assertFalse(ok)
        self.assertIn("catastrophic", problem)

    def test_a_number_outside_its_range_is_refused(self):
        ok, _, problem = self.check(
            {
                "verdict": "Fine",
                "findings": [{"title": "x", "severity": "low", "score": 99}],
            }
        )
        self.assertFalse(ok)
        self.assertIn("above the maximum", problem)

    def test_a_boolean_is_not_an_integer(self):
        # Python says otherwise; a schema must not.
        ok, _, _ = schema.validate(
            json.dumps({"n": True}), {"type": "object", "properties": {"n": {"type": "integer"}}}
        )
        self.assertFalse(ok)


class PromptLoadingTests(TestCase):
    def write(self, directory: Path, name: str, body: str) -> Path:
        path = directory / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_file_becomes_version_one(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(
                root,
                "resume.analysis.md",
                '---\n{"system": "Be careful.", "schema": {"type": "object"}}\n---\nReview {cv_text}.',
            )
            result = prompts.load_all(directory=root)

        self.assertEqual(result, {"files": 1, "created": 1, "unchanged": 0})
        template = PromptTemplate.objects.get(task_key="resume.analysis")
        self.assertEqual(template.version, 1)
        self.assertEqual(len(template.body_sha256), 64)

    def test_loading_an_unchanged_file_creates_no_new_version(self):
        # A version per deploy would make `AIRun.prompt_version` meaningless
        # and invalidate the cache for nothing.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "resume.analysis.md", "Review {cv_text}.")
            prompts.load_all(directory=root)
            second = prompts.load_all(directory=root)

        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(PromptTemplate.objects.count(), 1)

    def test_an_edited_file_becomes_a_new_version(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "resume.analysis.md", "Review {cv_text}.")
            prompts.load_all(directory=root)
            self.write(root, "resume.analysis.md", "Review {cv_text} carefully.")
            prompts.load_all(directory=root)

        versions = list(
            PromptTemplate.objects.filter(task_key="resume.analysis")
            .order_by("version")
            .values_list("version", flat=True)
        )
        self.assertEqual(versions, [1, 2])

    def test_a_file_with_no_configured_task_is_ignored(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "not.a.task.md", "Hello.")
            result = prompts.load_all(directory=root)

        self.assertEqual(result["created"], 0)
        self.assertEqual(PromptTemplate.objects.count(), 0)

    def test_malformed_front_matter_is_loud(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write(root, "resume.analysis.md", "---\n{not json}\n---\nBody")
            with self.assertRaises(ValueError):
                prompts.load_one(path)

    def test_a_missing_prompt_is_loud_rather_than_defaulted(self):
        # A runner that quietly substituted a default would produce output
        # nobody wrote.
        with self.assertRaises(prompts.PromptMissing):
            prompts.current("resume.analysis")

    def test_the_system_line_survives_into_the_stored_body(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(
                root, "resume.analysis.md", '---\n{"system": "Be careful."}\n---\nReview {cv_text}.'
            )
            prompts.load_all(directory=root)

        template = PromptTemplate.objects.get()
        system, body = prompts.render(template, {"cv_text": "a CV"})
        self.assertEqual(system, "Be careful.")
        self.assertEqual(body, "Review a CV.")

    def test_a_missing_placeholder_renders_blank_rather_than_raising(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "resume.analysis.md", "Review {cv_text} for {role}.")
            prompts.load_all(directory=root)

        template = PromptTemplate.objects.get()
        _, body = prompts.render(template, {"cv_text": "a CV"})
        self.assertEqual(body, "Review a CV for .")

    def test_the_repository_prompts_all_load(self):
        # The real files, parsed for real. A malformed prompt in the repository
        # is a deploy that fails at the worst moment otherwise.
        result = prompts.load_all()
        self.assertGreater(result["files"], 0)
        self.assertEqual(result["files"], result["created"])

    def test_the_management_command_runs(self):
        call_command("load_prompts")
        self.assertTrue(PromptTemplate.objects.exists())

    def test_a_schema_only_edit_is_a_new_version(self):
        # The schema was not compared: making a field required without
        # touching the text was read as "unchanged" and never loaded.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write(
                root, "resume.analysis.md", '---\n{"schema": {"type": "object"}}\n---\nReview.'
            )
            prompts.load_one(path)
            self.write(
                root,
                "resume.analysis.md",
                '---\n{"schema": {"type": "object", "required": ["score"]}}\n---\nReview.',
            )
            prompts.load_one(path)

        latest = (
            PromptTemplate.objects.filter(task_key="resume.analysis").order_by("-version").first()
        )
        self.assertEqual(latest.version, 2)
        self.assertEqual(latest.output_schema["required"], ["score"])

    def test_check_reports_prompts_that_were_not_loaded(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("load_prompts", "--check", stdout=StringIO())
        call_command("load_prompts", stdout=StringIO())
        out = StringIO()
        call_command("load_prompts", "--check", stdout=out)
        self.assertIn("every file matches", out.getvalue())

    def test_check_reports_a_file_edited_after_loading(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "resume.analysis.md", "Review {cv_text}.")
            prompts.load_all(directory=root)
            self.write(root, "resume.analysis.md", "Review {cv_text} carefully.")

            differing = prompts.stale(directory=root)

        self.assertEqual(differing, [("resume.analysis", "text differs from loaded version 1")])


class QuotaTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, _ = company_with_owner(create_user(email="owner@example.test"))

    def test_a_fresh_owner_is_allowed(self):
        allowed, reason = quotas.check(user=self.user)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")

    def test_the_period_row_is_created_on_first_use(self):
        quotas.check(user=self.user)
        self.assertEqual(AIUsageQuota.objects.count(), 1)

    def test_spend_accumulates(self):
        quotas.record(user=self.user, cost=Decimal("0.25"), tokens=100)
        quotas.record(user=self.user, cost=Decimal("0.25"), tokens=100)

        quota = quotas.usage_for(self.user)
        self.assertEqual(quota.cost, Decimal("0.50"))
        self.assertEqual(quota.tokens, 200)
        self.assertEqual(quota.runs, 2)

    def test_an_exhausted_budget_refuses_the_next_run(self):
        quotas.set_limit(owner=self.user, limit=Decimal("0.10"))
        quotas.record(user=self.user, cost=Decimal("0.10"))

        allowed, reason = quotas.check(user=self.user)
        self.assertFalse(allowed)
        self.assertIn("budget", reason)

    def test_a_run_charges_both_the_person_and_the_company(self):
        # Both are true: the person used it and the company paid for it.
        quotas.record(user=self.user, company=self.company, cost=Decimal("0.30"))

        self.assertEqual(quotas.usage_for(self.user).cost, Decimal("0.30"))
        self.assertEqual(quotas.usage_for(self.company).cost, Decimal("0.30"))

    def test_an_exhausted_company_stops_its_members(self):
        quotas.set_limit(owner=self.company, limit=Decimal("0.01"))
        quotas.record(company=self.company, cost=Decimal("0.02"))

        allowed, _ = quotas.check(user=self.user, company=self.company)
        self.assertFalse(allowed)

    def test_a_company_and_a_user_have_separate_ceilings(self):
        self.assertNotEqual(
            quotas.usage_for(self.user).limit_cost,
            quotas.usage_for(self.company).limit_cost,
        )

    def test_remaining_never_goes_negative(self):
        quotas.set_limit(owner=self.user, limit=Decimal("0.10"))
        quotas.record(user=self.user, cost=Decimal("5.00"))
        self.assertEqual(quotas.usage_for(self.user).remaining, Decimal("0"))


class FeedbackTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.run = AIRun.objects.create(
            task_key="resume.analysis", input_hash="a" * 64, status=RunStatus.OK, user=self.user
        )

    def test_a_verdict_is_recorded(self):
        record = feedback.record(run=self.run, user=self.user, verdict=Verdict.GOOD)
        self.assertEqual(record.verdict, Verdict.GOOD)

    def test_changing_your_mind_replaces_rather_than_duplicates(self):
        feedback.record(run=self.run, user=self.user, verdict=Verdict.GOOD)
        feedback.record(run=self.run, user=self.user, verdict=Verdict.BAD, comment="Wrong")

        self.assertEqual(AIFeedback.objects.count(), 1)
        self.assertEqual(AIFeedback.objects.get().verdict, Verdict.BAD)

    def test_an_invented_verdict_is_refused(self):
        with self.assertRaises(InvalidState):
            feedback.record(run=self.run, user=self.user, verdict="magnificent")

    def test_somebody_elses_run_is_not_found(self):
        # The run carries the input snapshot, which is that person's own CV.
        stranger = create_user(email="stranger@example.test")
        with self.assertRaises(NotFound):
            feedback.run_for(user=stranger, public_id=str(self.run.public_id))

    def test_your_own_run_is_found(self):
        found = feedback.run_for(user=self.user, public_id=str(self.run.public_id))
        self.assertEqual(found, self.run)

    def test_the_summary_counts_the_corpus(self):
        feedback.record(run=self.run, user=self.user, verdict=Verdict.BAD)
        self.assertEqual(feedback.summary(), {"good": 0, "bad": 1, "total": 1})


class EvalHarnessTests(TestCase):
    """The assertions, not the model. Structural on purpose: asserting exact
    model output produces a suite that fails on every harmless rewording and is
    switched off within a month."""

    def test_a_matching_output_passes(self):
        failures = evals.check(
            {"verdict": "Fine", "findings": [{"title": "x", "detail": "y", "severity": "high"}]},
            {
                "has": ["verdict"],
                "min_items": {"findings": 1},
                "each_has": {"findings": ["title", "detail"]},
            },
        )
        self.assertEqual(failures, [])

    def test_a_missing_field_fails(self):
        failures = evals.check({"findings": []}, {"has": ["verdict"]})
        self.assertEqual(len(failures), 1)
        self.assertIn("verdict", failures[0])

    def test_too_few_items_fails(self):
        failures = evals.check({"findings": [1]}, {"min_items": {"findings": 3}})
        self.assertIn("at least 3", failures[0])

    def test_a_required_term_that_is_absent_fails(self):
        failures = evals.check({"skills": [{"term": "Django"}]}, {"contains": ["django"]})
        self.assertEqual(failures, [])

        failures = evals.check({"skills": [{"term": "Django"}]}, {"contains": ["rust"]})
        self.assertEqual(len(failures), 1)

    def test_a_forbidden_term_that_appears_fails(self):
        # The regression guard: a prompt that starts inventing skills.
        failures = evals.check(
            {"skills": [{"term": "Django"}, {"term": "PostgreSQL"}]},
            {"absent": ["postgresql"]},
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("should not appear", failures[0])

    def test_each_has_names_the_offending_index(self):
        failures = evals.check(
            {"findings": [{"title": "a", "detail": "b"}, {"title": "c"}]},
            {"each_has": {"findings": ["title", "detail"]}},
        )
        self.assertIn("findings[1]", failures[0])

    def test_a_dotted_path_reaches_into_nested_output(self):
        failures = evals.check({"a": {"b": "c"}}, {"has": ["a.b"]})
        self.assertEqual(failures, [])

    def test_the_repository_cases_are_well_formed(self):
        # A malformed case file is a CI step that passes because it ran nothing.
        cases = evals.load_cases()
        self.assertGreater(len(cases), 0)
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertIn("task_key", case)
                self.assertIn("inputs", case)
                self.assertTrue(case.get("expect"))
