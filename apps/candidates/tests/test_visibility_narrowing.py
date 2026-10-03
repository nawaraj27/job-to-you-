"""
Decision D-06: narrowing visibility withdraws employer-held access.

Moving to "employers you've applied to" or to "nobody" revokes every PUBLIC,
POOL and INVITED grant and takes the candidate out of talent pools. APPLICATION
grants stay — they are the candidate's own relationship with that company —
and so do the application records. Under "nobody" no company can find or open
the profile, while the team that received an application keeps it.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.applications.models import CandidateVisibilityGrant, GrantBasis, TalentPoolMember
from apps.applications.services import pools as pool_service
from apps.applications.services import visibility as visibility_service
from apps.applications.tests.factories import seed_templates, submit
from apps.audit.models import AuditLog
from apps.candidates.models import Visibility
from apps.candidates.services import blocking
from apps.candidates.services import profile as profile_service
from apps.candidates.services import talent as talent_service
from apps.candidates.tests.test_talent import discoverable, make_searchable
from apps.common.errors import NotAllowed, NotFound
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish


class NarrowingFixture(TestCase):
    def setUp(self):
        seed_templates()
        self.candidate = discoverable(visibility=Visibility.EVERYONE)

        # One company per basis, so each grant's fate is visible on its own.
        self.public_co, self.public_member = company_with_owner(create_user())
        self.pool_co, self.pool_member = company_with_owner(create_user())
        self.invite_co, self.invite_member = company_with_owner(create_user())
        self.applied_co, self.applied_member = company_with_owner(create_user())

        # PUBLIC: the company opened the public profile.
        talent_service.view_profile(
            membership=self.public_member,
            actor=self.public_member.user,
            public_id=str(self.candidate.public_id),
        )
        # POOL: saved to a talent pool.
        self.pool = pool_service.create_pool(
            company=self.pool_co, actor=self.pool_member.user, name="Backend"
        )
        pool_service.add_member(
            pool=self.pool, candidate=self.candidate, actor=self.pool_member.user
        )
        # INVITED: the company invited them.
        visibility_service.grant(
            candidate=self.candidate, company=self.invite_co, basis=GrantBasis.INVITED
        )
        # APPLICATION: the candidate applied.
        self.application = submit(job=publish(create_job(self.applied_co)), profile=self.candidate)

    def narrow(self, to: str):
        self.candidate.refresh_from_db()
        profile_service.update_privacy(
            profile=self.candidate, actor=self.candidate.user, visibility=to
        )
        # The service recomputes completeness from real sections, which this
        # fixture does not build; restore the forced discoverability.
        self.candidate = make_searchable(self.candidate)

    def live(self, basis: str) -> bool:
        return (
            CandidateVisibilityGrant.objects.live()
            .filter(candidate=self.candidate, basis=basis)
            .exists()
        )


class NarrowToEmployersTests(NarrowingFixture):
    def setUp(self):
        super().setUp()
        for basis in (
            GrantBasis.PUBLIC,
            GrantBasis.POOL,
            GrantBasis.INVITED,
            GrantBasis.APPLICATION,
        ):
            self.assertTrue(self.live(basis), basis)
        self.narrow(Visibility.EMPLOYERS)

    def test_public_grants_are_revoked(self):
        self.assertFalse(self.live(GrantBasis.PUBLIC))
        with self.assertRaises(NotFound):
            talent_service.view_profile(
                membership=self.public_member,
                actor=self.public_member.user,
                public_id=str(self.candidate.public_id),
            )

    def test_pool_grants_are_revoked_and_the_pool_membership_removed(self):
        self.assertFalse(self.live(GrantBasis.POOL))
        member = TalentPoolMember.objects.get(pool=self.pool, candidate=self.candidate)
        self.assertIsNotNone(member.deleted_at)
        self.assertNotIn(self.candidate, talent_service.searchable_for(self.pool_co))

    def test_invited_grants_are_revoked(self):
        self.assertFalse(self.live(GrantBasis.INVITED))
        self.assertNotIn(self.candidate, talent_service.searchable_for(self.invite_co))

    def test_application_grants_remain(self):
        self.assertTrue(self.live(GrantBasis.APPLICATION))
        self.assertIn(
            self.candidate,
            talent_service.searchable_for(self.applied_co, self.applied_member),
        )
        talent_service.view_profile(
            membership=self.applied_member,
            actor=self.applied_member.user,
            public_id=str(self.candidate.public_id),
        )

    def test_each_company_s_revocation_is_audited_with_the_reason(self):
        rows = AuditLog.objects.filter(verb="candidate.visibility_revoked")
        by_company = {row.company_id: row.metadata for row in rows}
        self.assertEqual(set(by_company), {self.public_co.pk, self.pool_co.pk, self.invite_co.pk})
        self.assertEqual(by_company[self.pool_co.pk]["bases"], {"pool": 1})
        self.assertEqual(by_company[self.pool_co.pk]["pool_memberships"], 1)
        for metadata in by_company.values():
            self.assertEqual(metadata["reason"], "visibility_narrowed")
            self.assertEqual((metadata["from"], metadata["to"]), ("everyone", "employers"))


class NarrowToNobodyTests(NarrowingFixture):
    def setUp(self):
        super().setUp()
        self.narrow(Visibility.NOBODY)

    def test_only_the_application_grant_survives(self):
        self.assertFalse(self.live(GrantBasis.PUBLIC))
        self.assertFalse(self.live(GrantBasis.POOL))
        self.assertFalse(self.live(GrantBasis.INVITED))
        self.assertTrue(self.live(GrantBasis.APPLICATION))

    def test_no_company_can_find_or_open_the_profile(self):
        # SRS §2.7 "nobody — no company can read the profile", the applied
        # company included: the grant stays for the application, not for the
        # profile.
        for company, member in (
            (self.public_co, self.public_member),
            (self.pool_co, self.pool_member),
            (self.invite_co, self.invite_member),
            (self.applied_co, self.applied_member),
        ):
            with self.subTest(company=company.name):
                self.assertNotIn(self.candidate, talent_service.searchable_for(company, member))
                with self.assertRaises(NotFound):
                    talent_service.view_profile(
                        membership=member,
                        actor=member.user,
                        public_id=str(self.candidate.public_id),
                    )

    def test_the_application_record_remains_readable_to_the_team_that_received_it(self):
        self.application.refresh_from_db()
        self.client.force_login(self.applied_member.user)
        page = self.client.get(
            reverse(
                "employer:application_detail",
                args=[self.applied_co.slug, self.application.public_id],
            )
        )
        self.assertEqual(page.status_code, 200)


class NarrowingEdgeTests(NarrowingFixture):
    def test_employers_to_nobody_also_revokes_pool_and_invitation_grants(self):
        # Re-grant on the employers setting (a pool from the application),
        # then narrow again.
        self.narrow(Visibility.EMPLOYERS)
        applied_pool = pool_service.create_pool(
            company=self.applied_co, actor=self.applied_member.user, name="Later"
        )
        pool_service.add_member(
            pool=applied_pool, candidate=self.candidate, actor=self.applied_member.user
        )
        self.assertTrue(self.live(GrantBasis.POOL))

        self.narrow(Visibility.NOBODY)

        self.assertFalse(self.live(GrantBasis.POOL))
        self.assertTrue(self.live(GrantBasis.APPLICATION))

    def test_widening_revokes_nothing(self):
        self.narrow(Visibility.EMPLOYERS)
        before = AuditLog.objects.filter(verb="candidate.visibility_revoked").count()
        self.narrow(Visibility.EVERYONE)
        self.assertTrue(self.live(GrantBasis.APPLICATION))
        self.assertEqual(
            AuditLog.objects.filter(verb="candidate.visibility_revoked").count(), before
        )

    def test_blocking_still_revokes_the_application_grant_too(self):
        self.narrow(Visibility.EMPLOYERS)
        blocking.block_company(
            profile=self.candidate, actor=self.candidate.user, company=self.applied_co
        )
        self.assertFalse(self.live(GrantBasis.APPLICATION))
        with self.assertRaises(NotAllowed):
            visibility_service.grant(
                candidate=self.candidate, company=self.applied_co, basis=GrantBasis.POOL
            )
