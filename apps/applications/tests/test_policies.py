"""
Authorization for applications (SRS §2.3, §2.5, INV-4, ADR-A34).

Every rule from the failing side. The distinction under test throughout is
**NotFound rather than NotAllowed**: a 403 on another company's application
confirms that it exists, and in a two-sided marketplace that is a disclosure.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.applications import policies
from apps.applications.services import pipeline as pipeline_service
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotAllowed, NotFound
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.jobs.tests.factories import create_job, publish


class ReadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())

    def test_a_member_of_this_company_may_read_it(self):
        self.assertTrue(policies.can_read(self.application, self.membership))
        self.assertIs(
            policies.require_can_read(self.application, self.membership), self.application
        )

    def test_no_membership_means_not_found(self):
        self.assertFalse(policies.can_read(self.application, None))
        with self.assertRaises(NotFound):
            policies.require_can_read(self.application, None)

    def test_another_companys_membership_means_not_found(self):
        _, theirs = company_with_owner(create_user(email="them@example.test"))
        self.assertFalse(policies.can_read(self.application, theirs))
        with self.assertRaises(NotFound):
            policies.require_can_read(self.application, theirs)

    def test_a_suspended_member_may_not_read(self):
        self.membership.status = self.membership.Status.SUSPENDED
        self.membership.save(update_fields=["status"])
        self.assertFalse(policies.can_read(self.application, self.membership))

    def test_strict_scoping_hides_unassigned_jobs(self):
        self.company.strict_assignment_scoping = True
        self.company.save(update_fields=["strict_assignment_scoping"])

        manager = create_user(email="manager@example.test")
        membership = add_member(self.company, manager, Role.HIRING_MANAGER)

        self.assertFalse(policies.can_read(self.application, membership))

    def test_strict_scoping_lets_an_assigned_member_read(self):
        self.company.strict_assignment_scoping = True
        self.company.save(update_fields=["strict_assignment_scoping"])

        manager = create_user(email="assigned-manager@example.test")
        membership = add_member(self.company, manager, Role.HIRING_MANAGER)

        from apps.jobs.services import postings

        postings.assign(job=self.job, actor=self.owner, membership=membership)
        self.assertTrue(policies.can_read(self.application, membership))

    def test_strict_scoping_does_not_narrow_an_explicit_read_any(self):
        # The grant held on the membership itself widens a strict company back
        # out. The Recruiter preset carrying it cannot — or the setting would
        # narrow nobody it was written for (SRS §2.3).
        self.company.strict_assignment_scoping = True
        self.company.save(update_fields=["strict_assignment_scoping"])

        recruiter = create_user(email="recruiter@example.test")
        membership = add_member(self.company, recruiter, Role.RECRUITER)
        self.assertFalse(policies.can_read(self.application, membership))

        membership.permissions = ["application.read_any"]
        membership.save(update_fields=["permissions"])
        self.assertTrue(policies.can_read(self.application, membership))


class DecisionTests(TestCase):
    def setUp(self):
        self.company, self.membership = company_with_owner(create_user())

    def test_deciding_needs_the_grant(self):
        interviewer = add_member(
            self.company, create_user(email="i@example.test"), Role.INTERVIEWER
        )
        with self.assertRaises(NotAllowed):
            policies.require_can_decide(interviewer)

    def test_an_owner_may_decide(self):
        self.assertIsNone(policies.require_can_decide(self.membership))

    def test_offering_needs_the_grant(self):
        recruiter = add_member(self.company, create_user(email="r@example.test"), Role.RECRUITER)
        with self.assertRaises(NotAllowed):
            policies.require_can_offer(recruiter)

    def test_an_owner_may_offer(self):
        self.assertIsNone(policies.require_can_offer(self.membership))


class OwnerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.company, self.membership = company_with_owner(create_user())
        self.job = publish(create_job(self.company))
        self.profile = create_profile()
        self.application = submit(job=self.job, profile=self.profile)

    def test_the_owner_of_an_application_may_read_it(self):
        self.assertIs(policies.require_owner(self.application, self.profile), self.application)

    def test_a_stranger_gets_not_found(self):
        with self.assertRaises(NotFound):
            policies.require_owner(self.application, create_profile())

    def test_an_anonymous_reader_gets_not_found(self):
        with self.assertRaises(NotFound):
            policies.require_owner(self.application, None)

    def test_a_live_application_passes_the_live_check(self):
        self.assertIs(policies.require_live(self.application), self.application)

    def test_a_withdrawn_application_fails_it(self):
        from apps.applications.services import apply as apply_service

        apply_service.withdraw(application=self.application, actor=self.profile.user)
        with self.assertRaises(InvalidState):
            policies.require_live(self.application)

    def test_a_closed_application_fails_it(self):
        pipeline_service.move(
            application=self.application,
            actor=self.membership.user,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "declined"),
            reason="No",
        )
        with self.assertRaises(InvalidState):
            policies.require_live(self.application)


class ContactTests(TestCase):
    """ADR-A34, at the boundary that would leak it."""

    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())

    def test_contact_is_locked_on_a_new_application(self):
        self.assertFalse(policies.can_see_contact(self.application))
        with self.assertRaises(NotAllowed):
            policies.require_contact_revealed(self.application)

    def test_contact_unlocks_with_the_stage(self):
        pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, "interview"),
        )
        self.application.refresh_from_db()

        self.assertTrue(policies.can_see_contact(self.application))
        self.assertIsNone(policies.require_contact_revealed(self.application))
