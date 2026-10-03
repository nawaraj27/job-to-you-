"""
Extraction, retrieval, the cache and the exposure controls (FR-8.3 to FR-8.8).

What this file is answerable for, one class each:

  * feature extraction reads only what the allow-list permits, and only the
    right things (FR-8.9);
  * retrieval never returns a blocked or already-applied posting, and never
    returns an empty page while a filter could be relaxed (ADR-A24);
  * the cache invalidates by key construction — a new posting appears without
    anybody sweeping anything (ADR-A21, SRS §10.1);
  * one company cannot fill a feed and one posting cannot be shown to everybody
    (FR-8.8);
  * nothing is persisted except the application snapshot (ADR-A21).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.applications.models import Application
from apps.applications.services import pipeline as pipeline_service
from apps.candidates.models import BlockedCompany, CandidateSkill, ProfileExperience
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import create_company
from apps.jobs.models import Job, JobSkill, Requirement, Workplace
from apps.jobs.tests.factories import create_job, publish
from apps.matching import extract, retrieval, scoring
from apps.matching.features import ALLOWED_FEATURES
from apps.matching.services import exposure, recommendations, snapshots
from apps.resumes.models import ResumeDocument, ResumeTemplate, ResumeVersion, VersionReason
from apps.taxonomy.models import RelationKind, Skill, SkillRelation


def skill(name: str) -> Skill:
    return Skill.objects.create(name=name, slug=name.lower().replace(" ", "-"))


def application_for(profile, job, **overrides) -> Application:
    """An application row, without going through the apply service.

    A factory, not a service bypass: nothing here claims to be a domain
    operation, and the tests that care about applying live in `applications`.
    What matching needs is a row that exists.
    """
    document = ResumeDocument.objects.create(
        profile=profile, name="CV", template=ResumeTemplate.objects.first()
    )
    version = ResumeVersion.objects.create(
        document=document,
        version_no=1,
        content={"schema_version": 1, "contact": {}, "sections": []},
        sha256="a" * 64,
        created_reason=VersionReason.APPLIED,
        finalized_at=timezone.now(),
    )
    fields = {
        "job": job,
        "candidate": profile,
        "resume_version": version,
        "stage": pipeline_service.default_stage_for(job),
        "applied_at": timezone.now(),
        "last_activity_at": timezone.now(),
    }
    fields.update(overrides)
    return Application.objects.create(**fields)


class MatchingFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

        self.profile = create_profile()
        self.python = skill("Python")
        self.django = skill("Django")
        self.kubernetes = skill("Kubernetes")

        CandidateSkill.objects.create(profile=self.profile, skill=self.python, years=Decimal("4"))
        CandidateSkill.objects.create(profile=self.profile, skill=self.django, years=Decimal("3"))

        self.company = create_company()
        self.job = publish(create_job(self.company))
        JobSkill.objects.create(job=self.job, skill=self.python, requirement=Requirement.REQUIRED)

    def publish_job(self, **overrides) -> Job:
        job = publish(create_job(overrides.pop("company", self.company), **overrides))
        return job


class ExtractionTests(MatchingFixture):
    def test_a_candidate_vector_only_carries_allowed_features(self):
        vector = extract.candidate_features(self.profile)
        self.assertEqual(set(vector) - set(ALLOWED_FEATURES), set())

    def test_a_job_vector_only_carries_allowed_features(self):
        vector = extract.job_features(self.job)
        self.assertEqual(set(vector) - set(ALLOWED_FEATURES), set())

    def test_no_vector_carries_anything_identifying(self):
        # The point of the allow-list, asserted on the real output rather than
        # on a fixture: a name or an email reaching a ranking is not a bug
        # anybody finds by reading a dashboard.
        blob = str(extract.candidate_features(self.profile)).lower()
        self.assertNotIn(self.profile.user.email.lower(), blob)
        self.assertNotIn("institution", blob)

    def test_the_candidate_vector_carries_their_skills(self):
        vector = extract.candidate_features(self.profile)
        self.assertEqual(vector["skill_ids"], sorted([self.python.pk, self.django.pk]))
        self.assertEqual(vector["skill_years"][self.python.pk], 4.0)

    def test_experience_is_totalled_from_dated_roles(self):
        ProfileExperience.objects.create(
            profile=self.profile,
            title="Engineer",
            org_name="Nexa",
            start_date=dt.date(2021, 1, 1),
            end_date=dt.date(2024, 1, 1),
        )
        vector = extract.candidate_features(self.profile)
        self.assertEqual(vector["experience_months"], 36)
        self.assertEqual(vector["seniority"], "junior")

    def test_overlapping_roles_are_counted_once(self):
        # Freelancing while employed is not two careers. Shared arithmetic with
        # the CV analyser (apps.common.periods), so the two cannot disagree
        # about the same person.
        for org in ("Nexa", "Freelance"):
            ProfileExperience.objects.create(
                profile=self.profile,
                title="Engineer",
                org_name=org,
                start_date=dt.date(2021, 1, 1),
                end_date=dt.date(2024, 1, 1),
            )
        self.assertEqual(extract.candidate_features(self.profile)["experience_months"], 36)

    def test_a_current_role_runs_to_today(self):
        ProfileExperience.objects.create(
            profile=self.profile,
            title="Engineer",
            org_name="Nexa",
            start_date=timezone.localdate() - dt.timedelta(days=365),
            is_current=True,
        )
        self.assertGreaterEqual(extract.candidate_features(self.profile)["experience_months"], 11)

    def test_the_job_vector_separates_required_from_preferred(self):
        JobSkill.objects.create(job=self.job, skill=self.django, requirement=Requirement.PREFERRED)
        vector = extract.job_features(self.job)
        self.assertEqual(vector["job_required_skill_ids"], [self.python.pk])
        self.assertIn(self.django.pk, vector["job_skill_ids"])

    def test_implied_skills_earn_partial_credit(self):
        # SRS §10.2's example, end to end through the real taxonomy graph.
        SkillRelation.objects.create(
            from_skill=self.django,
            to_skill=self.kubernetes,
            kind=RelationKind.IMPLIES,
            weight=Decimal("0.80"),
        )
        credit = extract.skill_credit_for(self.profile)

        self.assertEqual(credit.credit_for(self.django.pk), 1.0)
        self.assertAlmostEqual(credit.credit_for(self.kubernetes.pk), 0.8, places=2)

    def test_an_unrelated_skill_earns_nothing(self):
        credit = extract.skill_credit_for(self.profile)
        self.assertEqual(credit.credit_for(self.kubernetes.pk), 0.0)

    def test_seniority_bands_are_read_off_a_table(self):
        self.assertEqual(extract.seniority_for(0), "entry")
        self.assertEqual(extract.seniority_for(30), "junior")
        self.assertEqual(extract.seniority_for(70), "mid")
        self.assertEqual(extract.seniority_for(120), "senior")
        self.assertEqual(extract.seniority_for(200), "lead")

    def test_a_posting_level_maps_to_months(self):
        self.assertEqual(extract.required_months("junior"), 12)
        self.assertEqual(extract.required_months("senior"), 96)
        self.assertEqual(extract.required_months(""), 0)
        self.assertEqual(extract.required_months("nonsense"), 0)


class RetrievalTests(MatchingFixture):
    def test_a_published_job_is_retrieved(self):
        found = retrieval.retrieve(self.profile)
        self.assertIn(self.job, found.jobs)

    def test_a_draft_job_is_not(self):
        draft = create_job(self.company)
        found = retrieval.retrieve(self.profile)
        self.assertNotIn(draft, found.jobs)

    def test_a_blocked_companys_posting_is_never_shown(self):
        # Not a preference. Showing a candidate a job at a company they blocked
        # is a bug wearing a feature's clothes, and it is not relaxed to reach
        # the floor either.
        BlockedCompany.objects.create(profile=self.profile, company=self.company)
        found = retrieval.retrieve(self.profile)
        self.assertNotIn(self.job, found.jobs)

    def test_a_blocked_company_stays_excluded_even_when_results_are_thin(self):
        BlockedCompany.objects.create(profile=self.profile, company=self.company)
        found = retrieval.retrieve(self.profile)
        self.assertEqual(found.jobs, [])

    def test_an_already_applied_job_is_not_recommended_again(self):
        application_for(self.profile, self.job)
        self.assertNotIn(self.job, retrieval.retrieve(self.profile).jobs)

    def test_the_order_is_deterministic(self):
        # Without a tiebreaker after published_at, two jobs published in the
        # same second swap places between requests and a candidate sees one
        # posting twice while never seeing another (SRS §10.5).
        for _ in range(5):
            self.publish_job()

        first = [job.pk for job in retrieval.retrieve(self.profile).jobs]
        second = [job.pk for job in retrieval.retrieve(self.profile).jobs]
        self.assertEqual(first, second)

    def test_a_small_corpus_uses_the_exact_path(self):
        self.assertEqual(retrieval.retrieve(self.profile).strategy, "small")

    def test_a_salary_filter_excludes_a_posting_that_cannot_pay(self):
        self.profile.salary_min_base = Decimal("2000000")
        self.profile.save(update_fields=["salary_min_base", "updated_at"])
        low = self.publish_job(salary_max_base=Decimal("100000"))

        found = retrieval.retrieve(self.profile)
        self.assertNotIn(low, [job for job in found.jobs if not found.widened])

    def test_a_posting_with_no_published_range_survives_the_salary_filter(self):
        # Most postings in this market publish nothing, and filtering them out
        # would remove the majority of the corpus.
        self.profile.salary_min_base = Decimal("2000000")
        self.profile.save(update_fields=["salary_min_base", "updated_at"])
        silent = self.publish_job(salary_max_base=None)

        self.assertIn(silent, retrieval.retrieve(self.profile).jobs)

    def test_a_thin_result_is_widened_and_says_so(self):
        # An empty state reads as "there is nothing for you", which is untrue
        # and the moment a candidate leaves.
        self.profile.salary_min_base = Decimal("9000000")
        self.profile.save(update_fields=["salary_min_base", "updated_at"])
        for _ in range(3):
            self.publish_job(salary_max_base=Decimal("100000"))

        found = retrieval.retrieve(self.profile)
        self.assertTrue(found.widened)
        self.assertIn("salary", found.relaxed)
        self.assertTrue(found.jobs)

    def test_a_full_result_is_not_widened(self):
        for _ in range(12):
            self.publish_job()
        found = retrieval.retrieve(self.profile)
        self.assertFalse(found.widened)
        self.assertEqual(found.relaxed, [])

    def test_workplace_preferences_filter_and_then_relax(self):
        self.profile.open_to = ["remote"]
        self.profile.save(update_fields=["open_to", "updated_at"])
        onsite = self.publish_job(workplace=Workplace.ON_SITE)

        found = retrieval.retrieve(self.profile)
        # Only reachable by widening, and the result says which filter went.
        if onsite in found.jobs:
            self.assertTrue(found.widened)

    def test_the_text_embedded_from_a_job_is_what_it_says_not_who_posted_it(self):
        text = retrieval.text_for(self.job)
        self.assertIn(self.job.title, text)
        self.assertIn("Python", text)

    def test_the_text_embedded_from_a_profile_carries_no_contact_details(self):
        # An embedding is the one place personal data would leave the platform
        # without anybody reading it (SRS §9.3.7).
        self.profile.headline = "Backend engineer"
        self.profile.save(update_fields=["headline", "updated_at"])

        text = retrieval.text_for(self.profile)
        self.assertIn("Backend engineer", text)
        self.assertNotIn(self.profile.user.email, text)

    def test_something_that_is_not_embeddable_is_refused(self):
        with self.assertRaises(TypeError):
            retrieval.text_for(self.company)


class FeedTests(MatchingFixture):
    def test_the_feed_scores_every_job_it_returns(self):
        feed = recommendations.feed_for(self.profile)
        self.assertTrue(feed.items)
        for item in feed.items:
            with self.subTest(job=item.job.pk):
                self.assertGreaterEqual(item.match.score, 0)
                self.assertTrue(item.match.band)
                self.assertEqual(len(item.match.components), len(scoring.WEIGHTS))

    def test_the_feed_is_ordered_by_score(self):
        weak = self.publish_job()
        JobSkill.objects.create(job=weak, skill=self.kubernetes, requirement=Requirement.REQUIRED)

        feed = recommendations.feed_for(self.profile, use_cache=False)
        scores = [item.match.score for item in feed.items]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_nothing_is_persisted(self):
        # ADR-A21: the schema's biggest operational liability never exists.
        recommendations.feed_for(self.profile)
        self.assertEqual(
            [model for model in Job._meta.apps.get_models() if model._meta.app_label == "matching"],
            [],
        )

    def test_a_second_read_is_served_from_cache(self):
        first = recommendations.feed_for(self.profile)
        second = recommendations.feed_for(self.profile)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(
            [item.job.pk for item in first.items], [item.job.pk for item in second.items]
        )

    def test_a_new_posting_appears_without_anybody_sweeping_the_cache(self):
        # The failure `job_generation` exists to prevent: without it, a new job
        # cannot appear in a cached feed until the candidate happens to edit
        # their profile — stale exactly when a relevant job is posted.
        recommendations.feed_for(self.profile)
        before = len(recommendations.feed_for(self.profile).items)

        fresh = self.publish_job()
        exposure.bump_generation()

        after = recommendations.feed_for(self.profile)
        self.assertEqual(len(after.items), before + 1)
        self.assertIn(fresh.pk, [item.job.pk for item in after.items])

    def test_editing_the_profile_invalidates_the_feed(self):
        recommendations.feed_for(self.profile)
        key_before = recommendations.cache_key(self.profile)

        self.profile.headline = "Something new"
        self.profile.save(update_fields=["headline", "updated_at"])
        self.profile.refresh_from_db()

        self.assertNotEqual(recommendations.cache_key(self.profile), key_before)

    def test_the_cache_key_carries_every_staleness_class(self):
        key = recommendations.cache_key(self.profile)
        self.assertIn(str(self.profile.pk), key)
        self.assertIn(str(scoring.ALGORITHM_VERSION), key)
        self.assertIn(str(exposure.job_generation()), key)

    def test_a_cold_cache_costs_latency_and_never_correctness(self):
        warm = recommendations.feed_for(self.profile)
        cache.clear()
        cold = recommendations.feed_for(self.profile)
        self.assertEqual([item.job.pk for item in warm.items], [item.job.pk for item in cold.items])

    def test_a_cached_feed_whose_job_has_closed_is_recomputed(self):
        # Showing a candidate a posting that has gone is worse than the fraction
        # of a second recomputing costs.
        recommendations.feed_for(self.profile)
        Job.objects.filter(pk=self.job.pk).update(status="closed")

        feed = recommendations.feed_for(self.profile)
        self.assertNotIn(self.job.pk, [item.job.pk for item in feed.items])

    def test_invalidate_drops_the_cached_feed(self):
        recommendations.feed_for(self.profile)
        recommendations.invalidate(self.profile)
        self.assertFalse(recommendations.feed_for(self.profile).cached)

    def test_a_candidate_with_few_skills_is_matched_provisionally(self):
        bare = create_profile()
        feed = recommendations.feed_for(bare)
        self.assertTrue(feed.provisional)
        self.assertTrue(all(item.match.provisional for item in feed.items))

    def test_a_candidate_with_enough_skills_is_not_provisional(self):
        CandidateSkill.objects.create(profile=self.profile, skill=self.kubernetes)
        self.assertFalse(recommendations.feed_for(self.profile).provisional)


class PairScoringTests(MatchingFixture):
    def test_one_pair_can_be_scored_directly(self):
        match = recommendations.score_pair(self.profile, self.job)
        self.assertGreater(match.score, 0)
        self.assertIn(self.python.pk, match.matched_skill_ids)

    def test_the_candidate_and_the_employer_see_the_same_number(self):
        # FR-8.4: the same scorer and the same breakdown on both sides.
        direct = recommendations.score_pair(self.profile, self.job)
        ranked = recommendations.rank_candidates(self.job, [self.profile])
        self.assertEqual(ranked[0][1].as_dict(), direct.as_dict())

    def test_ranking_orders_candidates_by_score(self):
        weaker = create_profile()
        ranked = recommendations.rank_candidates(self.job, [weaker, self.profile])
        self.assertEqual(ranked[0][0], self.profile)
        self.assertGreaterEqual(ranked[0][1].score, ranked[1][1].score)

    def test_an_unpublished_job_is_not_found_by_public_id(self):
        from apps.common.errors import NotFound

        draft = create_job(self.company)
        with self.assertRaises(NotFound):
            recommendations.recommendation_for(self.profile, job_public_id=str(draft.public_id))

    def test_a_published_job_is_found_and_scored(self):
        found = recommendations.recommendation_for(
            self.profile, job_public_id=str(self.job.public_id)
        )
        self.assertEqual(found.job, self.job)
        self.assertGreater(found.score, 0)


class ExposureTests(MatchingFixture):
    def test_one_company_cannot_fill_the_feed(self):
        # A feed that is nine-tenths one employer is a feed with one employer
        # in it (FR-8.8).
        for _ in range(6):
            self.publish_job(company=self.company)
        other = create_company(slug="other-co", name="Other Co")
        rival = self.publish_job(company=other)

        feed = recommendations.feed_for(self.profile, use_cache=False)
        top_five = [item.job.company_id for item in feed.items[:5]]
        self.assertIn(rival.company_id, top_five)

    def test_the_penalty_is_capped_so_one_employer_is_never_hidden(self):
        # A candidate who genuinely only fits one company's roles must still
        # see those roles.
        for _ in range(12):
            self.publish_job(company=self.company)
        feed = recommendations.feed_for(self.profile, use_cache=False)
        self.assertGreaterEqual(len(feed.items), 12)

    def test_ordering_changes_but_the_score_does_not(self):
        # The candidate is told a number; moving a posting down a list must not
        # change what that number is.
        for _ in range(4):
            self.publish_job(company=self.company)
        feed = recommendations.feed_for(self.profile, use_cache=False)

        for item in feed.items:
            direct = recommendations.score_pair(self.profile, item.job)
            with self.subTest(job=item.job.pk):
                self.assertEqual(item.match.score, direct.score)

    def test_impressions_are_counted(self):
        exposure.record_impressions([self.job.pk, self.job.pk])
        self.assertEqual(exposure.impressions_for(self.job.pk), 2)

    def test_a_heavily_shown_posting_is_pushed_down(self):
        rival = self.publish_job(company=create_company(slug="rival", name="Rival"))
        JobSkill.objects.create(job=rival, skill=self.python, requirement=Requirement.REQUIRED)

        # Same shape, so only exposure can separate them.
        for _ in range(exposure.DAILY_IMPRESSION_SOFT_CAP * 3):
            exposure.record_impressions([self.job.pk])

        feed = recommendations.feed_for(self.profile, use_cache=False)
        order = [item.job.pk for item in feed.items]
        self.assertLess(order.index(rival.pk), order.index(self.job.pk))

    def test_the_generation_counter_starts_at_zero_and_advances(self):
        cache.clear()
        self.assertEqual(exposure.job_generation(), 0)
        self.assertEqual(exposure.bump_generation(), 1)
        self.assertEqual(exposure.job_generation(), 1)

    def test_applying_caps_to_an_empty_feed_is_not_an_error(self):
        self.assertEqual(exposure.apply_caps([]), [])


class SnapshotTests(MatchingFixture):
    def test_a_snapshot_carries_the_score_and_the_breakdown(self):
        snapshot = snapshots.snapshot_for(self.profile, self.job)
        self.assertIn("score", snapshot)
        self.assertEqual(snapshot["algorithm_version"], scoring.ALGORITHM_VERSION)
        self.assertEqual(len(snapshot["components"]), len(scoring.WEIGHTS))

    def test_a_snapshot_never_fails_a_submission(self):
        # The application is the thing that matters to the candidate. An empty
        # snapshot is a missing analytic, not a lost opportunity.
        broken = create_profile()
        broken.pk = None
        self.assertEqual(snapshots.snapshot_for(broken, self.job), {})

    def test_a_snapshot_from_an_older_scorer_is_recognisable(self):
        application = application_for(
            self.profile, self.job, match_snapshot={"score": 50, "algorithm_version": 0}
        )
        self.assertTrue(snapshots.is_stale(application, current_version=scoring.ALGORITHM_VERSION))

    def test_a_current_snapshot_is_not_stale(self):
        application = application_for(
            self.profile,
            self.job,
            match_snapshot={"score": 50, "algorithm_version": scoring.ALGORITHM_VERSION},
        )
        self.assertFalse(snapshots.is_stale(application, current_version=scoring.ALGORITHM_VERSION))

    def test_an_empty_snapshot_is_not_reported_as_stale(self):
        # Absent and superseded are different facts, and the interface says
        # different things about them.
        application = application_for(self.profile, self.job, match_snapshot={})
        self.assertFalse(snapshots.is_stale(application, current_version=scoring.ALGORITHM_VERSION))
