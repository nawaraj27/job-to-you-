"""
"Complete" means complete (CVR-01).

The guided counter said "6 of 6 done" on a CV whose applicant-tracking checks
were failing. What is asserted here is the canonical rule, and that the two
screens that show it — the guided builder and the analysis report — read the
same answer.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.billing.tests import factories as billing_factories
from apps.candidates.services import profile as profile_service
from apps.resumes import examples, readiness
from apps.resumes.models import CheckStatus
from apps.resumes.services import analysis as analysis_service
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service


class ReadinessFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        from apps.resumes.analysis import target as target_rules

        # The advert reader caches the taxonomy for the process; a vocabulary
        # built inside another test's (rolled back) taxonomy must not be read.
        target_rules.reset_vocabulary()
        self.addCleanup(target_rules.reset_vocabulary)
        self.user = create_user(full_name="Maya Karki")
        self.client.force_login(self.user)
        self.profile = profile_service.get_or_create_profile(user=self.user)
        billing_factories.put_on_pro(self.profile)
        # A full CV: every section has something in it, so the guided
        # counter reads "all done" whatever the analysis says.
        self.document = document_service.create_document(
            profile=self.profile,
            actor=self.user,
            name="Full CV",
            content=examples.example_content(name="Maya Karki", email="maya@example.test"),
        )

    def analyse(self, **kwargs):
        self.document.refresh_from_db()
        return analysis_service.analyse(
            document=self.document, actor=self.user, defer_ai=True, **kwargs
        )

    def set_checks(self, analysis, status):
        analysis.checks.update(status=status)

    def assess(self):
        self.document.refresh_from_db()
        return readiness.assess(self.document)


class RuleTests(ReadinessFixture):
    def test_the_guided_counter_is_full_but_that_is_not_readiness(self):
        from apps.resumes.services import builders

        guided = builders.guided_state(document=self.document)
        self.assertEqual(guided["completed"], guided["total"])
        # Never analysed: an unknown is not a pass.
        self.assertFalse(self.assess().complete)

    def test_ats_satisfied_and_sections_filled_is_complete(self):
        analysis = self.analyse()
        self.set_checks(analysis, CheckStatus.PASS)

        result = self.assess()

        self.assertTrue(result.complete)
        self.assertEqual(result.label, "Complete")

    def test_ats_not_satisfied_is_incomplete(self):
        analysis = self.analyse()
        self.set_checks(analysis, CheckStatus.PASS)
        analysis.checks.filter(pk=analysis.checks.first().pk).update(status=CheckStatus.FAIL)

        result = self.assess()

        self.assertFalse(result.complete)
        self.assertEqual([item.key for item in result.outstanding], ["ats"])
        self.assertIn("Failing:", result.outstanding[0].detail)

    def test_a_warning_is_not_a_failure_but_is_still_said(self):
        analysis = self.analyse()
        self.set_checks(analysis, CheckStatus.WARN)

        result = self.assess()

        self.assertTrue(result.complete)
        ats = next(item for item in result.requirements if item.key == "ats")
        self.assertIn("Worth improving", ats.detail)

    def test_an_analysis_of_an_older_draft_does_not_count(self):
        analysis = self.analyse()
        self.set_checks(analysis, CheckStatus.PASS)
        draft_service.set_contact(draft=self.document.draft, field="headline", value="Changed")

        result = self.assess()

        self.assertFalse(result.complete)
        self.assertIn("changed since", result.outstanding[0].detail)

    def test_a_missing_required_section_is_incomplete(self):
        from apps.resumes import content as content_rules

        analysis = self.analyse()
        self.set_checks(analysis, CheckStatus.PASS)
        skills = content_rules.section_by_key(self.document.draft.content, "skills")
        draft_service.set_section_items(
            draft=self.document.draft, section_id=skills["id"], items=[]
        )

        result = self.assess()

        self.assertFalse(result.complete)
        self.assertIn("sections", [item.key for item in result.outstanding])

    def test_missing_target_job_evidence_is_incomplete(self):
        analysis = self.analyse(
            target_role="Backend Developer",
            advert="We need Python, Django, Kubernetes and Terraform experience.",
        )
        self.set_checks(analysis, CheckStatus.PASS)
        self.assertTrue(analysis.target.get("missing"), "fixture: the advert must ask for more")

        result = self.assess()

        self.assertFalse(result.complete)
        self.assertIn("target", [item.key for item in result.outstanding])


class ScreenTests(ReadinessFixture):
    def test_guided_and_analysis_screens_show_the_same_answer(self):
        analysis = self.analyse()
        self.set_checks(analysis, CheckStatus.PASS)
        analysis.checks.filter(pk=analysis.checks.first().pk).update(status=CheckStatus.FAIL)

        guided = self.client.get(
            reverse("resume:guided", kwargs={"document_id": self.document.public_id})
        )
        report = self.client.get(
            reverse("resume:analysis") + f"?document={self.document.public_id}"
        )

        for response in (guided, report):
            with self.subTest(url=response.request["PATH_INFO"]):
                self.assertContains(response, 'data-readiness="incomplete"')
                self.assertNotContains(response, 'data-readiness="complete"')


class CvJobsTests(ReadinessFixture):
    """The analysis page lists jobs from the matching feed, truthfully (MAT-02)."""

    def test_skills_on_the_cv_but_not_the_profile_are_named(self):
        response = self.client.get(
            reverse("resume:analysis") + f"?document={self.document.public_id}"
        )

        # The example CV lists Kubernetes; this profile has no skills at all.
        self.assertIn("Kubernetes", response.context["unlisted_skills"])
        self.assertContains(response, "matching cannot count them yet")

    def test_the_jobs_shown_are_the_feed_and_carry_its_labels(self):
        from apps.matching.services import recommendations

        response = self.client.get(
            reverse("resume:analysis") + f"?document={self.document.public_id}"
        )
        feed = recommendations.feed_for(self.profile)

        shown = [row["slug"] for row in response.context.get("cv_jobs", [])]
        self.assertEqual(shown, [item.job.slug for item in feed.items[:3]])


class CvJobReasonsTests(ReadinessFixture):
    """MAT-03: each recommended job says what it finds on this CV and what it lacks."""

    def test_matched_and_missing_skills_are_named_per_job(self):
        from types import SimpleNamespace
        from unittest import mock

        from apps.companies.tests.factories import company_with_owner
        from apps.jobs.models import JobSkill, Requirement
        from apps.jobs.tests.factories import create_job, create_skill, publish
        from apps.resumes.analysis import target as target_rules
        from apps.taxonomy.models import Skill
        from portals.candidate import resume_views

        company, _ = company_with_owner(create_user(), name="Summit Digital Labs")
        job = create_job(company, title="Backend Developer")
        for name, requirement in (
            ("Python", "required"),
            ("Haskell", "required"),
            ("Elixir", "preferred"),
        ):
            skill = Skill.objects.filter(name=name).first() or create_skill(name)
            JobSkill.objects.create(job=job, skill=skill, requirement=requirement)
        publish(job)
        target_rules.reset_vocabulary()
        feed = SimpleNamespace(
            items=[SimpleNamespace(job=job, match=SimpleNamespace(band="partial", score=61))]
        )

        with (
            mock.patch("apps.matching.services.recommendations.feed_for", return_value=feed),
            mock.patch("apps.core.features.is_enabled", return_value=True),
        ):
            context = resume_views._cv_jobs(self.profile, self.document)

        row = context["cv_jobs"][0]
        self.assertIn("Python", row["matched"])
        self.assertEqual(row["missing_required"], ["Haskell"])
        self.assertEqual(row["missing_preferred"], ["Elixir"])
        self.assertEqual(Requirement.REQUIRED, "required")
