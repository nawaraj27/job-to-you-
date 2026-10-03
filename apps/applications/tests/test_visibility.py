"""
Visibility grants (INV-4), review tools (FR-6.6, FR-6.11) and pools (FR-6.9).

INV-4 is the invariant this file exists to defend: an employer may read a
candidate only where a live grant says why. Every negative case here is a
company being refused a person they have no basis to see.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.applications.models import (
    ApplicationNote,
    ApplicationRating,
    CandidateVisibilityGrant,
    CompanyCandidateNote,
    GrantBasis,
    Tag,
    TalentPoolMember,
)
from apps.applications.services import pools as pool_service
from apps.applications.services import review as review_service
from apps.applications.services import visibility
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.common.errors import Conflict, InvalidState, NotAllowed, NotFound
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish


class GrantTests(TestCase):
    def setUp(self):
        self.company, _ = company_with_owner(create_user())
        self.profile = create_profile()

    def test_a_company_with_no_grant_cannot_see_a_candidate(self):
        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))

    def test_the_refusal_is_not_found_rather_than_forbidden(self):
        # A 403 would confirm that a person with this id exists.
        with self.assertRaises(NotFound):
            visibility.require_access(candidate=self.profile, company=self.company)

    def test_granting_twice_on_one_basis_makes_one_row(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        self.assertEqual(CandidateVisibilityGrant.objects.count(), 1)

    def test_an_invented_basis_is_refused(self):
        with self.assertRaises(InvalidState):
            visibility.grant(candidate=self.profile, company=self.company, basis="vibes")

    def test_an_expired_grant_is_not_live(self):
        CandidateVisibilityGrant.objects.create(
            candidate=self.profile,
            company=self.company,
            basis=GrantBasis.INVITED,
            expires_at=timezone.now() - dt.timedelta(days=1),
        )
        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))

    def test_a_revoked_grant_is_not_live(self):
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        visibility.revoke_all(candidate=self.profile, company=self.company)
        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))

    def test_revoking_touches_only_that_company(self):
        other, _ = company_with_owner(create_user(email="other@example.test"))
        visibility.grant(candidate=self.profile, company=self.company, basis=GrantBasis.POOL)
        visibility.grant(candidate=self.profile, company=other, basis=GrantBasis.POOL)

        visibility.revoke_all(candidate=self.profile, company=self.company)

        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))
        self.assertTrue(visibility.has_access(candidate=self.profile, company=other))


class ReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())

    def test_a_note_is_internal_and_has_no_way_to_become_otherwise(self):
        note = review_service.add_note(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            body="Strong on SQL, weak on infra.",
        )
        # FR-6.11: there is no candidate-visible flag on a note, by design.
        self.assertFalse(hasattr(note, "visible_to_candidate"))
        # And it does not enter the shared timeline.
        self.assertFalse(self.application.timeline.filter(type__icontains="note").exists())

    def test_an_empty_note_is_refused(self):
        with self.assertRaises(InvalidState):
            review_service.add_note(
                application=self.application,
                actor=self.owner,
                membership=self.membership,
                body="   ",
            )

    def test_only_the_author_may_delete_a_note(self):
        note = review_service.add_note(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            body="Mine",
        )
        stranger = create_user(email="stranger@example.test")

        with self.assertRaises(NotAllowed):
            review_service.delete_note(note=note, actor=stranger)

    def test_deleting_a_note_is_soft(self):
        note = review_service.add_note(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            body="Mine",
        )
        review_service.delete_note(note=note, actor=self.owner)

        note.refresh_from_db()
        self.assertIsNotNone(note.deleted_at)
        self.assertTrue(ApplicationNote.objects.filter(pk=note.pk).exists())

    def test_a_mention_of_somebody_outside_the_company_is_dropped(self):
        other, other_membership = company_with_owner(create_user(email="other@example.test"))
        note = review_service.add_note(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            body="Ask them",
            mentions=[other_membership.pk],
        )
        self.assertEqual(note.mentions, [])

    def test_a_tag_is_reused_rather_than_duplicated(self):
        review_service.add_tag(
            application=self.application, actor=self.owner, company=self.company, name="Strong"
        )
        second = submit(job=self.job, profile=create_profile())
        review_service.add_tag(
            application=second, actor=self.owner, company=self.company, name="strong"
        )
        self.assertEqual(Tag.objects.filter(company=self.company).count(), 1)

    def test_tagging_the_same_application_twice_is_idempotent(self):
        for _ in range(2):
            review_service.add_tag(
                application=self.application,
                actor=self.owner,
                company=self.company,
                name="Strong",
            )
        self.assertEqual(self.application.tag_links.count(), 1)

    def test_removing_a_tag_that_is_not_there_is_not_found(self):
        with self.assertRaises(NotFound):
            review_service.remove_tag(application=self.application, actor=self.owner, tag_id=999999)

    def test_a_rating_outside_one_to_five_is_refused(self):
        for score in (0, 6, "many"):
            with self.subTest(score=score), self.assertRaises(InvalidState):
                review_service.rate(application=self.application, actor=self.owner, score=score)

    def test_a_second_rating_by_the_same_person_replaces_the_first(self):
        review_service.rate(application=self.application, actor=self.owner, score=3)
        review_service.rate(application=self.application, actor=self.owner, score=5)

        self.assertEqual(ApplicationRating.objects.count(), 1)
        self.assertEqual(ApplicationRating.objects.get().score, 5)

    def test_a_note_on_a_candidate_needs_a_reason_to_see_them(self):
        stranger = create_profile()
        with self.assertRaises(NotFound):
            review_service.add_candidate_note(
                company=self.company, candidate=stranger, actor=self.owner, body="Interesting"
            )

    def test_a_note_on_a_visible_candidate_is_allowed(self):
        note = review_service.add_candidate_note(
            company=self.company,
            candidate=self.application.candidate,
            actor=self.owner,
            body="Worth a call later",
        )
        self.assertEqual(CompanyCandidateNote.objects.get(), note)


class PoolTests(TestCase):
    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner)
        self.profile = create_profile()
        self.pool = pool_service.create_pool(
            company=self.company, actor=self.owner, name="Backend 2026"
        )

    def test_two_pools_cannot_share_a_name(self):
        with self.assertRaises(Conflict):
            pool_service.create_pool(company=self.company, actor=self.owner, name="backend 2026")

    def test_a_pool_needs_a_name(self):
        with self.assertRaises(InvalidState):
            pool_service.create_pool(company=self.company, actor=self.owner, name="  ")

    def test_adding_somebody_grants_visibility(self):
        pool_service.add_member(pool=self.pool, candidate=self.profile, actor=self.owner)
        self.assertTrue(visibility.has_access(candidate=self.profile, company=self.company))

    def test_removing_them_revokes_it(self):
        pool_service.add_member(pool=self.pool, candidate=self.profile, actor=self.owner)
        pool_service.remove_member(pool=self.pool, candidate=self.profile, actor=self.owner)

        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))

    def test_removal_is_soft(self):
        pool_service.add_member(pool=self.pool, candidate=self.profile, actor=self.owner)
        pool_service.remove_member(pool=self.pool, candidate=self.profile, actor=self.owner)

        member = TalentPoolMember.objects.get()
        self.assertIsNotNone(member.deleted_at)

    def test_a_second_pool_keeps_the_grant_alive(self):
        second = pool_service.create_pool(
            company=self.company, actor=self.owner, name="Frontend 2026"
        )
        pool_service.add_member(pool=self.pool, candidate=self.profile, actor=self.owner)
        pool_service.add_member(pool=second, candidate=self.profile, actor=self.owner)

        pool_service.remove_member(pool=self.pool, candidate=self.profile, actor=self.owner)

        # They are still in the other pool, so the reason to see them stands.
        self.assertTrue(visibility.has_access(candidate=self.profile, company=self.company))

    def test_removal_does_not_revoke_a_grant_from_a_different_basis(self):
        seed_templates()
        job = publish(create_job(self.company))
        application = submit(job=job, profile=self.profile)

        pool_service.add_member(pool=self.pool, candidate=self.profile, actor=self.owner)
        pool_service.remove_member(pool=self.pool, candidate=self.profile, actor=self.owner)

        # They applied. That reason is untouched.
        self.assertTrue(visibility.has_access(candidate=self.profile, company=self.company))
        self.assertTrue(application.grants.exists())

    def test_deleting_a_pool_revokes_what_it_granted(self):
        pool_service.add_member(pool=self.pool, candidate=self.profile, actor=self.owner)
        pool_service.delete_pool(pool=self.pool, actor=self.owner)

        self.assertFalse(visibility.has_access(candidate=self.profile, company=self.company))

    def test_re_adding_somebody_previously_removed_works(self):
        pool_service.add_member(pool=self.pool, candidate=self.profile, actor=self.owner)
        pool_service.remove_member(pool=self.pool, candidate=self.profile, actor=self.owner)
        pool_service.add_member(pool=self.pool, candidate=self.profile, actor=self.owner)

        self.assertTrue(visibility.has_access(candidate=self.profile, company=self.company))
        self.assertEqual(TalentPoolMember.objects.filter(deleted_at__isnull=True).count(), 1)

    def test_removing_somebody_who_is_not_in_the_pool_is_not_found(self):
        with self.assertRaises(NotFound):
            pool_service.remove_member(pool=self.pool, candidate=self.profile, actor=self.owner)
