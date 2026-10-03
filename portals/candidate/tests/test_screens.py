"""
Candidate screens on real records (P3).

These are the tests that would have caught the failure this phase exists to
fix: a screen that renders someone else's data, or a save that reports success
while writing nothing.
"""

from __future__ import annotations

import json

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import UserPreferences
from apps.accounts.tests.factories import create_user
from apps.candidates.models import CandidateProfile, Visibility
from apps.notifications.models import NotificationPreference
from apps.resumes import content as content_rules
from apps.resumes.services import versions as version_service


class SignedOutTests(TestCase):
    def test_every_candidate_screen_requires_signing_in(self):
        for name in (
            "dashboard:index",
            "dashboard:profile",
            "settings:index",
            "settings:notifications",
            "settings:privacy",
            "settings:appearance",
            "settings:data",
        ):
            with self.subTest(url=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/", response["Location"])


class ProfileScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.user = create_user(full_name="Priya Sharma")
        self.client.force_login(self.user)
        self.url = reverse("dashboard:profile")
        self.act = reverse("dashboard:profile_act")

    def test_visiting_creates_the_profile_once(self):
        self.client.get(self.url)
        self.client.get(self.url)

        self.assertEqual(CandidateProfile.objects.filter(user=self.user).count(), 1)

    def test_the_screen_renders_the_signed_in_person(self):
        response = self.client.get(self.url)

        self.assertContains(response, "Priya Sharma")
        self.assertContains(response, "Profile strength")

    def test_each_edit_toggle_has_a_name_before_any_script_runs(self):
        # The label came only from Alpine's x-text: the server-rendered buttons
        # were empty, and two of them would both have been read as "Edit".
        response = self.client.get(self.url)

        self.assertContains(response, 'Edit</span><span class="sr-only"> basics</span>', html=False)
        self.assertContains(
            response,
            'Edit</span><span class="sr-only"> what you\'re looking for</span>',
            html=False,
        )

    def test_saving_basics_writes_and_reports_it(self):
        self.client.get(self.url)

        response = self.client.post(
            self.act, {"command": "basics", "section": "basics", "headline": "Backend Engineer"}
        )

        self.assertContains(response, "Backend Engineer")
        self.assertEqual(CandidateProfile.objects.get(user=self.user).headline, "Backend Engineer")
        self.assertIn("Profile updated", response["HX-Trigger"])

    def test_a_rejected_save_says_why_and_reopens_the_section(self):
        self.client.get(self.url)

        response = self.client.post(
            self.act, {"command": "basics", "section": "basics", "phone": "12"}
        )

        # Static template text is not escaped; the message below it is.
        self.assertContains(response, "That didn't save")
        self.assertContains(response, "phone number")

    def test_adding_a_skill_shows_it(self):
        self.client.get(self.url)

        response = self.client.post(self.act, {"command": "add_skill", "skill": "JS"})

        self.assertContains(response, "JavaScript")
        self.assertIn("Added JavaScript", response["HX-Trigger"])

    def test_an_unknown_skill_comes_back_with_suggestions(self):
        self.client.get(self.url)

        response = self.client.post(self.act, {"command": "add_skill", "skill": "postg"})

        self.assertContains(response, "Did you mean")

    def test_removing_a_skill_updates_the_meter(self):
        self.client.get(self.url)
        self.client.post(self.act, {"command": "add_skill", "skill": "Python"})
        skill_id = CandidateProfile.objects.get(user=self.user).skills.first().skill_id

        response = self.client.post(self.act, {"command": "remove_skill", "skill_id": skill_id})

        self.assertEqual(CandidateProfile.objects.get(user=self.user).skills.count(), 0)
        self.assertIn("Skill removed", response["HX-Trigger"])

    def test_adding_an_experience_renders_it(self):
        self.client.get(self.url)

        response = self.client.post(
            self.act,
            {
                "command": "add_entry",
                "section": "experience",
                "title": "Backend Engineer",
                "org_name": "Nexa Payments",
                "start_date": "2023-08-01",
            },
        )

        self.assertContains(response, "Nexa Payments")

    def test_changing_visibility_from_the_profile_screen(self):
        self.client.get(self.url)

        self.client.post(self.act, {"command": "visibility", "value": Visibility.EVERYONE})

        self.assertEqual(
            CandidateProfile.objects.get(user=self.user).visibility, Visibility.EVERYONE
        )

    def test_an_unknown_command_is_a_bad_request(self):
        self.client.get(self.url)

        response = self.client.post(self.act, {"command": "drop_tables"})

        self.assertEqual(response.status_code, 400)

    def test_one_account_cannot_edit_another_account_s_profile(self):
        # There is no profile id in the request at all — the profile is
        # resolved from the session, which is what makes this structural.
        self.client.get(self.url)
        other = create_user()
        self.client.force_login(other)
        self.client.post(self.act, {"command": "basics", "headline": "Theirs"})

        self.assertEqual(CandidateProfile.objects.get(user=self.user).headline, "")
        self.assertEqual(CandidateProfile.objects.get(user=other).headline, "Theirs")


class OverviewTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.client.force_login(self.user)

    def test_a_new_account_is_told_what_to_do_first(self):
        response = self.client.get(reverse("dashboard:index"))

        self.assertContains(response, "Add the skills")

    def test_the_next_action_follows_the_real_profile(self):
        call_command("seed_taxonomy", quiet_summary=True)
        from apps.candidates.services import profile as profile_service
        from apps.candidates.services import skills as skill_service

        profile = profile_service.get_or_create_profile(user=self.user)
        for name in ("Python", "Django", "PostgreSQL"):
            skill_service.add_skill(profile=profile, actor=self.user, text=name)

        response = self.client.get(reverse("dashboard:index"))

        self.assertNotContains(response, "Add the skills")
        self.assertContains(response, "complete")


class OverviewLiveRegionTests(TestCase):
    """The three regions the home screen rendered empty.

    `OverviewView` passed `"activity": []` and `"matched": []` as literals with
    the comment "Matching is P13 and the activity feed is P10", and never
    passed `report` at all — so the home page showed an empty score ring beside
    the words "· analysed", an empty match list, and an empty activity feed,
    for a candidate with a scored CV, matching jobs and a live application.

    P10 and P13 both shipped. These assert the wiring, not the features.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        from apps.candidates.services import profile as profile_service
        from apps.candidates.services import skills as skill_service

        self.user = create_user(full_name="Priya Sharma")
        self.client.force_login(self.user)
        self.profile = profile_service.get_or_create_profile(user=self.user)
        for name in ("Python", "Django", "PostgreSQL"):
            skill_service.add_skill(profile=self.profile, actor=self.user, text=name)

    def test_the_cv_card_carries_the_score_rather_than_an_empty_ring(self):
        from apps.resumes.models import VersionReason
        from apps.resumes.services import analysis as analysis_service
        from apps.resumes.services import documents as document_service
        from apps.resumes.services import drafts as draft_service

        document = document_service.create_document(
            profile=self.profile, actor=self.user, name="Priya Sharma CV"
        )
        draft_service.set_contact(draft=document.draft, field="name", value="Priya Sharma")
        summary = content_rules.section_by_key(document.draft.content, "summary")
        draft_service.set_section_text(
            draft=document.draft,
            section_id=summary["id"],
            text="Backend engineer with six years on payment systems.",
        )
        document.refresh_from_db()
        version = version_service.finalize(
            document=document, actor=self.user, reason=VersionReason.SAVED
        )
        analysis_service.analyse_version(version=version, actor=self.user, use_ai=False)

        response = self.client.get(reverse("dashboard:index"))

        self.assertIsNotNone(response.context["report"])
        self.assertEqual(response.context["report"]["filename"], "Priya Sharma CV")
        self.assertGreater(response.context["report"]["score"], 0)

    def test_matched_roles_are_the_real_feed_rather_than_an_empty_list(self):
        from apps.companies.tests.factories import company_with_owner
        from apps.jobs.tests.factories import create_job, publish

        owner = create_user(email="owner-overview@example.com")
        company, _ = company_with_owner(owner)
        publish(create_job(company))

        response = self.client.get(reverse("dashboard:index"))

        self.assertTrue(response.context["matched"])
        # Shaped like every other job row, so the card renders a salary, a
        # location and a score rather than an empty frame.
        self.assertIn("match", response.context["matched"][0])
        self.assertIn("title", response.context["matched"][0])

    def test_the_strong_count_is_read_on_the_right_scale(self):
        """`Match.score` is 0-100, not 0-1.

        Compared against `0.7` every match in the feed counted as strong, and
        the heading read "15 above 70%" over a list whose best score was 60.
        """
        from apps.companies.tests.factories import company_with_owner
        from apps.jobs.tests.factories import create_job, publish
        from apps.matching.services import recommendations

        owner = create_user(email="owner-strong@example.com")
        company, _ = company_with_owner(owner)
        publish(create_job(company))

        expected = sum(
            1
            for item in recommendations.feed_for(self.profile, use_cache=False).items
            if item.match.score >= 70
        )
        response = self.client.get(reverse("dashboard:index"))

        self.assertEqual(response.context["strong_count"], expected)

    def test_activity_rows_carry_a_sentence_and_a_link_that_reverses(self):
        from apps.applications.models import ApplicationEvent
        from apps.companies.tests.factories import company_with_owner
        from apps.jobs.tests.factories import create_job, publish
        from apps.matching.tests.test_matching import application_for

        owner = create_user(email="owner-activity@example.com")
        company, _ = company_with_owner(owner)
        job = publish(create_job(company))
        application = application_for(self.profile, job)
        ApplicationEvent.objects.create(
            application=application,
            actor_kind="candidate",
            type="application.submitted",
            visible_to_candidate=True,
        )

        response = self.client.get(reverse("dashboard:index"))
        rows = response.context["activity"]

        self.assertTrue(rows)
        self.assertIn(job.title, rows[0]["text"])
        self.assertIn(str(application.public_id), rows[0]["url"])
        self.assertEqual(response.status_code, 200)


class SettingsScreenTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.client.force_login(self.user)

    def test_account_settings_save_the_locale_and_timezone(self):
        response = self.client.post(
            reverse("settings:account_act"),
            {"command": "save", "language": "en", "timezone": "Asia/Kolkata"},
        )

        self.assertEqual(UserPreferences.objects.get(user=self.user).timezone, "Asia/Kolkata")
        self.assertIn("Account details saved", response["HX-Trigger"])

    def test_an_unavailable_language_is_refused_rather_than_ignored(self):
        response = self.client.post(
            reverse("settings:account_act"),
            {"command": "save", "language": "ne", "timezone": "Asia/Kathmandu"},
        )

        self.assertContains(response, "isn&#x27;t available yet")
        self.assertEqual(UserPreferences.objects.get(user=self.user).locale, "en")

    def test_the_notification_grid_renders_every_group(self):
        response = self.client.get(reverse("settings:notifications"))

        for label in ("New job matches", "Weekly digest", "Product news"):
            self.assertContains(response, label)

    def test_switching_a_channel_off_is_stored(self):
        self.client.post(
            reverse("settings:notifications_act"),
            {"command": "channel", "event": "job_matches", "channel": "email", "on": "false"},
        )

        row = NotificationPreference.objects.get(user=self.user, event_key="job_matches")
        self.assertFalse(row.email)

    def test_switching_everything_off_silences_every_group(self):
        self.client.post(reverse("settings:notifications_act"), {"command": "all_off"})

        rows = NotificationPreference.objects.filter(user=self.user)
        self.assertEqual(len(rows), 7)  # six groups plus "Companies you follow" (FOL-03)
        self.assertTrue(all(row.is_silent for row in rows))

    def test_a_muted_group_is_dropped_from_the_feed_not_hidden(self):
        # "Turn this off" must not mean "keep sending it, quietly".
        # Reads real rows now (P10); the session-backed feed is gone.
        from apps.notifications.models import Notification
        from apps.notifications.services import feed as feed_service

        Notification.objects.create(
            recipient=self.user,
            kind="application.submitted",
            event_key="application_updates",
            title="New application",
        )
        before = feed_service.unread_count(self.user)

        self.client.post(reverse("settings:notifications_act"), {"command": "all_off"})

        self.assertGreater(before, 0)
        self.assertEqual(feed_service.unread_count(self.user), 0)

    def test_privacy_visibility_writes_to_the_profile(self):
        self.client.post(
            reverse("settings:privacy_act"),
            {"command": "visibility", "value": Visibility.NOBODY},
        )

        self.assertEqual(CandidateProfile.objects.get(user=self.user).visibility, Visibility.NOBODY)

    def test_indexing_is_disabled_with_a_reason_while_private(self):
        response = self.client.get(reverse("settings:privacy"))

        self.assertContains(response, "Available once your profile is visible")

    def test_a_privacy_switch_on_the_profile_toggles(self):
        self.client.post(
            reverse("settings:privacy_act"), {"command": "toggle", "key": "show_salary"}
        )

        self.assertTrue(CandidateProfile.objects.get(user=self.user).show_salary)

    def test_a_privacy_switch_on_the_account_toggles(self):
        self.client.post(
            reverse("settings:privacy_act"), {"command": "toggle", "key": "share_analytics"}
        )

        self.assertFalse(UserPreferences.objects.get(user=self.user).share_analytics)

    def test_appearance_changes_report_the_classes_for_the_document(self):
        response = self.client.post(
            reverse("settings:appearance_act"), {"command": "toggle", "key": "larger_text"}
        )

        triggers = json.loads(response["HX-Trigger"])
        self.assertIn("prefs-larger-text", triggers["prefs:changed"]["classes"])
        self.assertTrue(UserPreferences.objects.get(user=self.user).larger_text)

    def test_the_theme_is_stored_and_rendered_before_the_first_paint(self):
        self.client.post(reverse("settings:appearance_act"), {"command": "theme", "value": "dark"})

        response = self.client.get(reverse("settings:appearance"))
        self.assertContains(response, 'data-theme-source="dark"')

    def test_an_unknown_theme_is_refused(self):
        response = self.client.post(
            reverse("settings:appearance_act"), {"command": "theme", "value": "neon"}
        )

        self.assertContains(response, "isn&#x27;t a theme")


class BlockedCompaniesScreenTests(TestCase):
    """Candidates could block a company but never see or lift the block (Talent item 4)."""

    def setUp(self):
        from apps.candidates.services import blocking
        from apps.candidates.tests.factories import create_profile
        from apps.companies.tests.factories import create_company

        self.profile = create_profile()
        self.user = self.profile.user
        self.company = create_company(name="Himal Blocked Traders")
        self.record = blocking.block_company(
            profile=self.profile, actor=self.user, company=self.company
        )
        self.client.force_login(self.user)

    def test_the_privacy_screen_lists_the_block_with_a_named_unblock_button(self):
        response = self.client.get(reverse("settings:privacy"))

        self.assertContains(response, "Companies you've blocked")
        self.assertContains(response, "Himal Blocked Traders")
        self.assertContains(response, 'aria-label="Unblock Himal Blocked Traders"')

    def test_unblocking_removes_the_block_and_is_audited(self):
        from apps.audit.models import AuditLog
        from apps.candidates.models import BlockedCompany

        response = self.client.post(
            reverse("settings:privacy_act"), {"command": "unblock", "block_id": self.record.pk}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Himal Blocked Traders is unblocked")
        self.assertContains(response, "You haven't blocked any companies.")
        self.assertFalse(BlockedCompany.objects.filter(pk=self.record.pk).exists())
        self.assertTrue(AuditLog.objects.filter(verb="company.unblocked").exists())

    def test_another_candidate_cannot_lift_my_block(self):
        from apps.candidates.models import BlockedCompany
        from apps.candidates.tests.factories import create_profile

        self.client.force_login(create_profile().user)
        for value in (self.record.pk, "not-a-number", ""):
            with self.subTest(block_id=value):
                response = self.client.post(
                    reverse("settings:privacy_act"), {"command": "unblock", "block_id": value}
                )
                self.assertContains(response, "not on your blocked list")
        self.assertTrue(BlockedCompany.objects.filter(pk=self.record.pk).exists())

    def test_the_employers_option_describes_the_grants_that_survive(self):
        # Decision D-06: only applications survive narrowing, and the copy says so.
        response = self.client.get(reverse("settings:privacy"))

        self.assertContains(response, "Only companies you&#x27;ve applied to can open it")
        self.assertContains(response, "saved you to a talent pool or invited you lose access")
        self.assertContains(response, "Companies you applied to keep your application")


class DataScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.user = create_user()
        self.client.force_login(self.user)

    def test_the_export_is_a_file_of_the_real_record(self):
        from apps.candidates.services import profile as profile_service
        from apps.candidates.services import skills as skill_service

        profile = profile_service.get_or_create_profile(user=self.user)
        profile_service.update_basics(profile=profile, actor=self.user, headline="Engineer")
        skill_service.add_skill(profile=profile, actor=self.user, text="Python")

        response = self.client.get(reverse("settings:export"))
        payload = json.loads(response.content)

        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(payload["profile"]["headline"], "Engineer")
        self.assertEqual(payload["skills"][0]["skill"], "Python")

    def test_erasure_needs_the_word(self):
        response = self.client.post(
            reverse("settings:data_act"), {"command": "delete", "confirm": "ok"}
        )

        self.assertContains(response, "Type DELETE to confirm")

    def test_erasure_empties_the_profile_and_says_the_account_survives(self):
        from apps.candidates.services import profile as profile_service
        from apps.candidates.services import skills as skill_service

        profile = profile_service.get_or_create_profile(user=self.user)
        skill_service.add_skill(profile=profile, actor=self.user, text="Python")

        response = self.client.post(
            reverse("settings:data_act"), {"command": "delete", "confirm": "delete"}
        )

        self.assertEqual(CandidateProfile.objects.get(user=self.user).skills.count(), 0)
        self.assertIn("account is still open", response["HX-Trigger"])


def _request_for(user):
    """A request the session-backed notification feed can read."""
    from django.contrib.sessions.backends.db import SessionStore
    from django.test import RequestFactory

    request = RequestFactory().get("/notifications/")
    request.user = user
    request.session = SessionStore()
    return request
