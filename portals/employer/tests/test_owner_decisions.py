"""
Owner decisions D-11 and D-13 (SRS §0.4, §2.3), asserted on real URLs and POSTs.

D-11 — a Company Admin sees the plan and cannot change it; the Owner keeps plan
management, and nobody can hand `billing.manage` to somebody else without
holding it.

D-13 — a Recruiter reaches applicants only on the jobs they are assigned to:
the board, the detail, every action, offers, message threads started from an
application, offer letters, talent search and notifications. Owner and Company
Admin reach every applicant; an explicit `application.read_any` widens one
member; nothing crosses a company boundary.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.applications import policies as application_policies
from apps.applications.models import ApplicationNote, GrantBasis
from apps.applications.services import offers as offer_service
from apps.applications.services import visibility as visibility_service
from apps.billing.models import SubscriptionState
from apps.billing.services import subscriptions
from apps.billing.tests import factories as billing_factories
from apps.candidates.models import Visibility
from apps.candidates.services import talent as talent_service
from apps.candidates.tests.factories import create_profile
from apps.candidates.tests.test_talent import discoverable
from apps.common.errors import NotAllowed, NotFound
from apps.companies.permissions import Grant, Role
from apps.companies.services import membership as membership_service
from apps.companies.tests.factories import add_member, company_with_owner
from apps.files import policies as file_policies
from apps.files.constants import ScanStatus
from apps.files.tests import factories as file_factories
from apps.jobs.models import JobAssignment
from apps.matching.tests.test_matching import application_for
from apps.messaging.models import SenderKind
from apps.messaging.services import conversations as conversation_service
from apps.notifications.models import OutboxEvent
from apps.notifications.services import audiences
from portals.employer.tests.test_role_matrix import MatrixFixture


class RecruiterAssignmentScopeTests(MatrixFixture):
    """D-13 on every applicant surface. `self.job` is assigned; `self.other_job` is not."""

    def setUp(self):
        super().setUp()
        self.assign(Role.RECRUITER)
        self.recruiter = self.members[Role.RECRUITER]

    def test_the_recruiter_preset_no_longer_carries_company_wide_read(self):
        self.assertNotIn(Grant.APPLICATION_READ_ANY, self.recruiter.grants)
        self.assertEqual(
            application_policies.read_scope(self.recruiter), application_policies.SCOPE_ASSIGNED
        )

    def test_an_assigned_job_opens_and_can_be_worked(self):
        self.as_role(Role.RECRUITER)

        self.assertEqual(self.client.get(self.detail()).status_code, 200)
        response = self.client.post(self.act(), {"action": "note", "body": "Strong on Go."})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ApplicationNote.objects.filter(application=self.application).exists())

    def test_an_unassigned_job_is_invisible_on_every_route(self):
        self.as_role(Role.RECRUITER)
        other = self.other_application
        name = other.candidate.user.full_name

        self.assertEqual(self.client.get(self.detail(other)).status_code, 404)
        self.assertEqual(
            self.client.post(self.act(other), {"action": "note", "body": "x"}).status_code, 404
        )
        self.assertFalse(ApplicationNote.objects.filter(application=other).exists())
        offer = self.url("employer:application_offer", other.public_id)
        self.assertEqual(self.client.post(offer, {"salary": "100"}).status_code, 404)
        message = self.url("employer:application_message", other.public_id)
        self.assertEqual(self.client.post(message, {"body": "Hello"}).status_code, 404)

        for page in ("employer:applicants", "employer:overview", "employer:interviews"):
            with self.subTest(page=page):
                self.assertNotContains(self.client.get(self.url(page)), name)
        board = self.client.get(self.url("employer:applicants"))
        self.assertContains(board, self.candidate.user.full_name)

    def test_a_bulk_move_cannot_reach_an_unassigned_applicant(self):
        self.as_role(Role.RECRUITER)
        before = self.other_application.stage_id

        self.client.post(
            self.url("employer:applicants"),
            {
                "action": "move",
                "stage": "reviewing",
                "application": [str(self.other_application.public_id)],
            },
        )

        self.other_application.refresh_from_db()
        self.assertEqual(self.other_application.stage_id, before)

    def test_owner_and_admin_reach_every_applicant(self):
        for role in ("owner", Role.ADMIN):
            with self.subTest(role=role):
                self.as_role(role)
                self.assertEqual(
                    self.client.get(self.detail(self.other_application)).status_code, 200
                )

    def test_an_explicit_grant_widens_one_recruiter(self):
        self.recruiter.permissions = [Grant.APPLICATION_READ_ANY]
        self.recruiter.save()
        self.as_role(Role.RECRUITER)

        self.assertEqual(self.client.get(self.detail(self.other_application)).status_code, 200)

    def test_assignment_follows_the_job_not_the_moment(self):
        # Assigning later opens it; removing the assignment closes it again.
        self.as_role(Role.RECRUITER)
        assignment = JobAssignment.objects.create(job=self.other_job, membership=self.recruiter)
        self.assertEqual(self.client.get(self.detail(self.other_application)).status_code, 200)
        assignment.delete()
        self.assertEqual(self.client.get(self.detail(self.other_application)).status_code, 404)

    def test_analytics_aggregates_stay_open_to_recruiters(self):
        self.as_role(Role.RECRUITER)
        self.assertEqual(self.client.get(self.url("employer:analytics")).status_code, 200)

    # --- messaging ---------------------------------------------------------

    def _thread(self, application):
        visibility_service.grant(
            candidate=application.candidate,
            company=self.company,
            basis=GrantBasis.APPLICATION,
            application=application,
        )
        thread = conversation_service.open_conversation(
            company=self.company,
            candidate=application.candidate,
            actor=self.owner,
            membership=self.owner_membership,
            job=application.job,
            application=application,
        )
        conversation_service.send(
            conversation=thread,
            actor=application.candidate.user,
            sender_kind=SenderKind.CANDIDATE,
            body="Thanks for getting in touch.",
            uploads=[file_factories.pdf_upload("portfolio.pdf")],
        )
        return thread

    def test_threads_about_unassigned_applicants_are_not_in_the_inbox(self):
        mine = self._thread(self.application)
        theirs = self._thread(self.other_application)
        self.as_role(Role.RECRUITER)
        inbox = self.url("employer:messages")

        listed = {thread.pk for thread in self.client.get(inbox).context["threads"]}
        self.assertIn(mine.pk, listed)
        self.assertNotIn(theirs.pk, listed)
        self.assertEqual(self.client.get(inbox, {"thread": str(theirs.public_id)}).status_code, 404)
        self.assertEqual(
            self.client.post(inbox, {"thread": str(theirs.public_id), "body": "Hi"}).status_code,
            404,
        )

    def test_candidate_files_and_notices_go_only_to_readers_of_the_application(self):
        theirs = self._thread(self.other_application)
        attachment = theirs.messages.first().attachments.first()
        attachment.scan_status = ScanStatus.CLEAN
        attachment.save(update_fields=["scan_status"])

        self.assertFalse(file_policies.can_access(self.recruiter.user, attachment))
        self.assertTrue(file_policies.can_access(self.members[Role.ADMIN].user, attachment))

        event = OutboxEvent(
            type="message.sent",
            payload={
                "conversation_public_id": str(theirs.public_id),
                "company_id": self.company.pk,
                "sender_kind": SenderKind.CANDIDATE,
            },
        )
        recipients = set(audiences.resolve("conversation_other_side", event))
        self.assertIn(self.owner, recipients)
        self.assertNotIn(self.recruiter.user, recipients)
        # Roles that cannot open the inbox were notified about threads too.
        self.assertNotIn(self.members[Role.VIEWER].user, recipients)

    def test_a_recruiter_assigned_later_can_open_earlier_files(self):
        theirs = self._thread(self.other_application)
        attachment = theirs.messages.first().attachments.first()
        attachment.scan_status = ScanStatus.CLEAN
        attachment.save(update_fields=["scan_status"])
        JobAssignment.objects.create(job=self.other_job, membership=self.recruiter)
        self.as_role(Role.RECRUITER)

        self.client.get(self.url("employer:messages"), {"thread": str(theirs.public_id)})

        self.assertTrue(file_policies.can_access(self.recruiter.user, attachment))

    # --- notifications -----------------------------------------------------

    def test_application_notices_go_to_the_people_who_can_open_them(self):
        event = OutboxEvent(
            type="application.submitted",
            payload={"application_public_id": str(self.other_application.public_id)},
        )
        recipients = set(audiences.resolve("application_company_team", event))

        self.assertIn(self.owner, recipients)
        self.assertIn(self.members[Role.ADMIN].user, recipients)
        for role in (Role.RECRUITER, Role.HIRING_MANAGER, Role.INTERVIEWER, Role.VIEWER):
            with self.subTest(role=role):
                self.assertNotIn(self.members[role].user, recipients)

        mine = OutboxEvent(
            type="application.submitted",
            payload={"application_public_id": str(self.application.public_id)},
        )
        self.assertIn(self.recruiter.user, set(audiences.resolve("application_company_team", mine)))

    # --- offer letters -----------------------------------------------------

    def test_an_offer_letter_is_read_only_by_readers_of_the_application(self):
        offer = offer_service.draft_offer(
            application=self.other_application,
            actor=self.owner,
            membership=self.owner_membership,
            salary="1200000",
            currency="NPR",
            document=file_factories.pdf_upload("offer.pdf"),
        )
        letter = offer.attachment
        letter.scan_status = ScanStatus.CLEAN
        letter.save(update_fields=["scan_status"])

        self.assertTrue(file_policies.can_access(self.owner, letter))
        self.assertFalse(file_policies.can_access(self.recruiter.user, letter))
        JobAssignment.objects.create(job=self.other_job, membership=self.recruiter)
        self.assertTrue(file_policies.can_access(self.recruiter.user, letter))

    # --- talent search -----------------------------------------------------

    def test_search_does_not_surface_applicants_of_unassigned_jobs(self):
        applicant = discoverable(visibility=Visibility.EMPLOYERS)
        application = application_for(applicant, self.other_job)
        visibility_service.grant(
            candidate=applicant,
            company=self.company,
            basis=GrantBasis.APPLICATION,
            application=application,
        )

        self.assertIn(applicant, talent_service.searchable_for(self.company, self.owner_membership))
        self.assertNotIn(applicant, talent_service.searchable_for(self.company, self.recruiter))
        with self.assertRaises(NotFound):
            talent_service.view_profile(
                membership=self.recruiter,
                actor=self.recruiter.user,
                public_id=str(applicant.public_id),
            )
        self.as_role(Role.RECRUITER)
        profile = self.url("employer:talent_profile", applicant.public_id)
        self.assertEqual(self.client.get(profile).status_code, 404)

        JobAssignment.objects.create(job=self.other_job, membership=self.recruiter)
        self.assertIn(applicant, talent_service.searchable_for(self.company, self.recruiter))


class CrossCompanyApplicantTests(MatrixFixture):
    """No role in another company reaches these applicants, by either URL."""

    def setUp(self):
        super().setUp()
        self.stranger = create_profile().user
        self.stranger_company, self.stranger_owner = company_with_owner(self.stranger)
        self.stranger_recruiter = add_member(
            self.stranger_company, create_profile().user, role=Role.RECRUITER
        )

    def test_a_foreign_owner_and_recruiter_see_nothing(self):
        for member in (self.stranger_owner, self.stranger_recruiter):
            with self.subTest(role=member.role):
                self.client.force_login(member.user)
                # Under this company's slug: not a member.
                self.assertIn(self.client.get(self.detail()).status_code, (403, 404))
                # Under their own slug, with this company's application id.
                foreign = reverse(
                    "employer:application_detail",
                    args=[self.stranger_company.slug, self.application.public_id],
                )
                self.assertEqual(self.client.get(foreign).status_code, 404)
                self.assertFalse(application_policies.can_read(self.application, member))

    def test_foreign_members_are_never_notified(self):
        event = OutboxEvent(
            type="application.submitted",
            payload={"application_public_id": str(self.application.public_id)},
        )
        recipients = set(audiences.resolve("application_company_team", event))
        self.assertNotIn(self.stranger, recipients)
        self.assertNotIn(self.stranger_recruiter.user, recipients)


class CompanyAdminBillingTests(TestCase):
    """D-11."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, self.owner_membership = company_with_owner(self.owner)
        self.admin = add_member(self.company, create_profile().user, role=Role.ADMIN)
        # A priced company plan: the catalogue's employer plan is priced by
        # agreement and cannot be invoiced automatically.
        self.subscription = subscriptions.subscribe(
            owner=self.company,
            plan=billing_factories.make_plan(
                code="agreed", audience="company", price=10, currency="USD"
            ),
        )
        self.url = reverse("employer:billing", args=[self.company.slug])

    def test_the_admin_sees_plan_status_and_invoices_without_controls(self):
        from apps.billing.services import invoices

        invoice = invoices.issue(subscription=self.subscription)
        self.client.force_login(self.admin.user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage"])
        self.assertContains(response, self.subscription.plan.name)
        self.assertContains(response, f"Status: {self.subscription.get_state_display()}")
        self.assertContains(response, invoice.number)
        self.assertNotContains(response, 'name="action"')
        self.assertContains(response, "Only a company owner can change")

    def test_the_admin_is_refused_every_write(self):
        self.client.force_login(self.admin.user)
        for action in ("cancel", "resume", "upgrade", ""):
            with self.subTest(action=action):
                self.assertEqual(self.client.post(self.url, {"action": action}).status_code, 403)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.cancel_at)
        self.assertEqual(self.subscription.state, SubscriptionState.ACTIVE)

    def test_the_owner_keeps_plan_management(self):
        self.client.force_login(self.owner)
        page = self.client.get(self.url)
        self.assertContains(page, 'value="cancel"')

        self.client.post(self.url, {"action": "cancel"})

        self.subscription.refresh_from_db()
        self.assertIsNotNone(self.subscription.cancel_at)

    def test_roles_below_admin_and_other_companies_cannot_write(self):
        for role in (Role.RECRUITER, Role.HIRING_MANAGER, Role.INTERVIEWER, Role.VIEWER):
            member = add_member(self.company, create_profile().user, role=role)
            with self.subTest(role=role):
                self.client.force_login(member.user)
                self.assertEqual(self.client.post(self.url, {"action": "cancel"}).status_code, 403)

        stranger = create_profile().user
        company_with_owner(stranger)
        self.client.force_login(stranger)
        self.assertIn(self.client.post(self.url, {"action": "cancel"}).status_code, (403, 404))

        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.cancel_at)

    def test_an_admin_cannot_hand_out_plan_management(self):
        with self.assertRaises(NotAllowed):
            membership_service.invite(
                actor=self.admin.user,
                actor_membership=self.admin,
                company=self.company,
                email="new.person@example.com",
                role=Role.RECRUITER,
                permissions=[Grant.BILLING_MANAGE],
            )
        with self.assertRaises(NotAllowed):
            membership_service.change_role(
                actor=self.admin.user,
                actor_membership=self.admin,
                target=self.admin,
                role=Role.ADMIN,
                permissions=[Grant.BILLING_MANAGE],
            )
        self.admin.refresh_from_db()
        self.assertNotIn(Grant.BILLING_MANAGE, self.admin.grants)

    def test_the_owner_may_still_delegate_explicitly(self):
        membership_service.change_role(
            actor=self.owner,
            actor_membership=self.owner_membership,
            target=self.admin,
            role=Role.ADMIN,
            permissions=[Grant.BILLING_MANAGE],
        )
        self.admin.refresh_from_db()
        self.assertIn(Grant.BILLING_MANAGE, self.admin.grants)
