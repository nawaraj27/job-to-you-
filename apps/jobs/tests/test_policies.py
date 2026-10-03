"""
Authorization and tenant isolation for jobs (SRS §2.3, ADR-A18).

Every rule is asserted from the failing side. A permission whose negative case
is untested does not count as implemented (DEVELOPER.md §8.1) — the positive
case passes just as well when the check is missing entirely.

The distinction this file exists to protect: a foreign company's posting is
**NotFound**, not NotAllowed. A 403 confirms that a job exists inside a company
the actor cannot see, which is a disclosure in a two-sided marketplace.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.common.errors import InvalidState, NotAllowed, NotFound
from apps.companies.permissions import Grant, Role
from apps.companies.tests.factories import add_member, company_with_owner, create_company
from apps.jobs import policies, selectors
from apps.jobs.models import Job, JobStatus
from apps.jobs.services import lifecycle, postings
from apps.jobs.tests.factories import create_job, publish


class CreateAccessTests(TestCase):
    """SRS §2.3 Jobs column: only Owner, Company Admin and Recruiter create postings.

    `require_can_manage_jobs` used to check membership alone, so a Viewer or an
    Interviewer could create draft postings by posting the form.
    """

    def setUp(self):
        self.company, self.owner_membership = company_with_owner(create_user())

    def member(self, role):
        return add_member(self.company, create_user(email=f"{role}@example.test"), role)

    def test_owner_admin_and_recruiter_may_create(self):
        for role in (Role.ADMIN, Role.RECRUITER):
            with self.subTest(role=role):
                policies.require_can_manage_jobs(self.member(role))
        policies.require_can_manage_jobs(self.owner_membership)

    def test_hiring_manager_interviewer_and_viewer_may_not(self):
        for role in (Role.HIRING_MANAGER, Role.INTERVIEWER, Role.VIEWER):
            with self.subTest(role=role), self.assertRaises(NotAllowed):
                policies.require_can_manage_jobs(self.member(role))

    def test_an_explicit_write_any_grant_allows_it(self):
        membership = self.member(Role.VIEWER)
        membership.permissions = [Grant.JOB_WRITE_ANY]
        membership.save()
        membership.refresh_from_db()
        self.assertTrue(policies.can_create_jobs(membership))


class WriteAccessTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner)
        self.job = create_job(self.company)

        self.recruiter = create_user(email="recruiter@example.test")
        self.recruiter_membership = add_member(self.company, self.recruiter, Role.RECRUITER)

    def test_a_recruiter_does_not_hold_company_wide_job_write(self):
        # The premise of assignment-based access. If this ever changes, the
        # assignment tests below stop proving anything.
        self.assertNotIn(Grant.JOB_WRITE_ANY, self.recruiter_membership.grants)

    def test_an_unassigned_recruiter_cannot_write_a_posting(self):
        with self.assertRaises(NotAllowed):
            policies.require_can_write(self.job, self.recruiter_membership)

    def test_assignment_grants_write_on_that_posting_only(self):
        postings.assign(
            job=self.job,
            actor=self.owner,
            membership=self.recruiter_membership,
        )
        policies.require_can_write(self.job, self.recruiter_membership)

        other = create_job(self.company)
        with self.assertRaises(NotAllowed):
            policies.require_can_write(other, self.recruiter_membership)

    def test_a_company_wide_grant_needs_no_assignment(self):
        policies.require_can_write(self.job, self.owner_membership)

    def test_publishing_needs_its_own_grant_even_when_assigned(self):
        interviewer = create_user(email="interviewer@example.test")
        membership = add_member(self.company, interviewer, Role.INTERVIEWER)
        postings.assign(job=self.job, actor=self.owner, membership=membership)

        # Assigned, so they may write it — but making it public is a separate
        # decision, because a live job is the company speaking.
        policies.require_can_write(self.job, membership)
        with self.assertRaises(NotAllowed):
            policies.require_can_publish(self.job, membership)

    def test_a_suspended_member_loses_write_access(self):
        postings.assign(job=self.job, actor=self.owner, membership=self.recruiter_membership)
        self.recruiter_membership.status = self.recruiter_membership.Status.SUSPENDED
        self.recruiter_membership.save(update_fields=["status"])

        # The assignment row survives, because a suspension is reversible; the
        # access it grants does not.
        self.assertFalse(policies.can_write(self.job, self.recruiter_membership))


class TenantIsolationTests(TestCase):
    """One company must not be able to see or touch another's postings."""

    def setUp(self):
        self.mine, self.my_membership = company_with_owner(create_user())
        self.theirs, self.their_membership = company_with_owner(
            create_user(email="them@example.test")
        )
        self.their_job = create_job(self.theirs)

    def test_a_foreign_posting_is_not_found_rather_than_forbidden(self):
        with self.assertRaises(NotFound):
            policies.require_can_write(self.their_job, self.my_membership)

    def test_a_company_scoped_read_never_returns_another_tenant(self):
        self.assertIsNone(selectors.job_for_company(self.mine, self.their_job.public_id))
        self.assertIsNotNone(selectors.job_for_company(self.theirs, self.their_job.public_id))

    def test_the_company_job_list_is_scoped(self):
        create_job(self.mine)
        mine = list(selectors.jobs_for_company(self.mine))
        self.assertEqual(len(mine), 1)
        self.assertNotIn(self.their_job, mine)

    def test_a_member_of_another_company_cannot_be_assigned(self):
        with self.assertRaises(NotFound):
            postings.assign(
                job=self.their_job,
                actor=self.their_membership.user,
                membership=self.my_membership,
            )

    def test_counts_are_scoped_to_one_company(self):
        publish(create_job(self.mine))
        counts = selectors.counts_for_company(self.theirs)
        self.assertEqual(counts["published"], 0)
        self.assertEqual(counts["drafts"], 1)


class PublicVisibilityTests(TestCase):
    def setUp(self):
        self.company, self.membership = company_with_owner(create_user())

    def test_a_draft_is_invisible_to_the_public(self):
        create_job(self.company)
        self.assertEqual(Job.objects.published().count(), 0)

    def test_an_unpublished_posting_is_not_found_rather_than_forbidden(self):
        job = create_job(self.company)
        with self.assertRaises(NotFound):
            policies.require_visible(job)

    def test_a_suspended_company_disappears_from_public_reads(self):
        job = publish(create_job(self.company))
        self.assertIn(job, Job.objects.published())

        from apps.companies.models import CompanyState

        self.company.state = CompanyState.SUSPENDED
        self.company.save(update_fields=["state"])

        # A suspension that leaves jobs reachable is not a suspension.
        self.assertNotIn(job, Job.objects.published())
        self.assertIsNone(selectors.published_job(job.slug))

    def test_a_deleted_posting_disappears_everywhere(self):
        job = publish(create_job(self.company))
        postings.delete_job(job=job, actor=self.membership.user)

        self.assertIsNone(selectors.published_job(job.slug))
        self.assertIsNone(selectors.job_for_company(self.company, job.public_id))

    def test_an_archived_posting_cannot_be_edited_until_it_is_restored(self):
        job = create_job(self.company)
        lifecycle.archive(job=job, actor=self.membership.user, membership=self.membership)
        with self.assertRaises(InvalidState):
            policies.require_editable(job)

    def test_a_company_board_shows_only_published_roles(self):
        publish(create_job(self.company))
        create_job(self.company)
        self.assertEqual(selectors.board_for_company(self.company).count(), 1)


class MembershiplessTests(TestCase):
    """Signed in, but not a member of anything."""

    def test_no_membership_means_no_write(self):
        company = create_company()
        job = create_job(company)
        self.assertFalse(policies.can_write(job, None))
        with self.assertRaises(NotFound):
            policies.require_can_write(job, None)

    def test_no_membership_cannot_create_a_posting(self):
        # NotFound, not NotAllowed: a stranger is not told the company exists.
        with self.assertRaises(NotFound):
            policies.require_can_manage_jobs(None)

    def test_status_is_never_writable_through_the_edit_path(self):
        company, membership = company_with_owner(create_user())
        job = create_job(company)
        # `status` is not in EDITABLE, so a hand-crafted POST cannot reach it —
        # the field is dropped, and an edit carrying nothing else is refused
        # rather than silently succeeding.
        self.assertNotIn("status", postings.EDITABLE)
        with self.assertRaises(InvalidState):
            postings.update_job(job=job, actor=membership.user, status=JobStatus.PUBLISHED)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.DRAFT)
