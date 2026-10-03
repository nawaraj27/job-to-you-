"""
Entitlements: the part of P18 everything else depends on (SRS §5.14).

The properties asserted here, and why each one is worth a test rather than a
reading of the code:

  * **Every account has a plan.** An owner with no subscription resolves to the
    default, so no enforcement path has an "unsubscribed" branch — which is
    exactly where a missing-plan bug would live, quietly allowing everything.
  * **A limit refuses in both directions.** Each is asserted allowed on the
    plan that grants it and refused on the plan that does not; a limit only
    ever tested from the permissive side is not a limit anybody has checked.
  * **Counters are per owner and per period.** One candidate's usage must never
    reduce another's allowance, and last month's must not reduce this month's.
  * **A plan limit is not a rate limit.** They are different exceptions with
    different status codes, because "slow down" and "this costs money" are
    different answers and only one of them is fixed by waiting.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.billing.limits import UnknownLimit
from apps.billing.models import UsageCounter
from apps.billing.services import entitlements, subscriptions
from apps.billing.services.entitlements import PlanLimitExceeded
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.common.errors import QuotaExceeded
from apps.companies.tests.factories import company_with_owner


class EntitlementFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        factories.seed()

    def setUp(self):
        self.profile = create_profile()
        self.company, _ = company_with_owner(create_profile().user)


class PlanResolutionTests(EntitlementFixture):
    def test_an_account_with_no_subscription_is_on_the_default(self):
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")

    def test_a_company_with_no_subscription_is_on_the_employer_plan(self):
        self.assertEqual(entitlements.plan_for(self.company).code, "employer")

    def test_a_subscription_decides_the_plan(self):
        factories.put_on_pro(self.profile)
        self.assertEqual(entitlements.plan_for(self.profile).code, "pro")

    def test_a_user_resolves_through_their_profile(self):
        factories.put_on_pro(self.profile)
        self.assertEqual(entitlements.plan_for(self.profile.user).code, "pro")

    def test_a_cancelled_subscription_stops_deciding(self):
        subscription = factories.put_on_pro(self.profile)
        subscriptions.expire(subscription=subscription, reason="cancelled")
        self.assertEqual(entitlements.plan_for(self.profile).code, "free")

    def test_a_past_due_subscription_still_entitles(self):
        # Cutting somebody off the hour a card fails loses the customer and the
        # payment. The withdrawal is a scheduled decision, not a reflex.
        subscription = factories.put_on_pro(self.profile)
        subscriptions.mark_past_due(subscription=subscription, invoice_number="INV-1")
        self.assertEqual(entitlements.plan_for(self.profile).code, "pro")

    def test_a_missing_default_plan_is_loud(self):
        from apps.billing.models import Plan

        Plan.objects.filter(audience="candidate").update(is_default=False)
        with self.assertRaises(LookupError):
            entitlements.plan_for(self.profile)


class FlagLimitTests(EntitlementFixture):
    def test_free_is_refused_pro_templates(self):
        self.assertFalse(entitlements.allows(self.profile, "pro_templates"))
        with self.assertRaises(PlanLimitExceeded):
            entitlements.require(self.profile, "pro_templates")

    def test_pro_is_allowed_pro_templates(self):
        factories.put_on_pro(self.profile)
        entitlements.require(self.profile, "pro_templates")  # does not raise

    def test_the_refusal_names_the_plan_and_the_limit(self):
        with self.assertRaises(PlanLimitExceeded) as caught:
            entitlements.require(self.profile, "pro_templates")

        self.assertEqual(caught.exception.details["limit"], "pro_templates")
        self.assertEqual(caught.exception.details["plan"], "free")

    def test_a_plan_limit_is_not_a_rate_limit(self):
        # Both are QuotaExceeded, and they must not be the same answer: 402
        # says "this costs money", 429 says "wait", and only one is true.
        from apps.common.ratelimit import RateLimited

        self.assertTrue(issubclass(PlanLimitExceeded, QuotaExceeded))
        self.assertEqual(PlanLimitExceeded.status_code, 402)
        self.assertEqual(RateLimited.status_code, 429)


class SetLimitTests(EntitlementFixture):
    def test_free_may_not_export_at_all(self):
        self.assertFalse(entitlements.allows(self.profile, "export_formats", value="pdf"))

    def test_pro_may_export_both_formats(self):
        factories.put_on_pro(self.profile)
        for fmt in ("pdf", "docx"):
            self.assertTrue(entitlements.allows(self.profile, "export_formats", value=fmt))

    def test_a_format_outside_the_list_is_refused_even_on_pro(self):
        factories.put_on_pro(self.profile)
        self.assertFalse(entitlements.allows(self.profile, "export_formats", value="rtf"))

    def test_an_absent_limit_means_unlimited(self):
        plan = factories.make_plan(code="open", limits={})
        subscriptions.subscribe(owner=self.profile, plan=plan)
        # `export_formats` declares an empty default, so absence is *not*
        # permissive for this one — the declared default governs.
        self.assertFalse(entitlements.allows(self.profile, "export_formats", value="pdf"))

    def test_an_explicit_null_means_unlimited(self):
        plan = factories.make_plan(code="anything", limits={"export_formats": None})
        subscriptions.subscribe(owner=self.profile, plan=plan)
        self.assertTrue(entitlements.allows(self.profile, "export_formats", value="rtf"))


class CountLimitTests(EntitlementFixture):
    def test_the_first_use_is_allowed_and_the_second_is_not(self):
        entitlements.require(self.profile, "resume_analyses")
        entitlements.consume(self.profile, "resume_analyses")

        with self.assertRaises(PlanLimitExceeded):
            entitlements.require(self.profile, "resume_analyses")

    def test_pro_is_unlimited(self):
        factories.put_on_pro(self.profile)
        for _ in range(5):
            entitlements.consume(self.profile, "resume_analyses")
        entitlements.require(self.profile, "resume_analyses")  # still fine

    def test_remaining_counts_down_and_stops_at_zero(self):
        self.assertEqual(entitlements.remaining(self.profile, "resume_analyses"), 1)
        entitlements.consume(self.profile, "resume_analyses")
        self.assertEqual(entitlements.remaining(self.profile, "resume_analyses"), 0)
        entitlements.consume(self.profile, "resume_analyses")
        self.assertEqual(entitlements.remaining(self.profile, "resume_analyses"), 0)

    def test_remaining_is_none_when_unlimited(self):
        factories.put_on_pro(self.profile)
        self.assertIsNone(entitlements.remaining(self.profile, "resume_analyses"))

    def test_one_persons_usage_does_not_spend_anothers(self):
        other = create_profile()
        entitlements.consume(self.profile, "resume_analyses")
        entitlements.require(other, "resume_analyses")  # untouched

    def test_last_months_usage_does_not_count_against_this_month(self):
        entitlements.consume(self.profile, "resume_analyses")
        UsageCounter.objects.filter(owner_id=str(self.profile.pk)).update(
            period_start=timezone.localdate().replace(day=1) - dt.timedelta(days=32)
        )
        entitlements.require(self.profile, "resume_analyses")

    def test_consuming_a_flag_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            entitlements.consume(self.profile, "pro_templates")

    def test_crossing_the_limit_emits_once(self):
        from apps.notifications.models import OutboxEvent

        entitlements.consume(self.profile, "resume_analyses")
        entitlements.consume(self.profile, "resume_analyses")
        entitlements.consume(self.profile, "resume_analyses")

        self.assertEqual(OutboxEvent.objects.filter(type="subscription.limit_reached").count(), 1)


class MoneyLimitTests(EntitlementFixture):
    def test_the_plan_supplies_the_ai_ceiling(self):
        from decimal import Decimal

        factories.put_on_pro(self.profile)
        self.assertEqual(
            entitlements.money(self.profile, "ai_cost_monthly", fallback=Decimal("2.00")),
            Decimal("20.00"),
        )

    def test_a_silent_plan_leaves_the_codes_default_in_place(self):
        # ADR-A28: AI configuration lives in code. A plan may raise the
        # ceiling; a plan that says nothing must not remove it.
        from decimal import Decimal

        plan = factories.make_plan(code="quiet", limits={})
        subscriptions.subscribe(owner=self.profile, plan=plan)
        self.assertEqual(
            entitlements.money(self.profile, "ai_cost_monthly", fallback=Decimal("2.00")),
            Decimal("2.00"),
        )


class VocabularyTests(EntitlementFixture):
    def test_an_undeclared_limit_is_refused(self):
        with self.assertRaises(UnknownLimit):
            entitlements.allows(self.profile, "unlimited_everything")

    def test_a_plan_cannot_be_saved_with_an_undeclared_limit(self):
        from apps.billing.services import plans

        with self.assertRaises(UnknownLimit):
            plans.create(
                actor=None,
                code="typo",
                name="Typo",
                audience="candidate",
                limits={"resume_analysis": 5},  # singular: a typo
            )

    def test_a_plan_cannot_be_saved_with_the_wrong_shape(self):
        from apps.billing.services import plans

        with self.assertRaises(ValueError):
            plans.create(
                actor=None,
                code="wrong",
                name="Wrong",
                audience="candidate",
                limits={"pro_templates": "yes please"},
            )

    def test_an_unreadable_schema_version_refuses_rather_than_guesses(self):
        plan = factories.make_plan(code="future", limits={})
        plan.limits = {"schema_version": 99, "pro_templates": True}
        plan.save()
        subscriptions.subscribe(owner=self.profile, plan=plan)

        with self.assertRaises(ValueError):
            entitlements.limits_for(self.profile)
