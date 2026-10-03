"""
Authorization and tenancy (SRS §2, ADR-A1, ADR-A18).

Every rule is asserted from the **failing** side. A permission whose negative
case is untested does not count as implemented (DEVELOPER.md §8.1), because the
positive case passes just as well when the check is missing entirely.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.common.errors import Conflict, InvalidState, NotAllowed, NotFound
from apps.companies import permissions, policies, selectors
from apps.companies.models import Company, CompanyMembership, CompanyState
from apps.companies.permissions import Grant, Role
from apps.companies.services import company as company_service
from apps.companies.services import membership as membership_service
from apps.companies.tests.factories import add_member, company_with_owner, create_company


class GrantMatrixTests(TestCase):
    def test_every_role_has_a_declared_grant_set(self):
        for role in Role.values:
            with self.subTest(role=role):
                self.assertIn(role, permissions.ROLE_GRANTS)

    def test_owner_holds_every_grant(self):
        self.assertEqual(permissions.ROLE_GRANTS[Role.OWNER], frozenset(Grant.values))

    def test_admin_cannot_manage_billing(self):
        self.assertNotIn(Grant.BILLING_MANAGE, permissions.ROLE_GRANTS[Role.ADMIN])

    def test_interviewer_holds_nothing(self):
        # The narrowest role in the system. If this ever gains a grant by
        # accident, an interviewer can act on candidates they were never shown.
        self.assertEqual(permissions.ROLE_GRANTS[Role.INTERVIEWER], frozenset())

    def test_unknown_grant_key_is_rejected_rather_than_ignored(self):
        # Silently returning False for a typo would look like a working
        # permission check that never grants anything.
        user = create_user()
        _, membership = company_with_owner(user)
        with self.assertRaises(ValueError):
            permissions.has(membership, "job.destroy_everything")

    def test_membership_permissions_are_validated_on_write(self):
        user = create_user()
        company = create_company()
        with self.assertRaises(ValueError):
            add_member(company, user, Role.RECRUITER, permissions=["not.a.grant"])

    def test_extra_grants_layer_on_top_of_the_role_preset(self):
        user = create_user()
        company = create_company()
        membership = add_member(
            company, user, Role.HIRING_MANAGER, permissions=[Grant.APPLICATION_DECIDE]
        )
        self.assertTrue(permissions.has(membership, Grant.APPLICATION_DECIDE))
        # A Hiring Manager reads only applicants to assigned jobs (SRS §2.3), so
        # the preset carries no company-wide read.
        self.assertFalse(permissions.has(membership, Grant.APPLICATION_READ_ANY))


class MembershipStateTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.company, self.membership = company_with_owner(self.user)

    def test_a_suspended_membership_carries_no_grants(self):
        self.membership.status = CompanyMembership.Status.SUSPENDED
        self.membership.save()

        self.assertFalse(permissions.has(self.membership, Grant.TEAM_INVITE))

    def test_a_removed_membership_carries_no_grants(self):
        self.membership.deleted_at = self.membership.created_at
        self.membership.save()

        self.assertFalse(permissions.has(self.membership, Grant.TEAM_INVITE))

    def test_a_suspended_company_disables_every_membership_at_once(self):
        # One column change must lock every surface, or suspension is a promise
        # each view has to remember to keep.
        self.company.state = CompanyState.SUSPENDED
        self.company.save()
        self.membership.refresh_from_db()

        self.assertFalse(permissions.has(self.membership, Grant.TEAM_INVITE))

    def test_no_membership_at_all_carries_no_grants(self):
        self.assertFalse(permissions.has(None, Grant.TEAM_INVITE))


class ScopingTests(TestCase):
    """Layer 2 — the actual security boundary (SRS §2.5)."""

    def setUp(self):
        self.insider = create_user()
        self.outsider = create_user()
        self.company, _ = company_with_owner(self.insider)
        self.other_company, _ = company_with_owner(self.outsider)

    def test_companies_for_returns_only_the_user_s_own(self):
        self.assertEqual(list(selectors.companies_for(self.insider)), [self.company])

    def test_an_outsider_sees_none_of_them(self):
        self.assertNotIn(self.company, selectors.companies_for(self.outsider))

    def test_an_anonymous_visitor_sees_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(list(selectors.companies_for(AnonymousUser())), [])

    def test_a_revoked_member_is_scoped_out_immediately(self):
        membership = selectors.membership_for(user=self.insider, company=self.company)
        membership.deleted_at = membership.created_at
        membership.save()

        self.assertEqual(list(selectors.companies_for(self.insider)), [])
        self.assertIsNone(selectors.membership_for(user=self.insider, company=self.company))

    def test_a_suspended_company_is_scoped_out(self):
        Company.objects.filter(pk=self.company.pk).update(state=CompanyState.SUSPENDED)
        membership = selectors.membership_for(user=self.insider, company=self.company)

        self.assertFalse(membership.is_active)

    def test_membership_lookup_is_scoped_by_slug_and_user_together(self):
        # The tenancy primitive. Resolving a slug without the user would let
        # anyone address any company by guessing its name.
        self.assertIsNone(selectors.membership_by_slug(user=self.outsider, slug=self.company.slug))
        self.assertIsNotNone(
            selectors.membership_by_slug(user=self.insider, slug=self.company.slug)
        )


class PolicyNegativeTests(TestCase):
    """One failing case per policy rule."""

    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner)

        self.recruiter = create_user()
        self.recruiter_membership = add_member(self.company, self.recruiter, Role.RECRUITER)

    def test_missing_membership_raises_not_found_not_not_allowed(self):
        # A 403 confirms the company exists to someone with no right to know.
        with self.assertRaises(NotFound):
            policies.require_membership(None)

    def test_recruiter_cannot_invite(self):
        with self.assertRaises(NotAllowed):
            policies.require_can_invite(self.recruiter_membership)

    def test_recruiter_cannot_edit_the_company(self):
        with self.assertRaises(NotAllowed):
            policies.require_can_edit_company(self.recruiter_membership)

    def test_admin_cannot_create_an_owner(self):
        # Otherwise an admin promotes themselves and removes the owner who
        # invited them, using only legitimate calls.
        admin = create_user()
        admin_membership = add_member(self.company, admin, Role.ADMIN)

        with self.assertRaises(NotAllowed):
            policies.require_can_assign_role(admin_membership, Role.OWNER)

    def test_admin_cannot_remove_an_owner(self):
        admin = create_user()
        admin_membership = add_member(self.company, admin, Role.ADMIN)

        with self.assertRaises(NotAllowed):
            policies.require_can_remove(
                actor_membership=admin_membership, target=self.owner_membership
            )

    def test_acting_on_another_company_s_member_raises_not_found(self):
        stranger = create_user()
        other_company, other_membership = company_with_owner(stranger)

        with self.assertRaises(NotFound):
            policies.require_can_remove(
                actor_membership=self.owner_membership, target=other_membership
            )

    def test_a_suspended_company_refuses_writes(self):
        self.company.state = CompanyState.SUSPENDED
        with self.assertRaises(NotAllowed):
            policies.require_company_active(self.company)


class InviteTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner)

    def _invite(self, email="new@example.com", role=Role.RECRUITER):
        return membership_service.invite(
            actor=self.owner,
            actor_membership=self.owner_membership,
            company=self.company,
            email=email,
            role=role,
        )

    def test_invite_stores_only_a_hash(self):
        invite, token = self._invite()

        self.assertNotEqual(invite.token_hash, token)
        self.assertNotIn(token, invite.token_hash)

    def test_accepting_creates_a_membership(self):
        _, token = self._invite(email="new@example.com")
        invitee = create_user(email="new@example.com")

        membership = membership_service.accept_invite(user=invitee, token=token)

        self.assertEqual(membership.company, self.company)
        self.assertEqual(membership.role, Role.RECRUITER)

    def test_an_invite_cannot_be_accepted_by_a_different_address(self):
        # The classic bug: forwarded invitation, wrong account joins, and the
        # company has a member nobody can account for.
        _, token = self._invite(email="intended@example.com")
        someone_else = create_user(email="other@example.com")

        with self.assertRaises(NotAllowed) as caught:
            membership_service.accept_invite(user=someone_else, token=token)

        self.assertEqual(caught.exception.code, "invite_email_mismatch")

    def test_an_invite_cannot_be_used_twice(self):
        _, token = self._invite(email="new@example.com")
        invitee = create_user(email="new@example.com")
        membership_service.accept_invite(user=invitee, token=token)

        with self.assertRaises(NotAllowed):
            membership_service.accept_invite(user=invitee, token=token)

    def test_an_unknown_token_is_not_found(self):
        with self.assertRaises(NotFound):
            membership_service.find_invite("not-a-real-token")

    def test_inviting_an_existing_member_is_refused(self):
        member = create_user(email="already@example.com")
        add_member(self.company, member, Role.RECRUITER)

        with self.assertRaises(Conflict):
            self._invite(email="already@example.com")

    def test_re_inviting_replaces_the_outstanding_invitation(self):
        self._invite(email="new@example.com")
        self._invite(email="new@example.com")

        self.assertEqual(selectors.open_invites_for(company=self.company).count(), 1)

    def test_a_recruiter_cannot_invite(self):
        recruiter = create_user()
        recruiter_membership = add_member(self.company, recruiter, Role.RECRUITER)

        with self.assertRaises(NotAllowed):
            membership_service.invite(
                actor=recruiter,
                actor_membership=recruiter_membership,
                company=self.company,
                email="x@example.com",
                role=Role.RECRUITER,
            )

    def test_rejoining_after_removal_reuses_the_membership_row(self):
        member = create_user(email="back@example.com")
        membership = add_member(self.company, member, Role.RECRUITER)
        membership_service.remove_member(
            actor=self.owner, actor_membership=self.owner_membership, target=membership
        )

        _, token = self._invite(email="back@example.com")
        rejoined = membership_service.accept_invite(user=member, token=token)

        self.assertEqual(rejoined.pk, membership.pk)
        self.assertEqual(
            CompanyMembership.objects.filter(company=self.company, user=member).count(), 1
        )


class LastOwnerTests(TestCase):
    """A company must always have an owner (SRS §2.6)."""

    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner)

    def test_the_last_owner_cannot_be_removed(self):
        with self.assertRaises(InvalidState):
            membership_service.remove_member(
                actor=self.owner,
                actor_membership=self.owner_membership,
                target=self.owner_membership,
            )

    def test_the_last_owner_cannot_be_demoted(self):
        with self.assertRaises(InvalidState):
            membership_service.change_role(
                actor=self.owner,
                actor_membership=self.owner_membership,
                target=self.owner_membership,
                role=Role.ADMIN,
            )

    def test_an_owner_can_be_removed_once_another_exists(self):
        second = create_user()
        second_membership = add_member(self.company, second, Role.OWNER)

        membership_service.remove_member(
            actor=self.owner, actor_membership=self.owner_membership, target=second_membership
        )

        self.assertEqual(selectors.active_owner_count(company=self.company), 1)


class OwnershipTransferTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, self.owner_membership = company_with_owner(self.owner)
        self.member = create_user()
        self.member_membership = add_member(self.company, self.member, Role.RECRUITER)

    def test_transfer_promotes_the_target_and_demotes_the_owner_to_admin(self):
        # The outgoing owner keeps access: a transfer that locks them out is
        # indistinguishable from a hostile takeover and is unrecoverable.
        membership_service.transfer_ownership(
            actor=self.owner,
            actor_membership=self.owner_membership,
            target=self.member_membership,
        )
        self.owner_membership.refresh_from_db()
        self.member_membership.refresh_from_db()

        self.assertEqual(self.member_membership.role, Role.OWNER)
        self.assertEqual(self.owner_membership.role, Role.ADMIN)

    def test_a_non_owner_cannot_transfer_ownership(self):
        with self.assertRaises(InvalidState):
            membership_service.transfer_ownership(
                actor=self.member,
                actor_membership=self.member_membership,
                target=self.owner_membership,
            )

    def test_ownership_cannot_be_transferred_across_companies(self):
        stranger = create_user()
        _, other_membership = company_with_owner(stranger)

        with self.assertRaises(NotFound):
            membership_service.transfer_ownership(
                actor=self.owner,
                actor_membership=self.owner_membership,
                target=other_membership,
            )


class CompanyServiceTests(TestCase):
    def test_creating_a_company_makes_the_creator_its_owner(self):
        # A company with no owner is unreachable: nobody could invite anyone
        # into it, so the two rows must commit together.
        user = create_user()
        company = company_service.create_company(actor=user, name="Lumen Systems")

        membership = selectors.membership_for(user=user, company=company)
        self.assertEqual(membership.role, Role.OWNER)

    def test_slugs_do_not_collide(self):
        first = company_service.create_company(actor=create_user(), name="Lumen Systems")
        second = company_service.create_company(actor=create_user(), name="Lumen Systems")

        self.assertNotEqual(first.slug, second.slug)

    def test_a_stale_edit_is_refused(self):
        from apps.common.errors import StaleWrite

        user = create_user()
        company = company_service.create_company(actor=user, name="Lumen")

        company_service.update_company(
            actor=user, company=company, row_version=company.row_version, name="Lumen One"
        )

        with self.assertRaises(StaleWrite):
            company_service.update_company(
                actor=user, company=company, row_version=0, name="Lumen Two"
            )

    def test_verification_cannot_be_submitted_without_documents(self):
        user = create_user()
        company = company_service.create_company(actor=user, name="Lumen")

        with self.assertRaises(InvalidState):
            company_service.submit_verification(actor=user, company=company)

    def test_suspension_requires_a_recorded_reason(self):
        user = create_user()
        company = company_service.create_company(actor=user, name="Lumen")

        with self.assertRaises(InvalidState):
            company_service.suspend_company(actor=user, company=company, reason="")
