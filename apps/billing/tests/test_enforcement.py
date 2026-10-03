"""
Where the plan actually stops somebody (P18's exit criterion).

> A plan limit is enforced in a service, never in a template.

That sentence is the whole point of this file. Each limit is asserted from the
*service*, with no view and no request involved, because a limit that only
holds when the page hides the button is not a limit — the builder posts a
template slug and the export endpoint takes a format, and both are reachable
without ever loading the screen that was supposed to enforce anything.

Each is also asserted in both directions: refused on free, allowed on Pro.
A gate only ever tested from the refusing side might be refusing everybody.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from apps.ai.services import quotas
from apps.billing.services import entitlements, subscriptions
from apps.billing.services.entitlements import PlanLimitExceeded
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.resumes.models import RenderFormat, ResumeTemplate, TemplateTier
from apps.resumes.services import documents


class EnforcementFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        factories.seed()

    def setUp(self):
        self.profile = create_profile()
        self.user = self.profile.user
        self.free_template = ResumeTemplate.objects.filter(tier=TemplateTier.FREE).first()
        self.pro_template = ResumeTemplate.objects.filter(tier=TemplateTier.PRO).first()


class TemplateGateTests(EnforcementFixture):
    def test_a_free_candidate_is_refused_a_pro_template(self):
        document = documents.create_document(profile=self.profile, actor=self.user)

        with self.assertRaises(PlanLimitExceeded):
            documents.set_template(document=document, actor=self.user, template=self.pro_template)

    def test_a_pro_candidate_may_use_one(self):
        factories.put_on_pro(self.profile)
        document = documents.create_document(profile=self.profile, actor=self.user)

        documents.set_template(document=document, actor=self.user, template=self.pro_template)
        document.refresh_from_db()
        self.assertEqual(document.template, self.pro_template)

    def test_a_free_template_is_never_gated(self):
        document = documents.create_document(profile=self.profile, actor=self.user)
        documents.set_template(document=document, actor=self.user, template=self.free_template)

    def test_a_document_cannot_be_created_straight_onto_a_pro_template(self):
        # The gate is on creation too: otherwise "new CV with this template" is
        # a way around the gate on "change to this template".
        with self.assertRaises(PlanLimitExceeded):
            documents.create_document(
                profile=self.profile, actor=self.user, template=self.pro_template
            )

    def test_the_default_template_is_one_everybody_may_use(self):
        # A platform default the candidate is then refused would be a broken
        # account rather than an upsell.
        self.assertEqual(documents.default_template().tier, TemplateTier.FREE)


class AnalysisMeterTests(EnforcementFixture):
    def test_the_free_plan_allows_one_analysis_a_month(self):
        from apps.resumes.services import analysis

        document = documents.create_document(profile=self.profile, actor=self.user)
        analysis.analyse(document=document, actor=self.user, use_ai=False)

        with self.assertRaises(PlanLimitExceeded):
            analysis.analyse(document=document, actor=self.user, use_ai=False)

    def test_pro_is_not_metered(self):
        from apps.resumes.services import analysis

        factories.put_on_pro(self.profile)
        document = documents.create_document(profile=self.profile, actor=self.user)

        for _ in range(3):
            analysis.analyse(document=document, actor=self.user, use_ai=False)

    def test_a_failed_analysis_does_not_spend_the_allowance(self):
        # Charging somebody for the platform's own failure is the sort of
        # billing bug people leave over.
        from unittest import mock

        from apps.resumes.services import analysis

        document = documents.create_document(profile=self.profile, actor=self.user)
        with (
            mock.patch.object(analysis, "analyse_version", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            analysis.analyse(document=document, actor=self.user, use_ai=False)

        self.assertEqual(entitlements.remaining(self.profile, "resume_analyses"), 1)

    def test_re_running_against_an_existing_version_is_not_metered(self):
        # A retry after a provider outage is not a second analysis anybody
        # asked for, and `analyse_version` is the path that retry takes.
        from apps.resumes.services import analysis, versions

        document = documents.create_document(profile=self.profile, actor=self.user)
        version = versions.finalize(document=document, actor=self.user, reason="analysed")

        analysis.analyse_version(version=version, actor=self.user, use_ai=False)
        self.assertEqual(entitlements.remaining(self.profile, "resume_analyses"), 1)


class ExportGateTests(EnforcementFixture):
    def test_a_free_candidate_cannot_export(self):
        from apps.resumes.services import renders

        document = documents.create_document(profile=self.profile, actor=self.user)
        with self.assertRaises(PlanLimitExceeded):
            renders.export(document=document, actor=self.user, fmt=RenderFormat.PDF)

    def test_a_pro_candidate_can(self):
        from apps.resumes.services import renders

        factories.put_on_pro(self.profile)
        document = documents.create_document(profile=self.profile, actor=self.user)
        render = renders.export(document=document, actor=self.user, fmt=RenderFormat.PDF)

        self.assertIsNotNone(render.attachment_id)

    def test_the_render_an_application_freezes_is_not_gated(self):
        # ADR-A8: that render is the platform keeping its own evidentiary
        # record, not a download somebody bought. Gating it would break
        # applying for a job on the free plan, which nothing advertises.
        from apps.resumes.services import renders, versions

        document = documents.create_document(profile=self.profile, actor=self.user)
        version = versions.finalize(document=document, actor=self.user, reason="applied")

        render = renders.render_version(version=version, actor=self.user, fmt=RenderFormat.PDF)
        self.assertIsNotNone(render.pk)


class AiBudgetTests(EnforcementFixture):
    def test_the_plan_sets_the_ai_ceiling(self):
        from decimal import Decimal

        factories.put_on_pro(self.profile)
        quota = quotas.usage_for(self.user)
        self.assertEqual(quota.limit_cost, Decimal("20.00"))

    def test_a_free_account_keeps_the_codes_default(self):
        from decimal import Decimal

        quota = quotas.usage_for(self.user)
        self.assertEqual(quota.limit_cost, Decimal("2.00"))

    def test_an_ai_call_survives_the_billing_catalogue_being_absent(self):
        # An AI run must not depend on the plan catalogue being loaded: the
        # lookup falls back to config/ai.py rather than failing the run.
        from decimal import Decimal

        from apps.billing.models import Plan

        Plan.objects.all().delete()
        allowed, reason = quotas.check(user=self.user)

        self.assertTrue(allowed)
        self.assertEqual(quotas.usage_for(self.user).limit_cost, Decimal("2.00"))


class DowngradeTests(EnforcementFixture):
    def test_losing_pro_takes_the_pro_template_gate_back(self):
        subscription = factories.put_on_pro(self.profile)
        document = documents.create_document(profile=self.profile, actor=self.user)
        documents.set_template(document=document, actor=self.user, template=self.pro_template)

        subscriptions.cancel(subscription=subscription, actor=self.user, immediately=True)

        # The document keeps its template — a CV must not change shape because
        # a card expired — but the candidate cannot move another one to it.
        document.refresh_from_db()
        self.assertEqual(document.template, self.pro_template)

        other = documents.create_document(profile=self.profile, actor=self.user, name="Second")
        with self.assertRaises(PlanLimitExceeded):
            documents.set_template(document=other, actor=self.user, template=self.pro_template)
