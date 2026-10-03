"""
The analysis service (FR-4.1, FR-4.8, FR-4.9) and its portal.

The properties this phase is answerable for, one class each:

  * a report is attached to an immutable version, and analysing twice does not
    manufacture versions (FR-4.1, SRS §5.5.1);
  * **the report survives every AI failure** — switched off, out of budget,
    provider down, schema error — with the score and the checks intact, which
    is what SRS §9.3.6 asks of an AI feature;
  * **no analysis path ever mutates a CV.** A fix is a `ResumeSuggestion`, and
    only an accept writes the draft (INV-3);
  * contact details never leave for a provider (SRS §9.3.7);
  * another candidate's report is `NotFound`, never `NotAllowed`.

Everything runs against `FakeAdapter` through the real P11 runner. Nothing here
patches `runner.run`: the point under test is that this feature uses the shared
pipeline, and a mocked runner would prove the opposite.
"""

from __future__ import annotations

import datetime as dt
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.ai.models import AIRun, RunStatus
from apps.ai.providers.base import ProviderFailure
from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts
from apps.billing.tests import factories as billing_factories
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotFound
from apps.core import features
from apps.core.models import FeatureFlag
from apps.notifications.models import OutboxEvent
from apps.resumes import content as content_rules
from apps.resumes import policies, selectors
from apps.resumes.analysis import ALGORITHM_VERSION
from apps.resumes.analysis import target as target_rules
from apps.resumes.models import (
    EnrichmentState,
    ResumeAnalysis,
    ResumeAtsCheck,
    ResumeFinding,
    ResumeSuggestion,
    SuggestionState,
    VersionReason,
)
from apps.resumes.services import analysis as analysis_service
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import suggestions as suggestion_service
from apps.resumes.services import versions as version_service
from config import ai as ai_config

FAKE_PROVIDER = ai_config.ProviderSpec(
    key="fake", adapter="apps.ai.providers.fake.FakeAdapter", base_url="", timeout_seconds=5
)
FAKE_MODEL = ai_config.ModelSpec(
    provider="fake",
    name="fake-1",
    input_cost_per_million=Decimal("1.00"),
    output_cost_per_million=Decimal("2.00"),
)

ANALYSIS_OUTPUT = {
    "verdict": "A working CV that undersells its scope.",
    "headline": "Strong delivery, quiet about scale",
    "findings": [
        {
            "title": "The scope of the settlement work is unclear",
            "detail": "It says what was built, never who used it.",
            "severity": "improve",
            "code": "vague_scope",
            "evidence": "Responsible for the settlement service.",
            "action": "Say who depended on it.",
        },
        {
            "title": "The summary lands",
            "detail": "It says what you do in one line.",
            "severity": "strength",
            "code": "clear_summary",
        },
    ],
}
REWRITE_OUTPUT = {"options": [{"text": "Shipped the settlement service.", "why": "Leads with it."}]}

#: How many bullets the entry in `AnalysisFixture` carries.
BULLETS_IN_THE_FIXTURE = 2
SUMMARY_OUTPUT = {"options": [{"text": "Backend engineer, four years.", "why": "Concrete."}]}


@contextmanager
def fake_ai():
    """Point every AI task at the fake provider.

    The analysis feature calls three task keys, so patching one would leave the
    others reaching a real chain and failing for reasons unrelated to the test.
    """
    tasks = {
        key: ai_config.TaskSpec(key=key, models=("primary",), cache_hours=0)
        for key in ai_config.TASKS
    }
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict(ai_config.PROVIDERS, {"fake": FAKE_PROVIDER}, clear=True)
        )
        stack.enter_context(mock.patch.dict(ai_config.MODELS, {"primary": FAKE_MODEL}, clear=True))
        stack.enter_context(mock.patch.dict(ai_config.TASKS, tasks, clear=True))
        yield


class AnalysisFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()
        self.profile = create_profile()
        self.actor = self.profile.user
        # The free plan meters one analysis a month (P18). This suite is about
        # what an analysis produces, so it buys the unmetered plan; the meter
        # itself is asserted in `apps/billing/tests/test_enforcement.py`.
        billing_factories.put_on_pro(self.profile)
        self.document = document_service.create_document(
            profile=self.profile, actor=self.actor, name="My CV"
        )
        self.draft = self.document.draft
        self._fill()

    def _fill(self) -> None:
        draft_service.set_contact(draft=self.draft, field="name", value="Priya Sharma")
        draft_service.set_contact(draft=self.draft, field="email", value="priya@example.com")
        draft_service.set_contact(draft=self.draft, field="phone", value="+977 9812345678")

        summary = content_rules.section_by_key(self.draft.content, "summary")
        draft_service.set_section_text(
            draft=self.draft,
            section_id=summary["id"],
            text=(
                "Backend engineer with four years building payment systems in Django, "
                "after a role with more architectural ownership."
            ),
        )

        experience = content_rules.section_by_key(self.draft.content, "experience")
        self.entry_id = draft_service.add_entry(
            draft=self.draft,
            section_id=experience["id"],
            title="Senior Backend Engineer",
            org="Nexa Payments",
            dates="Aug 2023 - Present",
        )
        draft_service.set_bullets(
            draft=self.draft,
            entry_id=self.entry_id,
            bullets=[
                "Responsible for the settlement service.",
                "Worked on the reporting API.",
            ],
        )

        skills = content_rules.section_by_key(self.draft.content, "skills")
        draft_service.set_section_items(
            draft=self.draft, section_id=skills["id"], items=["Python", "Django", "PostgreSQL"]
        )
        self.draft.refresh_from_db()


class AnalysisTests(AnalysisFixture):
    def test_analysing_freezes_a_version_and_attaches_the_report_to_it(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.document.refresh_from_db()
        self.assertIsNotNone(self.document.current_version)
        self.assertEqual(analysis.version, self.document.current_version)
        self.assertEqual(analysis.version.created_reason, VersionReason.ANALYSED)

    def test_analysing_twice_without_editing_reuses_the_version(self):
        # Ten clicks on Analyse must not produce ten identical immutable rows —
        # version spam by a different route than the autosave the draft/version
        # split exists to stop (SRS §5.5.1).
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT, ANALYSIS_OUTPUT)
            first = analysis_service.analyse(document=self.document, actor=self.actor)
            second = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(first.version_id, second.version_id)
        self.assertEqual(self.document.versions.count(), 1)
        self.assertEqual(ResumeAnalysis.objects.count(), 2)

    def test_editing_between_analyses_produces_a_second_version(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT, ANALYSIS_OUTPUT)
            first = analysis_service.analyse(document=self.document, actor=self.actor)
            draft_service.set_bullets(
                draft=self.draft,
                entry_id=self.entry_id,
                bullets=["Shipped settlement, 40,000/day."],
            )
            second = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertNotEqual(first.version_id, second.version_id)
        self.assertEqual(self.document.versions.count(), 2)

    def test_an_empty_cv_cannot_be_analysed(self):
        blank = document_service.create_document(
            profile=self.profile,
            actor=self.actor,
            name="Empty",
            content=content_rules.blank_document(),
        )
        with fake_ai(), self.assertRaises(InvalidState):
            analysis_service.analyse(document=blank, actor=self.actor)

    def test_a_redacted_version_cannot_be_analysed(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        version_service.redact(version=analysis.version, actor=self.actor)
        with fake_ai(), self.assertRaises(InvalidState):
            analysis_service.analyse_version(version=analysis.version, actor=self.actor)

    def test_the_report_records_the_scorer_version(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(analysis.algorithm_version, ALGORITHM_VERSION)
        self.assertTrue(analysis.breakdown["components"])
        self.assertEqual(sum(analysis.breakdown["weights"].values()), 100)

    def test_the_checks_are_persisted_once_per_key(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        keys = list(analysis.checks.values_list("key", flat=True))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("extraction", keys)

    def test_the_facts_the_report_states_are_stored_with_it(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(analysis.experience["roles"], 1)
        self.assertTrue(analysis.readability["has_summary"])
        self.assertEqual(analysis.skill_evidence["listed"], 3)

    def test_an_event_is_emitted_with_counts_and_no_cv_text(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        event = OutboxEvent.objects.filter(type="resume.analysed").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.payload["score"], analysis.score)
        blob = str(event.payload)
        self.assertNotIn("settlement", blob)
        self.assertNotIn("priya@example.com", blob)

    def test_the_audit_record_names_the_version_not_the_content(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        from apps.audit.models import AuditLog

        record = AuditLog.objects.filter(verb="resume.analysed").first()
        self.assertIsNotNone(record)
        self.assertEqual(record.metadata["version_no"], analysis.version.version_no)
        self.assertNotIn("settlement", str(record.metadata))


class DegradedAnalysisTests(AnalysisFixture):
    """The report when AI does not work. It still has to be a report."""

    def assert_usable(self, analysis: ResumeAnalysis) -> None:
        self.assertGreater(analysis.score, 0)
        self.assertTrue(analysis.verdict)
        self.assertTrue(analysis.checks.exists())
        self.assertTrue(analysis.findings.filter(from_ai=False).exists())
        self.assertFalse(analysis.findings.filter(from_ai=True).exists())
        self.assertFalse(analysis.has_ai)

    def test_a_report_is_produced_with_ai_switched_off(self):
        FeatureFlag.objects.create(
            key="ai",
            is_enabled=False,
            expires_on=timezone.localdate() + dt.timedelta(days=7),
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

        with fake_ai():
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assert_usable(analysis)
        self.assertEqual(analysis.ai_run.status, RunStatus.DISABLED)
        self.assertEqual(len(FakeAdapter.calls), 0)

    def test_a_report_is_produced_when_the_provider_fails(self):
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assert_usable(analysis)
        self.assertEqual(analysis.ai_run.status, RunStatus.PROVIDER_ERROR)

    def test_a_report_is_produced_when_the_model_returns_the_wrong_shape(self):
        with fake_ai():
            FakeAdapter.queue({"nonsense": True})
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assert_usable(analysis)
        self.assertEqual(analysis.ai_run.status, RunStatus.SCHEMA_ERROR)

    def test_a_report_is_produced_when_the_budget_is_spent(self):
        from apps.ai.services import quotas

        quotas.set_limit(owner=self.actor, limit=Decimal("0"))
        with fake_ai():
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assert_usable(analysis)
        self.assertEqual(analysis.ai_run.status, RunStatus.QUOTA_EXCEEDED)

    def test_the_score_is_identical_with_and_without_ai(self):
        # ADR-A6 made into a test: the model explains, it does not score. If
        # this ever fails, the number stopped being reproducible.
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            with_ai = analysis_service.analyse(document=self.document, actor=self.actor)
            without = analysis_service.analyse_version(
                version=with_ai.version, actor=self.actor, use_ai=False
            )

        self.assertEqual(with_ai.score, without.score)
        self.assertEqual(with_ai.breakdown["components"], without.breakdown["components"])

    def test_a_failed_run_is_still_recorded_and_linked(self):
        # ADR-A11: every call is a row, including the ones that failed. What it
        # must not do is claim the report was AI-assisted.
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertIsNotNone(analysis.ai_run_id)
        self.assertFalse(analysis.has_ai)
        self.assertTrue(AIRun.objects.filter(task_key="resume.analysis").exists())


class AiEnrichmentTests(AnalysisFixture):
    def test_the_model_adds_findings_and_they_are_labelled(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        ai_findings = analysis.findings.filter(from_ai=True)
        self.assertEqual(ai_findings.count(), 2)
        self.assertTrue(analysis.has_ai)
        self.assertEqual(analysis.verdict, ANALYSIS_OUTPUT["verdict"])
        self.assertEqual(analysis.headline, ANALYSIS_OUTPUT["headline"])

    def test_an_ai_finding_carries_no_section_ref_so_it_offers_no_fix(self):
        # The model does not know the content's internal ids, and a fix button
        # that writes to a guessed path is a fix button that corrupts a CV.
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        for finding in analysis.findings.filter(from_ai=True):
            with self.subTest(code=finding.code):
                self.assertEqual(finding.section_ref, "")
                self.assertFalse(finding.is_fixable)

    def test_the_number_of_ai_findings_is_capped(self):
        many = dict(ANALYSIS_OUTPUT)
        many["findings"] = [
            {"title": f"Point {index}", "detail": "…", "severity": "improve"} for index in range(30)
        ]
        with fake_ai():
            FakeAdapter.queue(many)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(
            analysis.findings.filter(from_ai=True).count(), analysis_service.MAX_AI_FINDINGS
        )

    def test_a_severity_outside_the_enum_is_refused_by_the_schema(self):
        # The schema is the enforcement, not a mapping layer downstream: an
        # output the report cannot render never becomes a finding at all, and
        # the deterministic report is unaffected.
        odd = dict(ANALYSIS_OUTPUT)
        odd["findings"] = [{"title": "Something", "detail": "…", "severity": "high"}]
        with fake_ai():
            FakeAdapter.queue(odd)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(analysis.ai_run.status, RunStatus.SCHEMA_ERROR)
        self.assertFalse(analysis.findings.filter(from_ai=True).exists())
        self.assertTrue(analysis.findings.filter(from_ai=False).exists())

    def test_a_severity_from_an_older_prompt_version_still_maps(self):
        # `_severity` is the guard for the case the schema cannot catch: a
        # prompt version whose enum differs from the report's vocabulary. Tested
        # directly, because reaching it through the runner is impossible while
        # the current schema holds — which is the point.
        self.assertEqual(analysis_service._severity("high"), "critical")
        self.assertEqual(analysis_service._severity("strength"), "strength")
        self.assertEqual(analysis_service._severity("anything else"), "improve")

    def test_model_quoted_evidence_is_capped(self):
        loud = dict(ANALYSIS_OUTPUT)
        loud["findings"] = [
            {
                "title": "T",
                "detail": "d",
                "severity": "improve",
                "evidence": "Responsible for the settlement service. " * 40,
            }
        ]
        with fake_ai():
            FakeAdapter.queue(loud)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertLessEqual(len(analysis.findings.get(from_ai=True).evidence), 300)

    def test_a_finding_that_quotes_words_the_cv_does_not_contain_is_dropped(self):
        """The one thing a review of somebody's own document must never do.

        The prompt asks for a quote. A small model sometimes writes a plausible
        one instead — "your CV says you managed a team of twelve" on a CV that
        says nothing of the kind. The candidate reads it, does not recognise
        their own document, and stops believing the rest of the report,
        including the measured half that was right.
        """
        invented = dict(ANALYSIS_OUTPUT)
        invented["findings"] = [
            {
                "title": "The team leadership is undersold",
                "detail": "You led people and barely mention it.",
                "severity": "improve",
                "evidence": "Managed a team of twelve engineers across three countries",
            }
        ]
        with fake_ai():
            FakeAdapter.queue(invented)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertFalse(analysis.findings.filter(from_ai=True).exists())
        # The measured report is untouched: a model that misquotes costs the
        # candidate its own findings and nothing else.
        self.assertTrue(analysis.findings.filter(from_ai=False).exists())

    def test_a_finding_that_quotes_the_cv_loosely_is_kept(self):
        # Models normalise as they quote — a dropped bullet marker, a
        # straightened dash, a trimmed full stop. Refusing those would throw
        # away findings that are perfectly well grounded.
        loose = dict(ANALYSIS_OUTPUT)
        loose["findings"] = [
            {
                "title": "The settlement bullet says nothing happened",
                "detail": "It names a responsibility, not an outcome.",
                "severity": "improve",
                "evidence": "- responsible for the settlement service",
            }
        ]
        with fake_ai():
            FakeAdapter.queue(loose)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(analysis.findings.filter(from_ai=True).count(), 1)

    def test_a_finding_with_no_quote_at_all_is_kept(self):
        # Not every judgement is about a particular line. "The roles do not tell
        # a story" is about all of them, and has nothing to quote.
        unquoted = dict(ANALYSIS_OUTPUT)
        unquoted["findings"] = [
            {"title": "The roles do not tell a story", "detail": "…", "severity": "improve"}
        ]
        with fake_ai():
            FakeAdapter.queue(unquoted)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(analysis.findings.filter(from_ai=True).count(), 1)

    def test_contact_details_are_not_sent_to_the_provider(self):
        # SRS §9.3.7. A model judging writing has no use for a phone number, and
        # the cheapest way to keep one out of a provider's logs is not to send it.
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis_service.analyse(document=self.document, actor=self.actor)

        sent = FakeAdapter.calls[0]["user"]
        self.assertNotIn("priya@example.com", sent)
        self.assertNotIn("9812345678", sent)
        self.assertIn("settlement", sent)

    def test_the_run_is_attributed_to_the_candidate(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(analysis.ai_run.user, self.actor)
        self.assertGreater(analysis.ai_run.cost, Decimal("0"))

    def test_the_analysis_goes_through_the_shared_runner(self):
        # Not a mocked call. The proof that this feature has no provider
        # knowledge of its own is that an AIRun exists for it (P11, ADR-A10).
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis_service.analyse(document=self.document, actor=self.actor)

        run = AIRun.objects.get(task_key="resume.analysis")
        self.assertEqual(run.status, RunStatus.OK)
        self.assertTrue(run.prompt_version)


class TargetedAnalysisTests(AnalysisFixture):
    def test_a_job_target_scores_coverage_against_that_job(self):
        from apps.companies.tests.factories import company_with_owner
        from apps.jobs.models import JobSkill
        from apps.taxonomy.models import Skill

        owner = create_profile().user
        company, _ = company_with_owner(owner)
        from apps.jobs.tests.factories import create_job

        job = create_job(company)
        skill = Skill.objects.create(name="Kubernetes", slug="kubernetes")
        JobSkill.objects.create(job=job, skill=skill)

        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor, job=job)

        component = next(
            item for item in analysis.breakdown["components"] if item["key"] == "skills_coverage"
        )
        self.assertEqual(analysis.job, job)
        self.assertIn("kubernetes", component["note"].lower())


ADVERT = """Senior Backend Engineer
Fonepay, Kathmandu

We need someone to own settlement and reconciliation end to end.
You will work in Python and Django with PostgreSQL, on a Kubernetes estate.
PCI-DSS compliance experience is essential. Reconciliation runs nightly today.
Requirements: 5+ years Python, strong SQL, Docker.
"""


class AdvertTargetTests(TestCase):
    """Reading a pasted job advert (FR-4.6).

    Deterministic on purpose: the advert is matched against the taxonomy, so
    every term reported can be pointed at in the text the candidate pasted, and
    two runs of the same advert say the same thing.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", verbosity=0)

    def setUp(self):
        target_rules.reset_vocabulary()

    def test_the_skills_an_advert_asks_for_are_resolved_through_the_taxonomy(self):
        target = target_rules.from_advert(ADVERT)

        self.assertIn("Python", target.skills)
        self.assertIn("Django", target.skills)
        self.assertIn("PostgreSQL", target.skills)
        self.assertIn("Kubernetes", target.skills)

    def test_a_standard_named_once_still_surfaces_as_a_keyword(self):
        # "PCI-DSS" appears exactly once and is the single term a recruiter's
        # keyword search is most likely to run. A frequency rule alone drops it.
        target = target_rules.from_advert(ADVERT)

        self.assertIn("PCI-DSS", target.keywords)

    def test_the_role_s_own_words_are_not_reported_as_missing_keywords(self):
        target = target_rules.from_advert(ADVERT)

        self.assertNotIn("engineer", [word.lower() for word in target.keywords])
        self.assertNotIn("backend", [word.lower() for word in target.keywords])

    def test_coverage_splits_what_the_cv_answers_from_what_it_does_not(self):
        target = target_rules.from_advert(ADVERT)
        covered = target_rules.coverage(target, {"python", "django", "postgresql"})

        self.assertEqual(sorted(covered["matched"]), ["Django", "PostgreSQL", "Python"])
        self.assertIn("Kubernetes", covered["missing"])
        self.assertEqual(covered["source"], "advert")
        self.assertIsNotNone(covered["percent"])

    def test_an_empty_advert_produces_no_target_rather_than_a_false_one(self):
        target = target_rules.from_advert("")

        self.assertTrue(target.is_empty)
        self.assertIsNone(target_rules.coverage(target, set())["percent"])


class AnalysisAgainstAnAdvertTests(AnalysisFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_taxonomy", verbosity=0)

    def setUp(self):
        super().setUp()
        target_rules.reset_vocabulary()

    def test_pasting_an_advert_records_what_it_asks_for_and_what_is_missing(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(
                document=self.document, actor=self.actor, advert=ADVERT
            )

        self.assertEqual(analysis.target["source"], "advert")
        self.assertTrue(analysis.target["matched"] or analysis.target["missing"])
        self.assertIsNotNone(analysis.target["percent"])
        self.assertIn("Senior Backend Engineer", analysis.advert)

    def test_a_skill_evidenced_in_a_bullet_counts_even_if_it_is_not_listed(self):
        # A candidate who shipped Kubernetes and wrote about it has Kubernetes.
        # Reporting it missing because it is absent from the skills list is
        # advice to pad the list, which is the opposite of the job.
        draft_service.set_bullets(
            draft=self.draft,
            entry_id=self._entry_id(),
            bullets=["Ran the Kubernetes estate behind the settlement service."],
        )
        self.document.refresh_from_db()

        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(
                document=self.document, actor=self.actor, advert=ADVERT
            )

        self.assertIn("Kubernetes", analysis.target["matched"])

    def test_analysing_without_a_target_records_none_rather_than_an_empty_one(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(analysis.target, {})

    def _entry_id(self) -> str:
        for section in self.draft.content["sections"]:
            for entry in section.get("entries") or []:
                return entry["id"]
        raise AssertionError("the fixture CV has no entries")


class DeferredEnrichmentTests(AnalysisFixture):
    """The model's half runs off the request (SRS §9.3.3, §9.3.6).

    Analysing a CV used to run the deterministic pass *and* the model call
    before answering. On a modest provider that is a minute and a half with the
    candidate watching a spinner, and the whole score — every number on the
    report — was ready in the first few milliseconds.

    So the report is written and returned without the prose, and `enrich` adds
    it afterwards. What these assert is that the split cannot move the number.
    """

    def test_a_deferred_analysis_returns_the_whole_deterministic_report(self):
        with fake_ai():
            analysis = analysis_service.analyse(
                document=self.document, actor=self.actor, defer_ai=True
            )

        self.assertGreater(analysis.score, 0)
        self.assertTrue(analysis.checks.exists())
        self.assertTrue(analysis.findings.exists())
        self.assertEqual(analysis.enrichment, EnrichmentState.PENDING)

    def test_a_deferred_analysis_does_not_call_a_model(self):
        with fake_ai():
            analysis_service.analyse(document=self.document, actor=self.actor, defer_ai=True)

        self.assertFalse(AIRun.objects.filter(task_key="resume.analysis").exists())

    def test_enriching_adds_prose_and_findings_without_moving_the_score(self):
        with fake_ai():
            analysis = analysis_service.analyse(
                document=self.document, actor=self.actor, defer_ai=True
            )
            before = analysis.score
            deterministic = analysis.findings.count()

            FakeAdapter.queue(ANALYSIS_OUTPUT)
            enriched = analysis_service.enrich(analysis=analysis, actor=self.actor)

        self.assertEqual(enriched.score, before)
        self.assertEqual(enriched.enrichment, EnrichmentState.DONE)
        self.assertTrue(enriched.headline)
        self.assertGreater(enriched.findings.count(), deterministic)
        self.assertTrue(enriched.findings.filter(from_ai=True).exists())

    def test_enriching_twice_does_not_double_the_findings(self):
        # The task retries. A second pass over a report that already has its
        # prose must be a no-op rather than a second set of findings.
        with fake_ai():
            analysis = analysis_service.analyse(
                document=self.document, actor=self.actor, defer_ai=True
            )
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis_service.enrich(analysis=analysis, actor=self.actor)
            after_first = analysis.findings.count()

            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis_service.enrich(analysis=analysis, actor=self.actor)

        self.assertEqual(analysis.findings.count(), after_first)

    def test_a_provider_outage_leaves_a_complete_deterministic_report(self):
        with fake_ai():
            analysis = analysis_service.analyse(
                document=self.document, actor=self.actor, defer_ai=True
            )
            score = analysis.score

            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            failed = analysis_service.enrich(analysis=analysis, actor=self.actor)

        self.assertEqual(failed.enrichment, EnrichmentState.FAILED)
        self.assertEqual(failed.score, score)
        self.assertTrue(failed.verdict)
        self.assertTrue(failed.checks.exists())

    def test_the_screen_says_the_review_is_still_coming(self):
        with fake_ai():
            analysis_service.analyse(document=self.document, actor=self.actor, defer_ai=True)

        self.client.force_login(self.actor)
        response = self.client.get(reverse("resume:analysis"))

        self.assertContains(response, "being prepared now")
        self.assertContains(response, "every 3s")


class RetryTheWrittenReviewTests(AnalysisFixture):
    """A failed review is a provider outage, not a decision the candidate made.

    The only way to try again was "Analyse this CV", which claims a monthly
    allowance. On a free plan that is one analysis a month — so a timeout on
    the platform's side spent the candidate's whole allowance, and what they
    lost it for was the half that had already worked: the score and the checks
    are never produced by a model and were complete all along.
    """

    def _failed_report(self):
        with fake_ai():
            analysis = analysis_service.analyse(
                document=self.document, actor=self.actor, defer_ai=True
            )
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            analysis_service.enrich(analysis=analysis, actor=self.actor)
            # Whatever the runner did not consume stays in the queue and would
            # be answered to the *retry*, which is the call under test.
            FakeAdapter.reset()
        analysis.refresh_from_db()
        return analysis

    def test_the_setup_really_does_leave_a_failed_review(self):
        self.assertEqual(self._failed_report().enrichment, EnrichmentState.FAILED)

    def test_retrying_does_not_spend_another_analysis(self):
        from apps.billing import limits
        from apps.billing.services import entitlements

        analysis = self._failed_report()
        before = entitlements.remaining(self.profile, limits.RESUME_ANALYSES.key)

        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis_service.retry_enrichment(analysis=analysis, actor=self.actor)

        self.assertEqual(entitlements.remaining(self.profile, limits.RESUME_ANALYSES.key), before)

    def test_retrying_never_freezes_another_version(self):
        analysis = self._failed_report()
        before = self.document.versions.count()

        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis_service.retry_enrichment(analysis=analysis, actor=self.actor)

        self.assertEqual(self.document.versions.count(), before)

    def test_a_successful_retry_fills_in_the_prose_and_leaves_the_score_alone(self):
        analysis = self._failed_report()
        score, checks = analysis.score, analysis.checks.count()

        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            retried = analysis_service.retry_enrichment(analysis=analysis, actor=self.actor)

        self.assertEqual(retried.enrichment, EnrichmentState.DONE)
        self.assertEqual(retried.verdict, ANALYSIS_OUTPUT["verdict"])
        self.assertEqual(retried.score, score)
        self.assertEqual(retried.checks.count(), checks)

    def test_a_second_failure_leaves_the_report_usable(self):
        analysis = self._failed_report()
        score = analysis.score

        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            retried = analysis_service.retry_enrichment(analysis=analysis, actor=self.actor)

        self.assertEqual(retried.enrichment, EnrichmentState.FAILED)
        self.assertEqual(retried.score, score)
        self.assertTrue(retried.findings.filter(from_ai=False).exists())

    def test_retrying_a_report_that_already_has_prose_changes_nothing(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)
        before = analysis.findings.filter(from_ai=True).count()

        with fake_ai():
            analysis_service.retry_enrichment(analysis=analysis, actor=self.actor)

        # Idempotent, so a double-click cannot duplicate the model's findings.
        self.assertEqual(analysis.findings.filter(from_ai=True).count(), before)

    def test_the_screen_offers_the_retry_and_says_it_is_free(self):
        self._failed_report()

        self.client.force_login(self.actor)
        response = self.client.get(reverse("resume:analysis"))

        self.assertContains(response, "Try the written review again")
        self.assertContains(response, "does not use one of your analyses")


class FixTests(AnalysisFixture):
    """FR-4.9, and the invariant underneath it (INV-3)."""

    def analyse(self, *outputs) -> ResumeAnalysis:
        with fake_ai():
            FakeAdapter.queue(*(outputs or (ANALYSIS_OUTPUT,)))
            return analysis_service.analyse(document=self.document, actor=self.actor)

    def test_applying_a_fix_creates_a_suggestion_and_does_not_touch_the_draft(self):
        analysis = self.analyse()
        finding = analysis.findings.get(code="duty_language")
        before = self.draft.content

        with fake_ai():
            FakeAdapter.queue(REWRITE_OUTPUT)
            suggestion = analysis_service.apply_finding(finding=finding, actor=self.actor)

        self.draft.refresh_from_db()
        self.assertEqual(self.draft.content, before)
        self.assertEqual(suggestion.state, SuggestionState.OFFERED)
        self.assertEqual(suggestion.target_path, f"entry.{self.entry_id}.bullets")

    def test_the_suggestion_records_the_finding_and_the_run(self):
        analysis = self.analyse()
        finding = analysis.findings.get(code="duty_language")

        with fake_ai():
            FakeAdapter.queue(REWRITE_OUTPUT)
            suggestion = analysis_service.apply_finding(finding=finding, actor=self.actor)

        self.assertEqual(suggestion.finding, finding)
        self.assertIsNotNone(suggestion.ai_run)
        self.assertEqual(suggestion.ai_run.task_key, "resume.bullet_rewrite")
        self.assertEqual(suggestion.source, "finding")

    def test_accepting_the_suggestion_is_what_writes_the_cv(self):
        analysis = self.analyse()
        finding = analysis.findings.get(code="duty_language")

        with fake_ai():
            FakeAdapter.queue(REWRITE_OUTPUT)
            suggestion = analysis_service.apply_finding(finding=finding, actor=self.actor)

        suggestion_service.accept(suggestion=suggestion, actor=self.actor)

        self.draft.refresh_from_db()
        bullets = content_rules.entry_by_id(self.draft.content, self.entry_id)[1]["bullets"]
        self.assertIn("Shipped the settlement service.", bullets)

    def test_a_summary_finding_asks_for_a_summary(self):
        summary = content_rules.section_by_key(self.draft.content, "summary")
        draft_service.set_section_text(draft=self.draft, section_id=summary["id"], text="")
        self.draft.refresh_from_db()

        analysis = self.analyse()
        finding = analysis.findings.get(code="no_summary")

        with fake_ai():
            FakeAdapter.queue(SUMMARY_OUTPUT)
            suggestion = analysis_service.apply_finding(finding=finding, actor=self.actor)

        self.assertEqual(suggestion.target_path, "summary")
        self.assertEqual(suggestion.proposed, "Backend engineer, four years.")
        self.assertEqual(suggestion.ai_run.task_key, "resume.summary")

    def test_a_failed_rewrite_says_so_rather_than_proposing_the_advice(self):
        """The fallback wrote instructions into the document.

        When the model failed, the suggestion carried the finding's own action
        text — "Rewrite these as things that happened" — and accepting it
        replaced the role's bullets with that sentence. The advice is already on
        the report where the candidate can read it; what it is not is a rewrite,
        and offering it as one cost people their content.
        """
        analysis = self.analyse()
        finding = analysis.findings.get(code="duty_language")

        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            with self.assertRaises(InvalidState):
                analysis_service.apply_finding(finding=finding, actor=self.actor)

        self.assertFalse(ResumeSuggestion.objects.filter(draft=self.draft).exists())

    def test_a_finding_with_no_rewrite_to_draft_offers_no_fix_button(self):
        """The skills list is the case that made this rule.

        A finding about skills claimed but never shown points at the skills
        list and advises naming one in a bullet. There is no rewrite to draft —
        which of a candidate's skills are real is a fact only they hold — so
        the button that promises "drafts a rewrite above" must not appear.
        """
        analysis = self.analyse()
        finding = ResumeFinding.objects.create(
            analysis=analysis,
            severity="improve",
            code="unevidenced_skills",
            title="Some skills are claimed but never shown",
            action="Name one of them in a bullet where you used it, or drop it.",
            section_ref="section.abc123.items",
        )

        self.assertFalse(finding.is_fixable)
        with self.assertRaises(InvalidState):
            analysis_service.apply_finding(finding=finding, actor=self.actor)

    def test_a_strength_cannot_be_applied(self):
        analysis = self.analyse()
        finding = analysis.findings.filter(severity="strength").first()
        with self.assertRaises(InvalidState):
            analysis_service.apply_finding(finding=finding, actor=self.actor)

    def test_a_finding_with_nowhere_to_write_cannot_be_applied(self):
        analysis = self.analyse()
        finding = ResumeFinding.objects.create(
            analysis=analysis, severity="improve", code="floating", title="No target"
        )
        with self.assertRaises(InvalidState):
            analysis_service.apply_finding(finding=finding, actor=self.actor)

    def test_a_rewrite_never_returns_fewer_bullets_than_it_was_given(self):
        """The bug that made this one call per bullet.

        Asked to rewrite three bullets in one call, a small model returned one:
        good wording, two of the candidate's bullets gone, and no sign on the
        screen that anything had been dropped. Accepting it would have deleted
        most of a role.
        """
        analysis = self.analyse()
        finding = analysis.findings.get(code="duty_language")
        before = content_rules.entry_by_id(self.draft.content, self.entry_id)[1]["bullets"]

        with fake_ai():
            # One usable answer for two bullets. The other call falls through to
            # the fake's canned reply, which does not match the task's schema.
            FakeAdapter.queue(REWRITE_OUTPUT)
            suggestion = analysis_service.apply_finding(finding=finding, actor=self.actor)

        self.assertEqual(
            len(suggestion.proposed.splitlines()),
            len(before),
            "a rewrite must return one line per bullet it was given",
        )
        # The bullet nothing usable came back for keeps the candidate's words.
        self.assertTrue(
            any(bullet in suggestion.proposed for bullet in before),
            "an unanswered bullet must survive the rewrite unchanged",
        )

    def test_applying_the_same_fix_twice_does_not_stack_suggestions(self):
        analysis = self.analyse()
        finding = analysis.findings.get(code="duty_language")

        with fake_ai():
            # One answer per bullet, per attempt: the rewrite asks for one
            # bullet at a time so a model cannot return three lines for two and
            # quietly lose one.
            FakeAdapter.queue(*([REWRITE_OUTPUT] * (2 * BULLETS_IN_THE_FIXTURE)))
            first = analysis_service.apply_finding(finding=finding, actor=self.actor)
            second = analysis_service.apply_finding(finding=finding, actor=self.actor)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ResumeSuggestion.objects.filter(draft=self.draft).count(), 1)


class HistoryTests(AnalysisFixture):
    def test_history_is_oldest_first_so_the_line_runs_forwards(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT, ANALYSIS_OUTPUT)
            analysis_service.analyse(document=self.document, actor=self.actor)
            draft_service.set_bullets(
                draft=self.draft,
                entry_id=self.entry_id,
                bullets=["Shipped settlement, 40,000/day."],
            )
            analysis_service.analyse(document=self.document, actor=self.actor)

        history = analysis_service.history_for(self.document)
        self.assertEqual(len(history), 2)
        self.assertLessEqual(history[0]["at"], history[1]["at"])
        self.assertTrue(all(point["comparable"] for point in history))

    def test_a_score_from_an_older_scorer_is_marked_rather_than_hidden(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        ResumeAnalysis.objects.filter(pk=analysis.pk).update(algorithm_version=0)
        history = analysis_service.history_for(self.document)
        self.assertFalse(history[0]["comparable"])

    def test_history_is_empty_before_any_analysis(self):
        self.assertEqual(analysis_service.history_for(self.document), [])


class ScopeTests(AnalysisFixture):
    """Every rule asserted from the failing side (DEVELOPER.md §8.1)."""

    def setUp(self):
        super().setUp()
        self.other = create_profile()
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            self.analysis = analysis_service.analyse(document=self.document, actor=self.actor)

    def test_another_candidates_report_is_not_found(self):
        self.assertIsNone(selectors.analysis_for(self.other, str(self.analysis.public_id)))

    def test_the_owner_finds_their_own_report(self):
        found = selectors.analysis_for(self.profile, str(self.analysis.public_id))
        self.assertEqual(found, self.analysis)

    def test_the_policy_refuses_a_stranger_with_not_found_not_forbidden(self):
        # A 403 confirms the report exists, and the existence of a report about
        # a named person's CV is itself the thing being protected.
        with self.assertRaises(NotFound):
            policies.require_analysis_owner(self.analysis, self.other)

    def test_the_policy_refuses_an_anonymous_reader(self):
        with self.assertRaises(NotFound):
            policies.require_analysis_owner(self.analysis, None)

    def test_the_policy_admits_the_owner(self):
        self.assertEqual(
            policies.require_analysis_owner(self.analysis, self.profile), self.analysis
        )

    def test_another_candidates_finding_is_not_found(self):
        finding = self.analysis.findings.first()
        self.assertIsNone(selectors.finding_for(self.other, finding.pk))

    def test_the_owners_finding_is_found(self):
        finding = self.analysis.findings.first()
        self.assertEqual(selectors.finding_for(self.profile, finding.pk), finding)

    def test_a_redacted_version_is_refused_by_the_policy(self):
        version_service.redact(version=self.analysis.version, actor=self.actor)
        self.analysis.version.refresh_from_db()
        with self.assertRaises(InvalidState):
            policies.require_analysable(self.analysis.version)


class TaskTests(AnalysisFixture):
    def test_the_background_task_analyses_a_version(self):
        from apps.resumes.tasks import analyse_version

        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            result = analyse_version(version.pk, actor_id=self.actor.pk)

        self.assertTrue(result.startswith("analysed:"))
        self.assertEqual(ResumeAnalysis.objects.filter(version=version).count(), 1)

    def test_a_deleted_version_is_not_an_error(self):
        from apps.resumes.tasks import analyse_version

        self.assertEqual(analyse_version(987654), "gone")

    def test_a_redacted_version_is_refused_rather_than_retried(self):
        from apps.resumes.tasks import analyse_version

        version = version_service.finalize(
            document=self.document, actor=self.actor, reason=VersionReason.SAVED
        )
        version_service.redact(version=version, actor=self.actor)

        with fake_ai():
            self.assertEqual(analyse_version(version.pk), "refused")


class ScreenTests(AnalysisFixture):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.actor)

    def test_the_screen_renders_before_any_analysis(self):
        response = self.client.get(reverse("resume:analysis"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "has not been analysed yet")

    def test_running_an_analysis_renders_the_report(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            response = self.client.post(
                reverse("resume:analysis_run"),
                {"document": str(self.document.public_id), "target_role": "Backend developer"},
            )

        self.assertEqual(response.status_code, 200)
        # The deterministic half is what the request itself produced.
        self.assertContains(response, "Machine readability")
        # The prose arrives from `enrich_analysis`, which is eager under test
        # and is re-read before the response is rendered.
        self.assertContains(response, ANALYSIS_OUTPUT["headline"])

    def test_the_report_says_when_the_written_review_could_not_be_produced(self):
        """A provider outage leaves a complete report, and says so.

        The wording changed with the deferred split: the review is asked for
        every time now, so "the AI review did not run" became "it could not be
        produced this time" — a different sentence about a different thing.
        """
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            response = self.client.post(
                reverse("resume:analysis_run"), {"document": str(self.document.public_id)}
            )

        self.assertContains(response, "measured only")
        self.assertContains(response, "could not be produced")
        self.assertContains(response, "every check below are unaffected")

    def test_ai_findings_are_labelled_on_the_page(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            response = self.client.post(
                reverse("resume:analysis_run"), {"document": str(self.document.public_id)}
            )

        self.assertContains(response, "The scope of the settlement work is unclear")
        self.assertContains(response, ">AI<")

    def test_the_fix_button_says_nothing_changes_without_an_accept(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            response = self.client.post(
                reverse("resume:analysis_run"), {"document": str(self.document.public_id)}
            )

        self.assertContains(response, "Nothing changes on your CV until you accept it")

    def test_applying_a_fix_from_the_screen_writes_a_suggestion_only(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)
        finding = analysis.findings.get(code="duty_language")
        before = self.draft.content

        with fake_ai():
            FakeAdapter.queue(REWRITE_OUTPUT)
            response = self.client.post(reverse("resume:analysis_fix"), {"finding": finding.pk})

        self.draft.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.draft.content, before)
        self.assertEqual(ResumeSuggestion.objects.filter(draft=self.draft).count(), 1)

    def test_another_candidate_cannot_apply_a_fix_from_my_report(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)
        finding = analysis.findings.get(code="duty_language")

        self.client.force_login(self.other_user())
        response = self.client.post(reverse("resume:analysis_fix"), {"finding": finding.pk})
        self.assertEqual(response.status_code, 404)

    def test_another_candidate_cannot_read_my_report(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        self.client.force_login(self.other_user())
        response = self.client.get(reverse("resume:analysis"), {"report": str(analysis.public_id)})
        self.assertEqual(response.status_code, 404)

    def test_the_screen_requires_signing_in(self):
        self.client.logout()
        response = self.client.get(reverse("resume:analysis"))
        self.assertEqual(response.status_code, 302)

    def other_user(self):
        return create_profile().user


class ImmutabilityTests(AnalysisFixture):
    def test_no_analysis_path_writes_to_a_finalized_version(self):
        # The belt-and-braces check: analysis reads a version and writes its own
        # rows, and the version's content is byte-identical afterwards.
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT, REWRITE_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)
            before = analysis.version.content

            finding = analysis.findings.get(code="duty_language")
            analysis_service.apply_finding(finding=finding, actor=self.actor)

        analysis.version.refresh_from_db()
        self.assertEqual(analysis.version.content, before)

    def test_deleting_a_version_takes_its_reports_with_it(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        analysis_id, version = analysis.pk, analysis.version
        self.document.versions.filter(pk=version.pk).delete()

        self.assertFalse(ResumeAnalysis.objects.filter(pk=analysis_id).exists())
        self.assertFalse(ResumeFinding.objects.filter(analysis_id=analysis_id).exists())
        self.assertFalse(ResumeAtsCheck.objects.filter(analysis_id=analysis_id).exists())


class UploadedCvTests(AnalysisFixture):
    """A CV that came from a file: the checks that need the original.

    `extraction` and `filename` are the two checks with nothing to read on a
    builder-written CV. When the document came from an upload they have real
    inputs, and this is where that wiring is asserted — the report is otherwise
    quietly missing the two findings a scanned PDF most needs.
    """

    def setUp(self):
        super().setUp()
        from apps.files.constants import ScanStatus
        from apps.files.models import Attachment
        from apps.resumes.models import ParseStatus, ResumeParse, ResumeUpload

        attachment = Attachment.objects.create(
            storage_key="ab/cd/cv.pdf",
            filename="cv.pdf",
            mime="application/pdf",
            size=1024,
            sha256="a" * 64,
            scan_status=ScanStatus.CLEAN,
            uploaded_by=self.actor,
        )
        self.upload = ResumeUpload.objects.create(
            profile=self.profile,
            attachment=attachment,
            original_filename="cv.pdf",
            mime="application/pdf",
            size=1024,
            sha256="a" * 64,
            parse_status=ParseStatus.PARSED,
            claimed_at=timezone.now(),
        )
        ResumeParse.objects.create(
            upload=self.upload,
            engine="test",
            engine_version="1",
            raw_text="Priya Sharma",
            warnings=["page 2 has no text layer"],
        )
        self.document.upload = self.upload
        self.document.save(update_fields=["upload", "updated_at"])

    def test_parse_warnings_reach_the_extraction_check(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        check = analysis.checks.get(key="extraction")
        self.assertEqual(check.status, "warn")
        self.assertIn("no text layer", check.note)

    def test_the_original_filename_reaches_the_filename_check(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=self.document, actor=self.actor)

        check = analysis.checks.get(key="filename")
        self.assertEqual(check.status, "warn")
        self.assertIn("cv.pdf", check.note)

    def test_an_upload_with_no_parse_yet_is_not_an_error(self):
        from apps.resumes.models import ResumeDocument, ResumeParse

        ResumeParse.objects.filter(upload=self.upload).delete()
        # Re-fetched rather than reused: the in-memory document still holds the
        # upload it was assigned, and that upload still holds the parse it
        # cached. What is under test is what a worker loading the row sees.
        document = ResumeDocument.objects.get(pk=self.document.pk)

        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            analysis = analysis_service.analyse(document=document, actor=self.actor)

        self.assertEqual(analysis.checks.get(key="extraction").status, "pass")


class LookupTests(AnalysisFixture):
    def setUp(self):
        super().setUp()
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            self.analysis = analysis_service.analyse(document=self.document, actor=self.actor)

    def test_a_report_is_found_by_its_public_id_within_its_document(self):
        found = analysis_service.find_analysis(
            document=self.document, public_id=str(self.analysis.public_id)
        )
        self.assertEqual(found, self.analysis)

    def test_a_report_from_another_document_is_not_found(self):
        other = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Second CV"
        )
        with self.assertRaises(NotFound):
            analysis_service.find_analysis(document=other, public_id=str(self.analysis.public_id))

    def test_latest_for_returns_the_newest_report(self):
        with fake_ai():
            FakeAdapter.queue(ANALYSIS_OUTPUT)
            newer = analysis_service.analyse(document=self.document, actor=self.actor)

        self.assertEqual(analysis_service.latest_for(self.document), newer)

    def test_latest_for_is_none_before_any_analysis(self):
        blank = document_service.create_document(
            profile=self.profile, actor=self.actor, name="Untouched"
        )
        self.assertIsNone(analysis_service.latest_for(blank))


class FallbackVerdictTests(AnalysisFixture):
    """What the report says when no model spoke.

    Written from the score, which is a fact — not "analysis unavailable", which
    would be false: the analysis is right there and only the prose is missing.
    """

    def test_each_band_says_something_different(self):
        verdicts = {analysis_service._fallback_verdict(score) for score in (95, 70, 50, 20)}
        self.assertEqual(len(verdicts), 4)

    def test_a_low_score_points_at_the_critical_items(self):
        self.assertIn("Fix this", analysis_service._fallback_verdict(10))

    def test_a_high_score_does_not_tell_someone_to_start_over(self):
        self.assertIn("good shape", analysis_service._fallback_verdict(90))

    def test_the_verdict_used_is_the_fallback_when_ai_is_off(self):
        with fake_ai():
            analysis = analysis_service.analyse_version(
                version=version_service.finalize(
                    document=self.document, actor=self.actor, reason=VersionReason.SAVED
                ),
                actor=self.actor,
                use_ai=False,
            )

        self.assertEqual(analysis.verdict, analysis_service._fallback_verdict(analysis.score))
