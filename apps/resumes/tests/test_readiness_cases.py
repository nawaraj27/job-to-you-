"""
Readiness, case by case, from real input rather than edited check rows.

The adversarial verification pass of 2026-09-21 asked five questions of the
readiness verdict (CVR-01) and found two defects behind them:

  * **TGT-01** — a skill of more than one word ("REST APIs", "UX research",
    "Content marketing") could never be found on a CV, because the target
    comparison looked it up in the set of the CV's *single words*. The skills
    score, which reads the skills list, counted the same skill as present, so
    one report said both. For a UI/UX or marketing posting, readiness could
    never be Complete.
  * Readiness failed on a missing **preferred** skill. A posting marks each
    skill required or preferred; only the required ones decide whether the CV
    is complete for it.

And one race in the same flow:

  * **ENR-01** — two review tasks for one report (a broker redelivery, or the
    free retry pressed while the first was running) both passed the "still
    pending" check before a minutes-long model call, and both appended their
    findings.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from apps.ai.providers.base import ProviderFailure
from apps.ai.providers.fake import FakeAdapter
from apps.billing.tests import factories as billing_factories
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import JobSkill, Requirement
from apps.jobs.tests.factories import create_job, create_skill, publish
from apps.resumes import content as content_rules
from apps.resumes.analysis import target as target_rules
from apps.resumes.models import CheckStatus, EnrichmentState, ResumeAnalysis, ResumeFinding
from apps.resumes.services import analysis as analysis_service
from apps.resumes.services import drafts as draft_service
from apps.taxonomy.models import Skill

from .test_analysis import ANALYSIS_OUTPUT, fake_ai
from .test_readiness import ReadinessFixture


class ReadinessCaseTests(ReadinessFixture):
    """The five cases, each from the CV's own content."""

    def posting(self, *, required, preferred=()):
        company, _ = company_with_owner(self.user, name="Summit Digital Labs")
        job = create_job(company, title="Backend Developer")
        for order, (name, requirement) in enumerate(
            [(name, Requirement.REQUIRED) for name in required]
            + [(name, Requirement.PREFERRED) for name in preferred]
        ):
            skill = Skill.objects.filter(name=name).first() or create_skill(name)
            JobSkill.objects.create(job=job, skill=skill, requirement=requirement, order=order)
        publish(job)
        target_rules.reset_vocabulary()
        return job

    def passing(self, analysis):
        analysis.checks.exclude(status=CheckStatus.FAIL).update(status=CheckStatus.PASS)
        return analysis

    def test_case_1_everything_satisfied_is_complete(self):
        job = self.posting(required=["Python"])
        analysis = self.analyse(job=job)
        self.assertFalse(analysis.checks.filter(status=CheckStatus.FAIL).exists())

        result = self.assess()

        self.assertTrue(result.complete, [item.detail for item in result.outstanding])

    def test_case_2_a_genuinely_failing_ats_check_is_incomplete(self):
        # No email address: the contact check fails on the content itself.
        draft_service.set_contact(draft=self.document.draft, field="email", value="")
        analysis = self.analyse()

        self.assertTrue(analysis.checks.filter(status=CheckStatus.FAIL).exists())
        result = self.assess()
        self.assertFalse(result.complete)
        self.assertEqual(result.label, "Incomplete")
        self.assertIn("ats", [item.key for item in result.outstanding])

    def test_case_3_missing_required_information_is_incomplete(self):
        skills = content_rules.section_by_key(self.document.draft.content, "skills")
        draft_service.set_section_items(
            draft=self.document.draft, section_id=skills["id"], items=[]
        )
        self.passing(self.analyse())

        result = self.assess()

        self.assertFalse(result.complete)
        self.assertIn("sections", [item.key for item in result.outstanding])

    def test_case_4_a_missing_required_skill_of_the_target_is_incomplete(self):
        job = self.posting(required=["Python", "Haskell"])
        self.passing(self.analyse(job=job))

        result = self.assess()

        self.assertFalse(result.complete)
        target = next(item for item in result.outstanding if item.key == "target")
        self.assertIn("Haskell", target.detail)
        self.assertNotIn("Python", target.detail)

    def test_case_5_a_missing_preferred_skill_is_advice_not_incompleteness(self):
        job = self.posting(required=["Python"], preferred=["Haskell"])
        self.passing(self.analyse(job=job))

        result = self.assess()

        self.assertTrue(result.complete, [item.detail for item in result.outstanding])
        target = next(item for item in result.requirements if item.key == "target")
        self.assertIn("Haskell", target.detail)
        self.assertIn("Worth adding", target.detail)

    def test_case_5_readiness_does_not_read_the_match_label(self):
        # Readiness is about the CV and what it is aimed at; the match label is
        # about a profile and a posting. With no target, a skill a posting
        # somewhere asks for does not make the CV incomplete.
        self.posting(required=["Haskell"])
        self.passing(self.analyse())

        self.assertTrue(self.assess().complete)

    def test_an_old_report_without_the_split_stays_strict(self):
        analysis = self.passing(self.analyse(job=self.posting(required=["Python"])))
        analysis.target = {**analysis.target, "missing": ["Haskell"]}
        analysis.target.pop("missing_required", None)
        analysis.save(update_fields=["target"])

        self.assertFalse(self.assess().complete)


class MultiWordSkillTests(ReadinessFixture):
    """TGT-01: skills of more than one word are found."""

    def test_a_multi_word_skill_in_a_bullet_is_present(self):
        covered = target_rules.coverage(
            target_rules.Target(source="job", skills=["REST APIs", "UX research", "Go"]),
            "built rest apis for 12 hotels. ran ux research with front-desk staff.",
        )

        self.assertEqual(covered["matched"], ["REST APIs", "UX research"])
        self.assertEqual(covered["missing"], ["Go"])

    def test_a_skill_inside_a_longer_word_is_not_present(self):
        covered = target_rules.coverage(
            target_rules.Target(source="job", skills=["SQL", "Java"]), "postgresql and javascript"
        )

        self.assertEqual(covered["missing"], ["Java", "SQL"])

    def test_the_report_and_readiness_agree_with_the_skills_score(self):
        company, _ = company_with_owner(self.user, name="Kora Studio")
        job = create_job(company, title="Marketing Specialist")
        skill = Skill.objects.filter(name="Content marketing").first() or create_skill(
            "Content marketing"
        )
        JobSkill.objects.create(job=job, skill=skill, requirement=Requirement.REQUIRED)
        publish(job)
        target_rules.reset_vocabulary()
        skills = content_rules.section_by_key(self.document.draft.content, "skills")
        draft_service.set_section_items(
            draft=self.document.draft,
            section_id=skills["id"],
            items=["Content marketing"],
        )

        analysis = self.analyse(job=job)

        self.assertEqual(analysis.target["missing"], [])
        self.assertEqual(analysis.target["matched"], ["Content marketing"])


class EnrichmentRaceTests(TestCase):
    """ENR-01: one review per report, whoever writes it first."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        from apps.ai.services import prompts

        prompts.load_all()

    def setUp(self):
        from apps.candidates.tests.factories import create_profile
        from apps.resumes import examples
        from apps.resumes.services import documents as document_service

        FakeAdapter.reset()
        self.profile = create_profile()
        billing_factories.put_on_pro(self.profile)
        self.document = document_service.create_document(
            profile=self.profile,
            actor=self.profile.user,
            name="CV",
            content=examples.example_content(name="Maya Karki", email="maya@example.test"),
        )
        self.analysis = analysis_service.analyse(
            document=self.document, actor=self.profile.user, defer_ai=True
        )
        self.assertEqual(self.analysis.enrichment, EnrichmentState.PENDING)

    REVIEW = ANALYSIS_OUTPUT

    def test_a_second_task_with_a_stale_copy_adds_nothing(self):
        stale = ResumeAnalysis.objects.get(pk=self.analysis.pk)
        with fake_ai():
            FakeAdapter.queue(self.REVIEW)
            FakeAdapter.queue(self.REVIEW)
            analysis_service.enrich(analysis=self.analysis)
            ai_after_first = ResumeFinding.objects.filter(
                analysis=self.analysis, from_ai=True
            ).count()
            analysis_service.enrich(analysis=stale)

        self.assertEqual(
            ResumeFinding.objects.filter(analysis=self.analysis, from_ai=True).count(),
            ai_after_first,
        )
        stale.refresh_from_db()
        self.assertEqual(stale.enrichment, EnrichmentState.DONE)

    def test_a_late_failure_does_not_undo_a_written_review(self):
        stale = ResumeAnalysis.objects.get(pk=self.analysis.pk)
        with fake_ai():
            FakeAdapter.queue(self.REVIEW)
            analysis_service.enrich(analysis=self.analysis)
            FakeAdapter.queue(*[ProviderFailure("down")] * 3)
            analysis_service.enrich(analysis=stale)

        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.enrichment, EnrichmentState.DONE)


class UncheckedAtsTests(ReadinessFixture):
    """MIS-01: out of date is not the same as failing."""

    def test_an_edit_after_the_analysis_reads_as_unchecked_not_failing(self):
        from django.urls import reverse

        analysis = self.passing(self.analyse()) if hasattr(self, "passing") else self.analyse()
        analysis.checks.update(status=CheckStatus.PASS)
        draft_service.set_contact(draft=self.document.draft, field="headline", value="Changed")

        result = self.assess()
        ats = next(item for item in result.requirements if item.key == "ats")
        self.assertFalse(result.complete)
        self.assertTrue(ats.unchecked)

        page = self.client.get(reverse("resume:analysis") + f"?document={self.document.public_id}")
        self.assertContains(page, "Not checked since your last edit")
        self.assertContains(page, "Nothing has failed")
        self.assertNotContains(page, "fix what the checks below point at")

    def test_a_failing_check_still_says_incomplete(self):
        from django.urls import reverse

        draft_service.set_contact(draft=self.document.draft, field="email", value="")
        self.analyse()

        page = self.client.get(reverse("resume:analysis") + f"?document={self.document.public_id}")
        self.assertContains(page, "fix what the checks below point at")
