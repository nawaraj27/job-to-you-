"""
Following a company, and hearing when it posts (FOL-01).

The publication goes through `jobs.services.lifecycle.publish` and the notice
through the real outbox and fan-out, so what is asserted is what a follower
would actually receive — not that a helper returns a list.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.candidates.models import BlockedCompany
from apps.candidates.services import profile as profile_service
from apps.common.errors import InvalidState, NotFound
from apps.companies.models import CompanyFollow, CompanyState
from apps.companies.services import follows
from apps.companies.tests.factories import company_with_owner
from apps.jobs.services import lifecycle
from apps.jobs.tests.factories import create_job
from apps.notifications.models import Notification, NotificationPreference
from apps.notifications.services import fanout, outbox


def deliver() -> None:
    for _ in range(10):
        if not outbox.drain(500):
            break
    for _ in range(10):
        if not fanout.run_due(limit=500).get("chunks"):
            break


class FollowFixture(TestCase):
    def setUp(self):
        self.owner = create_user(full_name="Owner Person")
        self.company, self.membership = company_with_owner(self.owner, name="Kora Studio")
        self.other_company, self.other_membership = company_with_owner(
            create_user(), name="Everest Logistics"
        )
        self.follower = create_user(full_name="Anjali Thapa")
        profile_service.get_or_create_profile(user=self.follower)
        self.bystander = create_user(full_name="Bina Bystander")
        profile_service.get_or_create_profile(user=self.bystander)

    def publish_new_job(self, company=None, membership=None, title="Backend Engineer"):
        company = company or self.company
        membership = membership or self.membership
        job = create_job(company, title=title)
        lifecycle.publish(job=job, actor=membership.user, membership=membership)
        deliver()
        return job

    def notices(self, user):
        return Notification.objects.filter(recipient=user, kind="job.published")


class RelationshipTests(FollowFixture):
    def test_follow_is_stored_and_reported(self):
        follows.follow(user=self.follower, company=self.company)

        self.assertTrue(follows.is_following(user=self.follower, company=self.company))
        self.assertFalse(follows.is_following(user=self.follower, company=self.other_company))

    def test_following_twice_is_one_follow(self):
        follows.follow(user=self.follower, company=self.company)
        follows.follow(user=self.follower, company=self.company)

        self.assertEqual(CompanyFollow.objects.filter(user=self.follower).count(), 1)

    def test_unfollow_is_safe_and_idempotent(self):
        follows.follow(user=self.follower, company=self.company)

        self.assertTrue(follows.unfollow(user=self.follower, company=self.company))
        self.assertFalse(follows.unfollow(user=self.follower, company=self.company))
        self.assertFalse(follows.is_following(user=self.follower, company=self.company))

    def test_a_suspended_company_cannot_be_followed(self):
        self.company.state = CompanyState.SUSPENDED
        self.company.save(update_fields=["state"])

        with self.assertRaises(NotFound):
            follows.follow(user=self.follower, company=self.company)

    def test_a_blocked_company_cannot_be_followed(self):
        BlockedCompany.objects.create(profile=self.follower.candidate_profile, company=self.company)

        with self.assertRaises(InvalidState):
            follows.follow(user=self.follower, company=self.company)


class NotificationTests(FollowFixture):
    def test_a_follower_hears_about_a_new_job(self):
        follows.follow(user=self.follower, company=self.company)

        job = self.publish_new_job(title="Senior Django Engineer")

        notice = self.notices(self.follower).get()
        self.assertIn("Kora Studio", notice.title)
        self.assertIn("Senior Django Engineer", notice.body)
        self.assertEqual(notice.target_url, reverse("jobs:detail", args=[job.slug]))

    def test_a_non_follower_hears_nothing(self):
        follows.follow(user=self.follower, company=self.company)

        self.publish_new_job()

        self.assertFalse(self.notices(self.bystander).exists())

    def test_following_one_company_says_nothing_about_another(self):
        follows.follow(user=self.follower, company=self.company)

        self.publish_new_job(company=self.other_company, membership=self.other_membership)

        self.assertFalse(self.notices(self.follower).exists())

    def test_unfollowing_stops_future_notices_and_keeps_old_ones(self):
        follows.follow(user=self.follower, company=self.company)
        self.publish_new_job(title="Job One")
        follows.unfollow(user=self.follower, company=self.company)

        self.publish_new_job(title="Job Two")

        bodies = list(self.notices(self.follower).values_list("body", flat=True))
        self.assertEqual(bodies, ["Job One"])

    def test_a_block_made_after_the_follow_silences_it(self):
        follows.follow(user=self.follower, company=self.company)
        BlockedCompany.objects.create(profile=self.follower.candidate_profile, company=self.company)

        self.publish_new_job()

        self.assertFalse(self.notices(self.follower).exists())

    def test_the_companies_you_follow_preference_is_respected(self):
        follows.follow(user=self.follower, company=self.company)
        NotificationPreference.objects.update_or_create(
            user=self.follower,
            event_key=NotificationPreference.Event.COMPANY_UPDATES,
            defaults={"in_app": False, "email": False},
        )

        self.publish_new_job()

        self.assertFalse(self.notices(self.follower).exists())

    def test_republishing_the_same_job_does_not_announce_it_twice(self):
        follows.follow(user=self.follower, company=self.company)
        job = self.publish_new_job()
        lifecycle.unpublish(job=job, actor=self.owner, membership=self.membership)
        job.refresh_from_db()
        lifecycle.publish(job=job, actor=self.owner, membership=self.membership)
        deliver()

        self.assertEqual(self.notices(self.follower).count(), 1)

    def test_following_after_a_job_was_published_says_nothing_about_it(self):
        """Found by the demo seed: the outbox drains later than it is written.

        The job is published while the worker is behind; the candidate then
        follows; then the backlog drains. They followed after the posting, so
        it is not news to them.
        """
        job = create_job(self.company, title="Published before the follow")
        lifecycle.publish(job=job, actor=self.owner, membership=self.membership)
        follows.follow(user=self.follower, company=self.company)

        deliver()

        self.assertFalse(self.notices(self.follower).exists())

    def test_a_draft_announces_nothing(self):
        follows.follow(user=self.follower, company=self.company)

        create_job(self.company, title="Still a draft")
        deliver()

        self.assertFalse(self.notices(self.follower).exists())


class ScreenTests(FollowFixture):
    def setUp(self):
        super().setUp()
        self.company_url = reverse("jobs:company", args=[self.company.slug])
        self.follow_url = reverse("jobs:company_follow", args=[self.company.slug])

    def test_the_company_page_offers_follow_and_shows_the_state(self):
        self.client.force_login(self.follower)

        self.assertContains(self.client.get(self.company_url), "Follow company")

        self.client.post(self.follow_url, {"action": "follow"})

        page = self.client.get(self.company_url)
        self.assertContains(page, "Following")
        self.assertContains(page, 'aria-pressed="true"')

    def test_unfollow_through_the_screen(self):
        self.client.force_login(self.follower)
        self.client.post(self.follow_url, {"action": "follow"})

        response = self.client.post(self.follow_url, {"action": "unfollow"}, HTTP_HX_REQUEST="true")

        self.assertContains(response, "Follow company")
        self.assertFalse(follows.is_following(user=self.follower, company=self.company))

    def test_signed_out_visitors_are_asked_to_sign_in_and_cannot_post(self):
        self.assertContains(self.client.get(self.company_url), "Sign in to follow")

        response = self.client.post(self.follow_url, {"action": "follow"})

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])
        self.assertFalse(CompanyFollow.objects.exists())

    def test_a_blocked_company_refusal_reads_as_a_restriction(self):
        BlockedCompany.objects.create(profile=self.follower.candidate_profile, company=self.company)
        self.client.force_login(self.follower)

        response = self.client.post(self.follow_url, {"action": "follow"}, HTTP_HX_REQUEST="true")

        self.assertEqual(response["X-Restriction"], "state")
        self.assertContains(response, "Unblock it", status_code=409)
        self.assertFalse(CompanyFollow.objects.exists())


class FollowBoundaryTests(FollowFixture):
    """The exact event boundary asked for in verification (2026-09-21)."""

    def all_notices(self, user):
        return Notification.objects.filter(recipient=user)

    def test_a_follows_b_does_not_then_a_unfollows(self):
        follows.follow(user=self.follower, company=self.company)

        self.publish_new_job(title="Job 1")

        self.assertEqual(self.notices(self.follower).count(), 1)
        self.assertEqual(self.all_notices(self.bystander).count(), 0)

        follows.unfollow(user=self.follower, company=self.company)
        self.publish_new_job(title="Job 2")

        self.assertEqual(
            list(self.notices(self.follower).values_list("body", flat=True)), ["Job 1"]
        )
        self.assertEqual(self.all_notices(self.bystander).count(), 0)

    def test_follow_unfollow_follow_is_one_row(self):
        follows.follow(user=self.follower, company=self.company)
        follows.unfollow(user=self.follower, company=self.company)
        follows.follow(user=self.follower, company=self.company)
        follows.follow(user=self.follower, company=self.company)

        self.assertEqual(
            CompanyFollow.objects.filter(user=self.follower, company=self.company).count(), 1
        )

        self.publish_new_job()
        self.assertEqual(self.notices(self.follower).count(), 1)

    def test_a_follow_created_after_publication_time_t_is_not_told(self):
        """Event time, not seed order: the follow row is dated after T."""
        import datetime as dt

        from apps.notifications.models import OutboxEvent

        job = create_job(self.company, title="Published at T")
        lifecycle.publish(job=job, actor=self.owner, membership=self.membership)
        published = OutboxEvent.objects.get(
            type="job.published", payload__job_public_id=str(job.public_id)
        )
        follow = follows.follow(user=self.follower, company=self.company)
        CompanyFollow.objects.filter(pk=follow.pk).update(
            created_at=published.created_at + dt.timedelta(seconds=1)
        )
        early = follows.follow(user=self.bystander, company=self.company)
        CompanyFollow.objects.filter(pk=early.pk).update(
            created_at=published.created_at - dt.timedelta(seconds=1)
        )

        deliver()

        self.assertFalse(self.notices(self.follower).exists())
        self.assertEqual(self.notices(self.bystander).count(), 1)

    def test_the_notice_names_nothing_about_the_follower_list(self):
        follows.follow(user=self.follower, company=self.company)
        follows.follow(user=self.bystander, company=self.company)

        self.publish_new_job(title="Job 1")

        for user in (self.follower, self.bystander):
            notice = self.notices(user).get()
            text = f"{notice.title} {notice.body} {notice.payload}"
            other = self.bystander if user == self.follower else self.follower
            self.assertNotIn(other.email, text)
            self.assertNotIn(other.full_name, text)
