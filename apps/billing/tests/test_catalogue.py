"""
Plan administration, coupons and the authorization on both (SRS §5.14, §2.4).

The properties worth holding here:

  * **One default plan per audience, and changing it is one act.** A staff
    member promoting a new default means "this one instead"; making them demote
    the old one first is a two-step somebody will get half-way through.
  * **A plan is never deleted.** `Subscription.plan` is `PROTECT`, because an
    invoice referencing a plan nobody can look up is a support ticket with no
    answer. Retiring is the operation that exists.
  * **Billing rights are a grant, not a role.** Asserted from the failing side
    for the company admin, who can do nearly everything else.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.billing import policies
from apps.billing.models import Coupon, Plan
from apps.billing.services import coupons, plans, subscriptions
from apps.billing.tests import factories
from apps.candidates.tests.factories import create_profile
from apps.common.errors import Conflict, InvalidState, NotAllowed, NotFound
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner


class CatalogueFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        factories.seed()

    def setUp(self):
        self.staff = create_profile().user


class PlanServiceTests(CatalogueFixture):
    def test_a_plan_is_created_and_audited(self):
        plan = plans.create(
            actor=self.staff,
            code="student",
            name="Student",
            audience="candidate",
            price=Decimal("4.00"),
            currency="USD",
            limits={"resume_analyses": 5},
        )

        self.assertEqual(plan.limits["resume_analyses"], 5)
        self.assertTrue(
            AuditLog.objects.filter(verb="billing.plan_created", target_id=str(plan.pk)).exists()
        )

    def test_the_document_is_stamped_with_its_schema_version(self):
        plan = plans.create(
            actor=self.staff, code="stamped", name="Stamped", audience="candidate", limits={}
        )
        self.assertEqual(plan.limits["schema_version"], 1)

    def test_a_duplicate_code_is_refused(self):
        with self.assertRaises(Conflict):
            plans.create(actor=self.staff, code="free", name="Free again", audience="candidate")

    def test_an_unknown_audience_is_refused(self):
        with self.assertRaises(InvalidState):
            plans.create(actor=self.staff, code="alien", name="Alien", audience="martian")

    def test_by_code_finds_a_plan_and_says_so_when_it_does_not(self):
        self.assertEqual(plans.by_code("pro").name, "Pro")
        with self.assertRaises(NotFound):
            plans.by_code("platinum")

    def test_published_lists_only_active_plans_for_one_audience(self):
        codes = {plan.code for plan in plans.published("candidate")}
        self.assertEqual(codes, {"free", "pro"})

    def test_updating_limits_revalidates_them(self):
        plan = factories.pro_plan()
        plans.update(plan=plan, actor=self.staff, limits={"resume_analyses": 3})

        plan.refresh_from_db()
        self.assertEqual(plan.limits["resume_analyses"], 3)

        with self.assertRaises(ValueError):
            plans.update(plan=plan, actor=self.staff, limits={"resume_analyses": "lots"})

    def test_promoting_a_new_default_demotes_the_old_one(self):
        plan = factories.pro_plan()
        plans.update(plan=plan, actor=self.staff, is_default=True)

        self.assertEqual(
            Plan.objects.filter(audience="candidate", is_default=True).get().code, "pro"
        )

    def test_creating_a_default_demotes_the_old_one(self):
        plans.create(
            actor=self.staff,
            code="basic",
            name="Basic",
            audience="candidate",
            is_default=True,
        )
        self.assertEqual(
            Plan.objects.filter(audience="candidate", is_default=True).get().code, "basic"
        )

    def test_two_defaults_are_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Plan.objects.create(
                code="second-default", name="Second", audience="candidate", is_default=True
            )

    def test_retiring_takes_a_plan_out_of_circulation(self):
        plan = factories.pro_plan()
        plans.retire(plan=plan, actor=self.staff)

        plan.refresh_from_db()
        self.assertFalse(plan.is_active)
        self.assertEqual([row.code for row in plans.published("candidate")], ["free"])

    def test_the_default_plan_cannot_be_retired(self):
        # Retiring it would leave every unsubscribed account with no plan, and
        # therefore with no answer to any entitlement question.
        with self.assertRaises(InvalidState):
            plans.retire(plan=factories.free_plan(), actor=self.staff)

    def test_a_plan_in_use_cannot_be_deleted(self):
        profile = create_profile()
        subscriptions.subscribe(owner=profile, plan=factories.pro_plan())

        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError), transaction.atomic():
            factories.pro_plan().delete()

    def test_a_free_plan_knows_it_is_free_and_a_quoted_one_does_not(self):
        self.assertTrue(factories.free_plan().is_free)
        self.assertFalse(factories.employer_plan().is_free)


class CouponTests(CatalogueFixture):
    def test_a_valid_code_is_returned(self):
        Coupon.objects.create(code="WELCOME", percent_off=20)
        coupon = coupons.validate(code="welcome", plan=factories.pro_plan())
        self.assertEqual(coupon.code, "WELCOME")

    def test_an_inactive_code_is_refused(self):
        Coupon.objects.create(code="OFF", percent_off=20, is_active=False)
        with self.assertRaises(InvalidState):
            coupons.validate(code="OFF", plan=factories.pro_plan())

    def test_a_code_for_another_plan_says_which_plan(self):
        Coupon.objects.create(code="EMPLOYERS", percent_off=20, plan=factories.employer_plan())
        with self.assertRaises(InvalidState):
            coupons.validate(code="EMPLOYERS", plan=factories.pro_plan())

    def test_preview_shows_what_the_first_invoice_would_be(self):
        coupon = Coupon.objects.create(code="QUARTER", percent_off=25)
        pro = factories.pro_plan()
        self.assertEqual(coupons.preview(coupon=coupon, plan=pro), pro.price * Decimal("0.75"))

    def test_a_preview_never_goes_below_zero(self):
        coupon = Coupon.objects.create(
            code="HUGE", amount_off=factories.pro_plan().price + Decimal("999")
        )
        self.assertEqual(coupons.preview(coupon=coupon, plan=factories.pro_plan()), Decimal("0"))

    def test_redeeming_counts_and_audits(self):
        coupon = Coupon.objects.create(code="ONCE", percent_off=10, max_redemptions=2)
        coupons.redeem(coupon=coupon, actor=self.staff)

        self.assertEqual(coupon.redemptions, 1)
        self.assertTrue(AuditLog.objects.filter(verb="billing.coupon_redeemed").exists())

    def test_an_expired_coupon_is_not_redeemable(self):
        coupon = Coupon.objects.create(
            code="GONE", percent_off=10, expires_on=timezone.localdate() - dt.timedelta(days=1)
        )
        self.assertFalse(coupon.is_redeemable)

    def test_a_coupon_cannot_be_both_a_percentage_and_an_amount(self):
        # Two answers to what it is worth, and whichever the code applies first
        # becomes an accidental policy.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Coupon.objects.create(code="BOTH", percent_off=10, amount_off=Decimal("5"))

    def test_a_percentage_above_a_hundred_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Coupon.objects.create(code="TOOMUCH", percent_off=150)


class AdminActionTests(CatalogueFixture):
    """The staff operations, which are the only self-serve path a company has.

    Worth testing rather than trusting: these are the actions somebody reaches
    for during an incident, and an admin action that raises is discovered by
    the person least able to work around it.
    """

    def setUp(self):
        super().setUp()
        self.profile = create_profile()
        self.subscription = subscriptions.subscribe(owner=self.profile, plan=factories.pro_plan())

    def request(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        request = RequestFactory().post("/admin/")
        request.user = self.staff
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_recording_a_bank_transfer_settles_the_invoice(self):
        from apps.billing.models import Invoice, InvoiceState
        from apps.billing.services import invoices

        invoice = invoices.issue(subscription=self.subscription)
        admin_class = _admin_for(Invoice)
        admin_class.record_bank_transfer(self.request(), Invoice.objects.filter(pk=invoice.pk))

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, InvoiceState.PAID)

    def test_voiding_from_the_admin_closes_an_unpaid_invoice(self):
        from apps.billing.models import Invoice, InvoiceState
        from apps.billing.services import invoices

        invoice = invoices.issue(subscription=self.subscription)
        admin_class = _admin_for(Invoice)
        admin_class.void_invoice(self.request(), Invoice.objects.filter(pk=invoice.pk))

        invoice.refresh_from_db()
        self.assertEqual(invoice.state, InvoiceState.VOID)

    def test_cancelling_immediately_from_the_admin_ends_the_plan(self):
        from apps.billing.models import Subscription, SubscriptionState

        admin_class = _admin_for(Subscription)
        admin_class.cancel_immediately(
            self.request(), Subscription.objects.filter(pk=self.subscription.pk)
        )

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.state, SubscriptionState.CANCELLED)

    def test_the_money_records_cannot_be_edited_in_a_form(self):
        # A record of money that can be edited in a form is not a record.
        from apps.billing.models import Invoice, Payment, Subscription

        for model in (Subscription, Invoice, Payment):
            admin_class = _admin_for(model)
            self.assertFalse(admin_class.has_add_permission(self.request()))
            self.assertFalse(admin_class.has_change_permission(self.request()))
            self.assertFalse(admin_class.has_delete_permission(self.request()))


def _admin_for(model):
    """The registered admin instance, so an action is exercised as it ships."""
    from django.contrib import admin as django_admin

    return django_admin.site.get_model_admin(model)


class PolicyTests(CatalogueFixture):
    def setUp(self):
        super().setUp()
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)

    def test_an_owner_may_manage_billing(self):
        self.assertTrue(policies.can_manage_company_billing(self.membership))
        policies.require_company_billing(self.membership)

    def test_a_company_admin_may_not(self):
        admin = add_member(self.company, create_profile().user, role=Role.ADMIN)

        self.assertFalse(policies.can_manage_company_billing(admin))
        with self.assertRaises(NotAllowed):
            policies.require_company_billing(admin)

    def test_a_non_member_may_not(self):
        with self.assertRaises(NotAllowed):
            policies.require_company_billing(None)
        with self.assertRaises(NotAllowed):
            policies.require_company_view(None)

    def test_any_active_member_may_read(self):
        recruiter = add_member(self.company, create_profile().user, role=Role.RECRUITER)
        policies.require_company_view(recruiter)

    def test_a_candidates_subscription_is_their_own(self):
        profile = create_profile()
        subscription = subscriptions.subscribe(owner=profile, plan=factories.pro_plan())

        self.assertIs(policies.require_own_subscription(subscription, profile), subscription)

    def test_a_strangers_subscription_is_not_found_rather_than_forbidden(self):
        # A 403 would confirm the subscription exists, which is a fact about
        # somebody else's account.
        profile = create_profile()
        subscription = subscriptions.subscribe(owner=profile, plan=factories.pro_plan())

        with self.assertRaises(NotFound):
            policies.require_own_subscription(subscription, create_profile())
        with self.assertRaises(NotFound):
            policies.require_own_subscription(subscription, None)
