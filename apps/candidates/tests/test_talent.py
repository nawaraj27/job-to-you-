"""
Talent search: who may be found, who may be read, what is recorded
(FR-9.1 to FR-9.4, SRS §10.6, INV-4).

The properties this file is answerable for, each asserted from the failing side:

  * **a candidate on `employers` who has not applied is not discoverable.**
    "Employers you've applied to" is narrower than "employers", and reading it
    as the wider one would be the platform making their decision for them;
  * **no employer can read a profile without the candidate being told.** The
    view is written in the same transaction as the grant;
  * search needs `candidate.search`, export needs `candidate.export`, and an
    unauthorised candidate is `NotFound` rather than `NotAllowed`;
  * an export carries no contact details, is capped, logs every row as a view,
    and feeds anomaly detection;
  * ranking is deterministic and its weights sum to 1.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.applications.models import CandidateVisibilityGrant, GrantBasis
from apps.applications.services import visibility as visibility_service
from apps.audit.models import AuditLog
from apps.candidates import search as talent_search
from apps.candidates.models import (
    CandidateSkill,
    ProfileExperience,
    ProfileView,
    Seeking,
    ViewSource,
    Visibility,
)
from apps.candidates.services import profile as profile_service
from apps.candidates.services import talent as talent_service
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotAllowed, NotFound
from apps.companies.models import Role
from apps.companies.tests.factories import add_member, company_with_owner, create_company
from apps.notifications.models import OutboxEvent
from apps.taxonomy.models import Location, Skill


def discoverable(**overrides):
    """A profile a search can find: complete enough, and publicly visible.

    `is_searchable` is derived from completeness, not chosen (P3), so it is set
    **after** any recompute rather than before — a fixture that sets it first
    has it silently overwritten and then fails somewhere else entirely. Setting
    it directly is a factory constructing state, not a service bypass: what is
    under test is the search, and building a genuinely 80%-complete profile in
    every test would be forty lines of setup for one boolean.
    """
    profile = create_profile(
        headline=overrides.pop("headline", "Backend engineer building payment systems"),
        visibility=overrides.pop("visibility", Visibility.EVERYONE),
        seeking=overrides.pop("seeking", Seeking.ACTIVE),
        **overrides,
    )
    return make_searchable(profile)


def make_searchable(profile):
    """Force the derived discoverability fields. Call last."""
    profile.is_searchable = profile.visibility != Visibility.NOBODY
    profile.completeness = max(profile.completeness, 80)
    profile.save(update_fields=["is_searchable", "completeness", "updated_at"])
    profile.refresh_from_db()
    return profile


class TalentFixture(TestCase):
    def setUp(self):
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)

        self.python = Skill.objects.create(name="Python", slug="python")
        self.kubernetes = Skill.objects.create(name="Kubernetes", slug="kubernetes")
        # `name` is a derived property on Location, not a column.
        self.kathmandu = Location.objects.create(country="NP", city="Kathmandu")

        self.candidate = discoverable()
        self.candidate.location = self.kathmandu
        self.candidate.save(update_fields=["location", "updated_at"])
        CandidateSkill.objects.create(profile=self.candidate, skill=self.python, years=Decimal("4"))
        ProfileExperience.objects.create(
            profile=self.candidate,
            title="Backend Engineer",
            org_name="Nexa",
            start_date=dt.date(2021, 1, 1),
            end_date=dt.date(2024, 1, 1),
        )
        profile_service.recompute(self.candidate)
        make_searchable(self.candidate)

    def query(self, **kwargs) -> talent_search.TalentQuery:
        return talent_search.TalentQuery(**kwargs)

    def search(self, **kwargs):
        return talent_service.search(
            membership=self.membership, actor=self.owner, query=self.query(**kwargs)
        )


class VisibilityTests(TalentFixture):
    """FR-9.1. Who is findable, and by whom."""

    def test_a_public_candidate_is_findable(self):
        found = talent_service.searchable_for(self.company)
        self.assertIn(self.candidate, found)

    def test_a_candidate_who_chose_nobody_is_not_findable(self):
        hidden = discoverable(visibility=Visibility.NOBODY)
        self.assertNotIn(hidden, talent_service.searchable_for(self.company))

    def test_a_candidate_on_employers_who_has_not_applied_is_not_findable(self):
        # "Employers you've applied to" is narrower than "employers". Reading it
        # as the wider one would be the platform making their decision.
        applied_only = discoverable(visibility=Visibility.EMPLOYERS)
        self.assertNotIn(applied_only, talent_service.searchable_for(self.company))

    def test_a_candidate_on_employers_who_applied_here_is_findable(self):
        applied = discoverable(visibility=Visibility.EMPLOYERS)
        visibility_service.grant(
            candidate=applied, company=self.company, basis=GrantBasis.APPLICATION
        )
        self.assertIn(applied, talent_service.searchable_for(self.company))

    def test_a_grant_to_another_company_does_not_make_them_findable_here(self):
        applied = discoverable(visibility=Visibility.EMPLOYERS)
        other = create_company(slug="other-co", name="Other Co")
        visibility_service.grant(candidate=applied, company=other, basis=GrantBasis.APPLICATION)
        self.assertNotIn(applied, talent_service.searchable_for(self.company))

    def test_a_revoked_grant_removes_them_again(self):
        applied = discoverable(visibility=Visibility.EMPLOYERS)
        visibility_service.grant(
            candidate=applied, company=self.company, basis=GrantBasis.APPLICATION
        )
        visibility_service.revoke_all(candidate=applied, company=self.company)
        self.assertNotIn(applied, talent_service.searchable_for(self.company))

    def test_an_incomplete_profile_is_not_findable(self):
        # Ranking an empty profile against a real one wastes both sides' time.
        thin = create_profile(visibility=Visibility.EVERYONE)
        self.assertFalse(thin.is_searchable)
        self.assertNotIn(thin, talent_service.searchable_for(self.company))


class SearchPermissionTests(TalentFixture):
    def test_searching_needs_the_search_grant(self):
        stranger = create_profile().user
        membership = add_member(self.company, stranger, Role.INTERVIEWER)

        with self.assertRaises(NotAllowed):
            talent_service.search(
                membership=membership, actor=stranger, query=self.query(text="engineer")
            )

    def test_a_recruiter_may_search(self):
        recruiter = create_profile().user
        membership = add_member(self.company, recruiter, Role.RECRUITER)

        result = talent_service.search(
            membership=membership, actor=recruiter, query=self.query(text="engineer")
        )
        self.assertGreaterEqual(result.total, 1)

    def test_an_empty_query_is_refused(self):
        # "Show me everyone" is not a search — it is a download, and it is the
        # shape a scraper's first request takes.
        with self.assertRaises(InvalidState):
            self.search()

    def test_a_search_is_audited_without_naming_who_came_back(self):
        self.search(text="engineer")

        row = AuditLog.objects.filter(verb="candidate.searched").first()
        self.assertIsNotNone(row)
        self.assertNotIn(str(self.candidate.public_id), str(row.metadata))
        self.assertIn("results", row.metadata)

    def test_searching_does_not_log_a_view_per_result(self):
        # A result row is a headline, not a profile read. Logging one per row
        # would drown the candidate's screen until it told them nothing.
        self.search(text="engineer")
        self.assertFalse(ProfileView.objects.exists())


class SearchFilterTests(TalentFixture):
    """FR-9.2."""

    def test_a_skill_filter_narrows_the_result(self):
        other = discoverable(headline="Frontend developer")
        CandidateSkill.objects.create(profile=other, skill=self.kubernetes)

        result = self.search(skill_ids=(self.python.pk,))
        self.assertIn(self.candidate, result.profiles)
        self.assertNotIn(other, result.profiles)

    def test_a_candidate_matching_several_wanted_skills_appears_once(self):
        # The join fan-out that makes a count wrong rather than merely ugly.
        CandidateSkill.objects.create(profile=self.candidate, skill=self.kubernetes)

        result = self.search(skill_ids=(self.python.pk, self.kubernetes.pk))
        self.assertEqual(result.total, 1)
        self.assertEqual(len(result.profiles), 1)

    def test_free_text_finds_a_candidate_by_a_skill_they_hold(self):
        """The single most likely thing anybody types into that box.

        `search_vector` is a generated column over the headline and the
        summary, and skills live in a related table it cannot reach — so a
        recruiter typing "Python" matched nobody, while candidates with Python
        on their profile sat in the corpus.
        """
        holder = discoverable(headline="Engineer who writes no prose")
        CandidateSkill.objects.create(profile=holder, skill=self.python)

        result = self.search(text="Python")

        self.assertIn(holder, result.profiles)

    def test_a_candidate_with_several_matching_skills_appears_once(self):
        # The same join fan-out as the skill filter, in the text path — and one
        # `distinct()` does not fix it, because the ranking annotations join the
        # table again afterwards.
        # Two skills that both contain the term. (Was "e", which decision D-05
        # now refuses: one letter no longer matches skills by substring.)
        testing = Skill.objects.create(name="Python testing", slug="python-testing")
        CandidateSkill.objects.create(profile=self.candidate, skill=testing)

        result = self.search(text="pyt")

        self.assertEqual(result.total, len({profile.pk for profile in result.profiles}))

    def test_free_text_still_matches_the_words_on_the_profile(self):
        written = discoverable(headline="Reconciliation specialist")

        result = self.search(text="reconciliation")

        self.assertIn(written, result.profiles)

    def test_a_nonsense_token_matches_no_skill_or_word(self):
        # Was one of two tests sharing a name, so Python silently discarded it
        # and it never ran. A nonsense token cannot match a skill name or a word.
        self.assertEqual(self.search(text="zzzznotaskill").total, 0)

    def test_a_location_filter_narrows_the_result(self):
        elsewhere = discoverable(headline="Engineer elsewhere")
        result = self.search(location_id=self.kathmandu.pk)

        self.assertIn(self.candidate, result.profiles)
        self.assertNotIn(elsewhere, result.profiles)

    def test_a_remote_willing_candidate_matches_any_location(self):
        # An employer filtering by city says where the work is, not where the
        # person must sleep.
        remote = discoverable(headline="Remote engineer", open_to=["remote"])
        result = self.search(location_id=self.kathmandu.pk, remote_ok=True)
        self.assertIn(remote, result.profiles)

    def test_an_availability_filter_narrows_the_result(self):
        passive = discoverable(headline="Engineer not looking", seeking=Seeking.NOT_LOOKING)
        result = self.search(seeking=Seeking.ACTIVE)

        self.assertIn(self.candidate, result.profiles)
        self.assertNotIn(passive, result.profiles)

    def test_an_experience_filter_uses_the_derived_column(self):
        junior = discoverable(headline="Junior engineer")
        ProfileExperience.objects.create(
            profile=junior,
            title="Junior",
            org_name="Somewhere",
            start_date=timezone.localdate() - dt.timedelta(days=200),
        )
        profile_service.recompute(junior)
        make_searchable(junior)

        result = self.search(min_experience_months=24)
        self.assertIn(self.candidate, result.profiles)
        self.assertNotIn(junior, result.profiles)

    def test_a_salary_ceiling_does_not_exclude_a_candidate_who_stated_nothing(self):
        # Most have not stated one, and filtering them out would hide the
        # majority of the corpus to enforce a ceiling nobody contradicted.
        result = self.search(text="engineer", max_salary_base=Decimal("500000"))
        self.assertIn(self.candidate, result.profiles)

    def test_a_salary_ceiling_excludes_somebody_asking_for_more(self):
        expensive = discoverable(headline="Expensive engineer")
        expensive.salary_min_base = Decimal("9000000")
        expensive.save(update_fields=["salary_min_base", "updated_at"])

        result = self.search(text="engineer", max_salary_base=Decimal("500000"))
        self.assertNotIn(expensive, result.profiles)

    def test_free_text_matches_the_headline(self):
        result = self.search(text="payment")
        self.assertIn(self.candidate, result.profiles)

    def test_a_real_word_on_no_profile_matches_nothing(self):
        # A dictionary word, unlike the nonsense token above, goes through
        # full-text stemming, which is where a false match would come from.
        self.assertEqual(self.search(text="ornithology").total, 0)


class RankingTests(TalentFixture):
    def test_the_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(talent_search.WEIGHTS.values()), 1.0, places=6)

    def test_the_order_is_deterministic(self):
        for index in range(4):
            other = discoverable(headline=f"Backend engineer {index}")
            CandidateSkill.objects.create(profile=other, skill=self.python)

        first = [row.pk for row in self.search(skill_ids=(self.python.pk,)).profiles]
        second = [row.pk for row in self.search(skill_ids=(self.python.pk,)).profiles]
        self.assertEqual(first, second)

    def test_matching_more_of_the_wanted_skills_ranks_higher(self):
        both = discoverable(headline="Engineer with both")
        CandidateSkill.objects.create(profile=both, skill=self.python)
        CandidateSkill.objects.create(profile=both, skill=self.kubernetes)

        result = self.search(skill_ids=(self.python.pk, self.kubernetes.pk))
        self.assertEqual(result.profiles[0], both)

    def test_an_actively_looking_candidate_outranks_an_identical_passive_one(self):
        passive = discoverable(
            headline="Backend engineer building payment systems", seeking=Seeking.NOT_LOOKING
        )
        CandidateSkill.objects.create(profile=passive, skill=self.python)
        passive.completeness = self.candidate.completeness
        passive.experience_months = self.candidate.experience_months
        passive.save(update_fields=["completeness", "experience_months", "updated_at"])

        result = self.search(skill_ids=(self.python.pk,))
        order = [row.pk for row in result.profiles]
        self.assertLess(order.index(self.candidate.pk), order.index(passive.pk))

    def test_sorting_by_experience_uses_the_real_months(self):
        senior = discoverable(headline="Backend engineer, long career")
        ProfileExperience.objects.create(
            profile=senior,
            title="Engineer",
            org_name="Long Co",
            start_date=dt.date(2010, 1, 1),
            end_date=dt.date(2024, 1, 1),
        )
        profile_service.recompute(senior)
        make_searchable(senior)

        result = self.search(text="engineer", sort="experience")
        self.assertEqual(result.profiles[0], senior)

    def test_experience_months_is_the_shared_arithmetic(self):
        # Overlapping roles counted once — the same answer the CV analyser and
        # the matcher give about the same person (P12, P13).
        ProfileExperience.objects.create(
            profile=self.candidate,
            title="Freelance",
            org_name="Self",
            start_date=dt.date(2021, 1, 1),
            end_date=dt.date(2024, 1, 1),
        )
        profile_service.recompute(self.candidate)

        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.experience_months, 36)


class ViewTests(TalentFixture):
    """FR-9.3 and INV-4."""

    def open_profile(self, profile=None, **kwargs):
        return talent_service.view_profile(
            membership=self.membership,
            actor=self.owner,
            public_id=str((profile or self.candidate).public_id),
            **kwargs,
        )

    def test_opening_a_public_profile_creates_the_grant(self):
        # INV-4: every employer read is authorised by a grant row, including
        # the first one.
        self.open_profile()

        self.assertTrue(
            CandidateVisibilityGrant.objects.live()
            .filter(candidate=self.candidate, company=self.company, basis=GrantBasis.PUBLIC)
            .exists()
        )

    def test_opening_a_profile_records_a_view_the_candidate_can_see(self):
        self.open_profile()

        view = ProfileView.objects.get(candidate=self.candidate)
        self.assertEqual(view.company, self.company)
        self.assertEqual(view.actor, self.owner)
        self.assertEqual(view.source, ViewSource.SEARCH)

    def test_the_grant_and_the_view_are_written_together(self):
        # A read that was authorised but not recorded is exactly the read a
        # candidate is entitled to know about.
        self.open_profile()
        self.assertEqual(ProfileView.objects.count(), 1)
        self.assertEqual(
            CandidateVisibilityGrant.objects.filter(candidate=self.candidate).count(), 1
        )

    def test_opening_it_again_does_not_stack_grants(self):
        self.open_profile()
        self.open_profile()

        self.assertEqual(
            CandidateVisibilityGrant.objects.filter(
                candidate=self.candidate, basis=GrantBasis.PUBLIC
            ).count(),
            1,
        )
        self.assertEqual(ProfileView.objects.count(), 2)

    def test_a_candidate_this_company_may_not_see_is_not_found(self):
        # `NotFound`, never `NotAllowed`: a 403 would confirm a person with that
        # id is on the platform, which is what the setting withholds.
        hidden = discoverable(visibility=Visibility.NOBODY)
        with self.assertRaises(NotFound):
            self.open_profile(hidden)

    def test_a_refused_read_records_nothing(self):
        hidden = discoverable(visibility=Visibility.NOBODY)
        with self.assertRaises(NotFound):
            self.open_profile(hidden)

        self.assertFalse(ProfileView.objects.filter(candidate=hidden).exists())
        self.assertFalse(CandidateVisibilityGrant.objects.filter(candidate=hidden).exists())

    def test_viewing_needs_the_search_grant(self):
        stranger = create_profile().user
        membership = add_member(self.company, stranger, Role.INTERVIEWER)

        with self.assertRaises(NotAllowed):
            talent_service.view_profile(
                membership=membership,
                actor=stranger,
                public_id=str(self.candidate.public_id),
            )

    def test_the_view_is_audited(self):
        self.open_profile()
        self.assertTrue(AuditLog.objects.filter(verb="candidate.viewed").exists())

    def test_an_applicant_read_is_recorded_with_its_own_source(self):
        applied = discoverable(visibility=Visibility.EMPLOYERS)
        visibility_service.grant(
            candidate=applied, company=self.company, basis=GrantBasis.APPLICATION
        )
        self.open_profile(applied, source=ViewSource.APPLICATION)

        self.assertEqual(ProfileView.objects.get(candidate=applied).source, ViewSource.APPLICATION)


class WhoViewedMeTests(TalentFixture):
    """FR-2.8, from the candidate's side."""

    def test_the_candidate_sees_which_company_looked(self):
        talent_service.view_profile(
            membership=self.membership,
            actor=self.owner,
            public_id=str(self.candidate.public_id),
        )
        rows = talent_service.views_for(self.candidate)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], self.company.name)
        self.assertEqual(rows[0]["times"], 1)

    def test_repeat_reads_in_one_day_are_grouped(self):
        # Ungrouped, ordinary interest reads as surveillance.
        for _ in range(4):
            talent_service.view_profile(
                membership=self.membership,
                actor=self.owner,
                public_id=str(self.candidate.public_id),
            )

        rows = talent_service.views_for(self.candidate)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["times"], 4)

    def test_the_individual_recruiter_is_not_named(self):
        # The company is accountable for the read; naming a person would expose
        # one person's browsing to another.
        talent_service.view_profile(
            membership=self.membership,
            actor=self.owner,
            public_id=str(self.candidate.public_id),
        )
        row = talent_service.views_for(self.candidate)[0]

        self.assertNotIn(self.owner.email, str(row))
        self.assertNotIn("actor", row)

    def test_two_companies_appear_separately(self):
        other = create_company(slug="other-co", name="Other Co")
        other_owner = create_profile().user
        other_membership = add_member(other, other_owner, Role.OWNER)

        talent_service.view_profile(
            membership=self.membership,
            actor=self.owner,
            public_id=str(self.candidate.public_id),
        )
        talent_service.view_profile(
            membership=other_membership,
            actor=other_owner,
            public_id=str(self.candidate.public_id),
        )

        names = {row["company"] for row in talent_service.views_for(self.candidate)}
        self.assertEqual(names, {self.company.name, "Other Co"})

    def test_a_candidate_nobody_looked_at_sees_nothing(self):
        self.assertEqual(talent_service.views_for(self.candidate), [])


class ExportTests(TalentFixture):
    """FR-9.4."""

    def export(self, membership=None, actor=None, ids=None):
        return talent_service.export(
            membership=membership or self.membership,
            actor=actor or self.owner,
            public_ids=ids if ids is not None else [str(self.candidate.public_id)],
        )

    def test_export_needs_its_own_grant(self):
        # A recruiter may search and may not export: taking a list of people off
        # the platform is a different act from looking at them.
        recruiter = create_profile().user
        membership = add_member(self.company, recruiter, Role.RECRUITER)

        with self.assertRaises(NotAllowed):
            self.export(membership=membership, actor=recruiter)

    def test_an_owner_may_export(self):
        rows = self.export()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["public_id"], str(self.candidate.public_id))

    def test_an_export_carries_no_contact_details(self):
        # ADR-A34 reveals those at a stage requiring an application, and an
        # export is not that stage.
        blob = str(self.export())
        self.assertNotIn(self.candidate.user.email, blob)
        self.assertNotIn("phone", blob)

    def test_an_export_only_carries_allow_listed_fields(self):
        # So a column added later cannot leave in a spreadsheet because nobody
        # thought about it.
        row = self.export()[0]
        self.assertEqual(set(row), set(talent_service.EXPORT_FIELDS))

    def test_every_exported_candidate_is_told(self):
        # An export that did not appear on the candidate's screen would be a way
        # to read a hundred profiles invisibly.
        self.export()
        self.assertEqual(ProfileView.objects.filter(candidate=self.candidate).count(), 1)

    def test_an_empty_export_is_refused(self):
        with self.assertRaises(InvalidState):
            self.export(ids=[])

    def test_an_oversized_export_is_refused(self):
        ids = [str(self.candidate.public_id)] * (talent_service.EXPORT_LIMIT + 1)
        with self.assertRaises(InvalidState):
            self.export(ids=ids)

    def test_a_candidate_this_company_may_not_see_is_not_exported(self):
        hidden = discoverable(visibility=Visibility.NOBODY)
        rows = self.export(ids=[str(hidden.public_id), str(self.candidate.public_id)])

        self.assertEqual(len(rows), 1)
        self.assertFalse(ProfileView.objects.filter(candidate=hidden).exists())

    def test_the_export_is_audited_with_its_size(self):
        self.export()
        row = AuditLog.objects.filter(verb="candidate.exported").first()
        self.assertEqual(row.metadata["count"], 1)


class AnomalyTests(TalentFixture):
    """§15.4. Reports, never blocks."""

    def test_an_ordinary_export_raises_nothing(self):
        from apps.candidates.services import anomaly

        signals = anomaly.check_export(company=self.company, actor=self.owner, count=3)
        self.assertEqual(signals, [])

    def test_many_small_exports_in_a_day_are_noticed(self):
        # Somebody paging a shortlist out in small batches to stay under a cap.
        from apps.candidates.services import anomaly

        for _ in range(anomaly.DAILY_EXPORT_COUNT_THRESHOLD):
            AuditLog.objects.create(
                verb="candidate.exported", company=self.company, metadata={"count": 2}
            )

        signals = anomaly.check_export(company=self.company, actor=self.owner, count=2)
        self.assertTrue(any(signal.kind == "export_frequency" for signal in signals))

    def test_a_large_daily_export_volume_is_an_alert(self):
        from apps.candidates.services import anomaly

        AuditLog.objects.create(
            verb="candidate.exported",
            company=self.company,
            metadata={"count": anomaly.DAILY_EXPORT_THRESHOLD},
        )
        signals = anomaly.check_export(company=self.company, actor=self.owner, count=1)

        self.assertTrue(any(signal.severity == "alert" for signal in signals))

    def test_heavy_viewing_is_flagged(self):
        from apps.candidates.services import anomaly

        ProfileView.objects.bulk_create(
            ProfileView(candidate=self.candidate, company=self.company, actor=self.owner)
            for _ in range(anomaly.DAILY_VIEW_THRESHOLD)
        )
        signals = anomaly.check_viewing(company=self.company, actor=self.owner)

        self.assertTrue(any(signal.kind == "view_volume" for signal in signals))

    def test_the_day_is_the_platform_s_local_day_not_utc(self):
        """Views at 04:45 Kathmandu count towards the same day as views at 06:15.

        The day started at UTC midnight (05:45 in Kathmandu), so a company's
        morning was split across two "days" and could straddle the threshold.
        """
        import datetime as dt
        from unittest import mock

        from apps.candidates.services import anomaly

        earlier = dt.datetime(2026, 9, 14, 23, 0, tzinfo=dt.UTC)  # 04:45 on the 15th locally
        checked = dt.datetime(2026, 9, 15, 0, 30, tzinfo=dt.UTC)  # 06:15 on the 15th locally
        ProfileView.objects.bulk_create(
            ProfileView(candidate=self.candidate, company=self.company, actor=self.owner)
            for _ in range(anomaly.DAILY_VIEW_THRESHOLD)
        )
        ProfileView.objects.filter(company=self.company).update(created_at=earlier)

        with mock.patch("django.utils.timezone.now", return_value=checked):
            signals = anomaly.check_viewing(company=self.company, actor=self.owner)

        self.assertTrue(any(signal.kind == "view_volume" for signal in signals))

    def test_a_flag_never_blocks_the_export(self):
        # A false positive that blocks a real recruiter is a lost customer; one
        # that raises a flag costs somebody two minutes of reading.
        from apps.candidates.services import anomaly

        AuditLog.objects.create(
            verb="candidate.exported",
            company=self.company,
            metadata={"count": anomaly.DAILY_EXPORT_THRESHOLD},
        )
        rows = talent_service.export(
            membership=self.membership,
            actor=self.owner,
            public_ids=[str(self.candidate.public_id)],
        )
        self.assertEqual(len(rows), 1)

    def test_a_flag_emits_an_event_without_naming_candidates(self):
        # An alert about somebody reading a list of people must not carry the
        # list.
        from apps.candidates.services import anomaly

        ProfileView.objects.bulk_create(
            ProfileView(candidate=self.candidate, company=self.company, actor=self.owner)
            for _ in range(anomaly.DAILY_VIEW_THRESHOLD)
        )
        anomaly.check_viewing(company=self.company, actor=self.owner)

        event = OutboxEvent.objects.filter(type="candidate.access_anomaly").first()
        self.assertIsNotNone(event)
        self.assertNotIn(str(self.candidate.public_id), str(event.payload))

    def test_a_flag_is_audited(self):
        from apps.candidates.services import anomaly

        ProfileView.objects.bulk_create(
            ProfileView(candidate=self.candidate, company=self.company, actor=self.owner)
            for _ in range(anomaly.DAILY_VIEW_THRESHOLD)
        )
        anomaly.check_viewing(company=self.company, actor=self.owner)

        self.assertTrue(AuditLog.objects.filter(verb="candidate.view_anomaly").exists())

    def test_the_sweep_checks_active_companies(self):
        from apps.candidates.services import anomaly

        ProfileView.objects.create(candidate=self.candidate, company=self.company, actor=self.owner)
        result = anomaly.sweep()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["flagged"], 0)

    def test_the_sweep_flags_a_company_over_the_line(self):
        from apps.candidates.services import anomaly

        ProfileView.objects.bulk_create(
            ProfileView(candidate=self.candidate, company=self.company, actor=self.owner)
            for _ in range(anomaly.DAILY_VIEW_THRESHOLD)
        )
        self.assertEqual(anomaly.sweep()["flagged"], 1)
