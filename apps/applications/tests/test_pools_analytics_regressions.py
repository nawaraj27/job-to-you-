"""
Talent pools, employer analytics and raw ids — the first loop over these
screens (Loop Closure POOL-01..04, ANA-01, IDS-01). Reproduced on the
development database before fixing.

  * a non-numeric `?pool=`, `pool_id` or `note_id` was a 500;
  * a Viewer or Interviewer could create pools and remove people from them, and
    read the pool page that lists candidates by name (SRS §2.3: Candidates None);
  * an Interviewer could read company analytics (SRS §2.3: Jobs/Applications None);
  * a candidate who blocked the company stayed listed in its pools, and saving
    them to a pool again wrote a fresh live `pool` grant.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.applications.models import CandidateVisibilityGrant, TalentPool
from apps.applications.services import pools as pool_service
from apps.applications.services import visibility
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.services import blocking
from apps.candidates.tests.factories import create_profile
from apps.common.errors import NotAllowed
from apps.common.ids import as_pk
from apps.companies.permissions import Role
from apps.companies.tests.factories import add_member, company_with_owner
from apps.jobs.tests.factories import create_job, publish


class PoolFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company))
        self.candidate = create_profile()
        self.application = submit(job=self.job, profile=self.candidate)
        self.pool = pool_service.create_pool(company=self.company, actor=self.owner, name="Backend")
        pool_service.add_member(pool=self.pool, candidate=self.candidate, actor=self.owner)
        self.pools_url = reverse("employer:pools", args=["acme-co"])
        self.act_url = reverse(
            "employer:application_act", args=["acme-co", self.application.public_id]
        )


class MalformedIdTests(PoolFixture):
    def test_as_pk(self):
        for value, expected in (
            ("12", 12),
            (" 7 ", 7),
            ("abc", None),
            ("", None),
            (None, None),
            ("-1", None),
            ("0", None),
            ("1e3", None),
            (str(2**64), None),
        ):
            with self.subTest(value=value):
                self.assertEqual(as_pk(value), expected)

    def test_non_numeric_ids_are_not_server_errors(self):
        self.client.force_login(self.owner)
        cases = (
            ("get", self.pools_url, {"pool": "abc"}),
            ("post", self.pools_url, {"action": "delete", "pool_id": "abc"}),
            ("post", self.act_url, {"action": "pool", "pool_id": "abc"}),
            ("post", self.act_url, {"action": "delete_note", "note_id": "abc"}),
        )
        for method, url, data in cases:
            with self.subTest(url=url, data=data):
                response = getattr(self.client, method)(url, data)
                self.assertLess(response.status_code, 500)
        self.assertTrue(TalentPool.objects.filter(pk=self.pool.pk).exists())


class PoolPermissionTests(PoolFixture):
    def test_viewer_and_interviewer_cannot_read_or_change_pools(self):
        for role in (Role.VIEWER, Role.INTERVIEWER):
            user = create_user(email=f"{role}@example.test")
            add_member(self.company, user, role)
            self.client.force_login(user)
            with self.subTest(role=role):
                self.assertEqual(self.client.get(self.pools_url).status_code, 403)
                self.client.post(self.pools_url, {"action": "create", "name": f"By {role}"})
                self.client.post(
                    self.pools_url,
                    {
                        "action": "remove",
                        "pool_id": self.pool.pk,
                        "candidate": self.candidate.public_id,
                    },
                )
                self.client.post(self.pools_url, {"action": "delete", "pool_id": self.pool.pk})

        self.assertEqual(TalentPool.objects.filter(company=self.company).count(), 1)
        self.assertTrue(
            self.pool.members.filter(candidate=self.candidate, deleted_at__isnull=True).exists()
        )

    def test_a_recruiter_still_manages_pools(self):
        recruiter = create_user()
        add_member(self.company, recruiter, Role.RECRUITER)
        self.client.force_login(recruiter)

        self.assertEqual(self.client.get(self.pools_url).status_code, 200)
        self.client.post(self.pools_url, {"action": "create", "name": "Frontend"})
        self.assertTrue(TalentPool.objects.filter(company=self.company, name="Frontend").exists())

    def test_the_rail_hides_pools_search_and_analytics_from_an_interviewer(self):
        interviewer = create_user()
        add_member(self.company, interviewer, Role.INTERVIEWER)
        self.client.force_login(interviewer)
        page = self.client.get(reverse("employer:overview", args=["acme-co"]))

        for name in ("employer:pools", "employer:talent", "employer:analytics"):
            with self.subTest(route=name):
                self.assertNotContains(page, f'href="{reverse(name, args=["acme-co"])}"')


class AnalyticsPermissionTests(PoolFixture):
    def test_interviewer_is_refused_and_viewer_reads(self):
        url = reverse("employer:analytics", args=["acme-co"])
        interviewer = create_user()
        add_member(self.company, interviewer, Role.INTERVIEWER)
        viewer = create_user()
        add_member(self.company, viewer, Role.VIEWER)

        self.client.force_login(interviewer)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(url).status_code, 200)


class BlockedCandidatePoolTests(PoolFixture):
    def test_blocking_takes_the_candidate_out_of_the_company_s_pools(self):
        blocking.block_company(
            profile=self.candidate, actor=self.candidate.user, company=self.company
        )

        self.client.force_login(self.owner)
        page = self.client.get(self.pools_url, {"pool": self.pool.pk})
        self.assertEqual(list(page.context["members"]), [])

    def test_no_grant_can_be_written_after_a_block(self):
        blocking.block_company(
            profile=self.candidate, actor=self.candidate.user, company=self.company
        )

        self.client.force_login(self.owner)
        response = self.client.post(
            self.act_url, {"action": "pool", "pool_id": self.pool.pk}, follow=True
        )

        self.assertContains(response, "blocked your company")
        self.assertFalse(
            CandidateVisibilityGrant.objects.live()
            .filter(candidate=self.candidate, company=self.company)
            .exists()
        )
        with self.assertRaises(NotAllowed):
            visibility.grant(candidate=self.candidate, company=self.company, basis="pool")
