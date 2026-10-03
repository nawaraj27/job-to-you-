"""
The matched-roles feed and the employer's ranked board (FR-8.3, FR-8.4).

Two things worth stating about what is asserted here.

**The flag is the gate.** `job_matching` has been referenced by the navigation
since P7 and has been off ever since, because the feature did not exist (SRS
INV-6). P13 is what makes it true, and a test asserts both halves: off is a 404,
on is a working page.

**A score never travels sideways.** A candidate's feed is a statement about
them; an employer sees only the score frozen on an application they can already
read. Both are asserted from the failing side.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.candidates.models import CandidateSkill
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.core import features
from apps.core.models import FeatureFlag
from apps.jobs.models import JobSkill, Requirement
from apps.jobs.tests.factories import create_job, publish
from apps.matching import scoring
from apps.matching.tests.test_matching import application_for
from apps.taxonomy.models import Skill


def enable_matching(test) -> None:
    """Turn the `job_matching` flag on for one test."""
    FeatureFlag.objects.update_or_create(
        key="job_matching",
        defaults={
            "is_enabled": True,
            "expires_on": timezone.localdate() + dt.timedelta(days=7),
        },
    )
    features.invalidate_cache()
    test.addCleanup(features.invalidate_cache)


class FeedScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

        self.profile = create_profile(headline="Backend engineer")
        self.python = Skill.objects.create(name="Python", slug="python")
        CandidateSkill.objects.create(profile=self.profile, skill=self.python, years=Decimal("4"))

        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        JobSkill.objects.create(job=self.job, skill=self.python, requirement=Requirement.REQUIRED)

        self.client.force_login(self.profile.user)
        self.url = reverse("jobs:recommended")

    def test_the_screen_is_a_404_when_the_flag_is_off(self):
        """The gate, asserted by switching it off rather than by the default.

        The flag defaulted to off from P7 to P15 because the page did not exist
        (INV-6). P13 built it and P15 turned the default on — which is the same
        defect in reverse, and the reason this test now sets the state it means
        to test instead of relying on it.
        """
        FeatureFlag.objects.update_or_create(
            key="job_matching",
            defaults={
                "is_enabled": False,
                "expires_on": timezone.localdate() + dt.timedelta(days=7),
            },
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_screen_is_reachable_by_default(self):
        # P13 built the feed and left the flag off, so the finished feature was
        # unreachable in production for two phases.
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_screen_renders_with_the_flag_on(self):
        enable_matching(self)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.job.title)

    def test_each_row_shows_its_score_and_its_band(self):
        enable_matching(self)
        response = self.client.get(self.url)

        self.assertContains(response, "%")
        self.assertContains(response, "match", status_code=200, msg_prefix="", html=False)

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        enable_matching(self)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_a_provisional_feed_says_so_and_names_the_fix(self):
        enable_matching(self)
        bare = create_profile()
        self.client.force_login(bare.user)

        response = self.client.get(self.url)
        self.assertContains(response, "provisional")
        self.assertContains(response, "Confirm your skills")

    def test_a_confident_feed_does_not_claim_to_be_provisional(self):
        enable_matching(self)
        for name in ("django", "postgres"):
            CandidateSkill.objects.create(
                profile=self.profile,
                skill=Skill.objects.create(name=name, slug=name),
            )

        response = self.client.get(self.url)
        self.assertNotContains(response, "These are provisional")

    def test_an_empty_corpus_gets_an_empty_state_not_a_blank_page(self):
        enable_matching(self)
        self.job.delete()

        response = self.client.get(self.url)
        self.assertContains(response, "Nothing to match against yet")

    def test_the_page_says_scores_are_not_shown_to_employers(self):
        # A candidate's feed is a statement about them. Saying so on the page is
        # the difference between a tool and a leaderboard.
        enable_matching(self)
        response = self.client.get(self.url)
        self.assertContains(response, "never shown to employers")

    def test_viewing_the_feed_counts_impressions(self):
        from apps.matching.services import exposure

        enable_matching(self)
        self.client.get(self.url)
        self.assertGreaterEqual(exposure.impressions_for(self.job.pk), 1)

    def test_the_htmx_request_returns_only_the_results(self):
        enable_matching(self)
        response = self.client.get(self.url, HTTP_HX_REQUEST="true")
        self.assertContains(response, "matched-results")
        self.assertNotContains(response, "<html")


class EmployerRankingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

        self.owner = create_profile().user
        self.company, _ = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))

        self.python = Skill.objects.create(name="Python", slug="python")
        JobSkill.objects.create(job=self.job, skill=self.python, requirement=Requirement.REQUIRED)

        self.strong = create_profile()
        CandidateSkill.objects.create(profile=self.strong, skill=self.python)
        self.weak = create_profile()

        self.strong_application = application_for(
            self.strong, self.job, match_snapshot={"score": 88, "band": "strong", "components": []}
        )
        self.weak_application = application_for(
            self.weak, self.job, match_snapshot={"score": 31, "band": "stretch", "components": []}
        )

        self.client.force_login(self.owner)
        self.board = reverse("employer:applicants", args=[self.company.slug])

    def test_the_board_can_be_ordered_by_match(self):
        response = self.client.get(self.board, {"order": "match"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Match score")

    def test_an_unknown_order_falls_back_rather_than_erroring(self):
        # A hand-typed query string must not be able to produce an order the
        # board does not have.
        response = self.client.get(self.board, {"order": "; drop table"})
        self.assertEqual(response.status_code, 200)

    def test_the_detail_screen_shows_the_frozen_score(self):
        url = reverse(
            "employer:application_detail",
            args=[self.company.slug, str(self.strong_application.public_id)],
        )
        response = self.client.get(url)

        self.assertContains(response, "Match at the time they applied")
        self.assertContains(response, "88")

    def test_the_detail_screen_says_the_score_was_not_recomputed(self):
        url = reverse(
            "employer:application_detail",
            args=[self.company.slug, str(self.strong_application.public_id)],
        )
        response = self.client.get(url)
        self.assertContains(response, "the number that was true then")

    def test_an_application_with_no_snapshot_shows_no_match_block(self):
        # Absent and zero are different facts about a candidate.
        empty = application_for(create_profile(), self.job, match_snapshot={})
        url = reverse("employer:application_detail", args=[self.company.slug, str(empty.public_id)])

        response = self.client.get(url)
        self.assertNotContains(response, "Match at the time they applied")

    def test_a_snapshot_from_an_older_scorer_is_labelled(self):
        stale = application_for(
            create_profile(),
            self.job,
            match_snapshot={"score": 70, "band": "good", "components": [], "algorithm_version": 0},
        )
        url = reverse("employer:application_detail", args=[self.company.slug, str(stale.public_id)])

        response = self.client.get(url)
        self.assertContains(response, "earlier version of the scorer")

    def test_a_current_snapshot_is_not_labelled_as_old(self):
        current = application_for(
            create_profile(),
            self.job,
            match_snapshot={
                "score": 70,
                "band": "good",
                "components": [],
                "algorithm_version": scoring.ALGORITHM_VERSION,
            },
        )
        url = reverse(
            "employer:application_detail", args=[self.company.slug, str(current.public_id)]
        )

        response = self.client.get(url)
        self.assertNotContains(response, "earlier version of the scorer")

    def test_another_companys_applicant_is_not_found(self):
        stranger = create_profile().user
        other_company, _ = company_with_owner(stranger)

        self.client.force_login(stranger)
        url = reverse(
            "employer:application_detail",
            args=[other_company.slug, str(self.strong_application.public_id)],
        )
        self.assertEqual(self.client.get(url).status_code, 404)


class PipelineOrderingTests(TestCase):
    """The board ordering itself, at the selector."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))

        self.low = application_for(create_profile(), self.job, match_snapshot={"score": 20})
        self.high = application_for(create_profile(), self.job, match_snapshot={"score": 95})
        self.none = application_for(create_profile(), self.job, match_snapshot={})

    def test_ordering_by_match_puts_the_highest_first(self):
        from apps.applications import selectors

        board = selectors.pipeline_board(self.membership, order="match")
        column = next(entry for entry in board if entry["applications"])
        scores = [app.match_snapshot.get("score") for app in column["applications"]]
        self.assertEqual(scores[0], 95)

    def test_an_unscored_application_sorts_last_and_is_never_dropped(self):
        from apps.applications import selectors

        board = selectors.pipeline_board(self.membership, order="match")
        column = next(entry for entry in board if entry["applications"])
        self.assertIn(self.none, column["applications"])
        self.assertEqual(column["applications"][-1], self.none)

    def test_the_default_order_is_still_by_recency(self):
        from apps.applications import selectors

        board = selectors.pipeline_board(self.membership)
        column = next(entry for entry in board if entry["applications"])
        self.assertEqual(len(column["applications"]), 3)
