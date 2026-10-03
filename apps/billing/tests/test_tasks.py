"""
The nightly billing run (SRS §19, cron trigger).

The order the four tasks run in is the property under test, not an
implementation detail: a subscription that is both cancelled and overdue must
end once, as a cancellation, because that is the truthful reason and the one
the customer will read in their email.

The other property is that every one of them is safe to run twice. A billing
cron that double-billed on a retry would be a defect nobody could undo by
deploying a fix.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Invoice, Subscription, SubscriptionState
from apps.billing.services import invoices, subscriptions
from apps.billing.tasks import chase_overdue, end_cancelled, end_unpaid, prune_usage, renew_due
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner


class TaskFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        factories.seed()

    def setUp(self):
        self.profile = create_profile()
        self.subscription = factories.put_on_pro(self.profile)
        self.today = timezone.localdate()

    def end_period(self, *, days_ago: int = 1) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(
            current_period_end=self.today - dt.timedelta(days=days_ago)
        )
        self.subscription.refresh_from_db()


class RenewalTests(TaskFixture):
    def test_a_finished_period_renews_and_invoices(self):
        self.end_period()
        self.assertEqual(renew_due(), 1)

        self.subscription.refresh_from_db()
        self.assertGreater(self.subscription.current_period_end, self.today)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_a_period_still_running_is_left_alone(self):
        self.assertEqual(renew_due(), 0)

    def test_running_twice_bills_once(self):
        self.end_period()
        renew_due()
        renew_due()
        self.assertEqual(Invoice.objects.count(), 1)

    def test_a_free_plan_renews_without_an_invoice(self):
        subscriptions.change_plan(subscription=self.subscription, plan=factories.free_plan())
        self.end_period()

        self.assertEqual(renew_due(), 1)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_a_quoted_plan_renews_without_an_invoice(self):
        # "Talk to us" means the invoice is agreed by a person; the cron must
        # not invent an amount for it.
        company, _ = company_with_owner(create_profile().user)
        subscription = subscriptions.subscribe(owner=company, plan=factories.employer_plan())
        Subscription.objects.filter(pk=subscription.pk).update(
            current_period_end=self.today - dt.timedelta(days=1)
        )

        renew_due()
        self.assertEqual(Invoice.objects.filter(subscription=subscription).count(), 0)

    def test_a_subscription_scheduled_to_end_is_not_renewed(self):
        subscriptions.cancel(subscription=self.subscription)
        self.end_period()

        self.assertEqual(renew_due(), 0)


class DunningTests(TaskFixture):
    def test_an_overdue_invoice_moves_the_subscription_to_past_due(self):
        invoice = invoices.issue(subscription=self.subscription)
        Invoice.objects.filter(pk=invoice.pk).update(due_on=self.today - dt.timedelta(days=1))

        self.assertEqual(chase_overdue(), 1)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.state, SubscriptionState.PAST_DUE)

    def test_the_plan_still_works_while_past_due(self):
        from apps.billing.services import entitlements

        subscriptions.mark_past_due(subscription=self.subscription, invoice_number="INV-1")
        self.assertTrue(entitlements.allows(self.profile, "pro_templates"))

    def test_nothing_is_chased_when_everything_is_paid(self):
        invoices.issue(subscription=self.subscription)
        self.assertEqual(chase_overdue(), 0)

    def test_the_grace_period_has_to_pass_before_anything_ends(self):
        subscriptions.mark_past_due(subscription=self.subscription, invoice_number="INV-1")
        self.assertEqual(end_unpaid(), 0)

        self.end_period(days_ago=30)
        self.assertEqual(end_unpaid(), 1)


class CancellationTests(TaskFixture):
    def test_a_cancellation_date_that_has_arrived_ends_it(self):
        subscriptions.cancel(subscription=self.subscription)
        Subscription.objects.filter(pk=self.subscription.pk).update(cancel_at=self.today)

        self.assertEqual(end_cancelled(), 1)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.state, SubscriptionState.CANCELLED)

    def test_a_future_cancellation_is_left_alone(self):
        subscriptions.cancel(subscription=self.subscription)
        self.assertEqual(end_cancelled(), 0)

    def test_a_cancelled_and_overdue_subscription_ends_as_a_cancellation(self):
        # Order matters: the reason a customer reads must be the true one.
        subscriptions.mark_past_due(subscription=self.subscription, invoice_number="INV-1")
        subscriptions.cancel(subscription=self.subscription)
        Subscription.objects.filter(pk=self.subscription.pk).update(
            cancel_at=self.today, current_period_end=self.today - dt.timedelta(days=30)
        )

        end_cancelled()
        end_unpaid()

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.state, SubscriptionState.CANCELLED)


class PruneTests(TaskFixture):
    def test_old_counters_are_removed_and_recent_ones_kept(self):
        from apps.billing.models import UsageCounter
        from apps.billing.services import entitlements

        entitlements.consume(self.profile, "resume_analyses")
        UsageCounter.objects.all().update(period_start=dt.date(2000, 1, 1))
        entitlements.consume(self.profile, "resume_analyses")

        self.assertEqual(prune_usage(), 1)
        self.assertEqual(UsageCounter.objects.count(), 1)
