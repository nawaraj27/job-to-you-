"""
Curation through Django admin (ADR-A5).

Staff maintain this taxonomy by hand, so the admin is not decoration: it is the
only interface where merging and deprecation actually happen. These tests cover
the two things that would hurt if they broke — a curation action that silently
does nothing, and a delete button that quietly discards referenced rows.
"""

from __future__ import annotations

from django.contrib.admin.sites import site
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_staff
from apps.taxonomy.admin import SkillAdmin
from apps.taxonomy.models import (
    ImportRun,
    Skill,
    SkillAlias,
    SkillStatus,
    TermSource,
    TermStatus,
    UnresolvedTerm,
)
from apps.taxonomy.services import terms as term_service


class CurationActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)
        cls.curator = create_staff(is_superuser=True)

    def setUp(self):
        self.client.force_login(self.curator)
        self.url = reverse("admin:taxonomy_skill_changelist")

    def test_merging_asks_which_skill_survives_before_doing_anything(self):
        duplicate = Skill.objects.create(slug="postgre-sql", name="Postgre SQL")
        target = Skill.objects.get(slug="postgresql")

        response = self.client.post(
            self.url,
            {"action": "merge_skills", "_selected_action": [duplicate.pk, target.pk]},
        )

        self.assertContains(response, "Merge skills")
        duplicate.refresh_from_db()
        self.assertTrue(duplicate.is_active)

    def test_confirming_a_merge_folds_the_duplicates_in(self):
        duplicate = Skill.objects.create(slug="postgre-sql", name="Postgre SQL")
        target = Skill.objects.get(slug="postgresql")

        self.client.post(
            self.url,
            {
                "action": "merge_skills",
                "_selected_action": [duplicate.pk, target.pk],
                "confirmed": "1",
                "target": target.pk,
            },
        )
        duplicate.refresh_from_db()

        self.assertEqual(duplicate.status, SkillStatus.MERGED)
        self.assertEqual(duplicate.replaced_by_id, target.pk)

    def test_merging_one_skill_asks_for_a_second(self):
        target = Skill.objects.get(slug="postgresql")

        response = self.client.post(
            self.url, {"action": "merge_skills", "_selected_action": [target.pk]}, follow=True
        )

        self.assertContains(response, "Select the duplicates")

    def test_deprecating_requires_a_reason(self):
        skill = Skill.objects.get(slug="selenium")

        response = self.client.post(
            self.url,
            {
                "action": "deprecate_skills",
                "_selected_action": [skill.pk],
                "confirmed": "1",
                "reason": "",
            },
        )
        skill.refresh_from_db()

        self.assertContains(response, "Deprecate skills")
        self.assertTrue(skill.is_active)

    def test_deprecating_with_a_replacement_forwards_resolution(self):
        skill = Skill.objects.get(slug="selenium")

        self.client.post(
            self.url,
            {
                "action": "deprecate_skills",
                "_selected_action": [skill.pk],
                "confirmed": "1",
                "reason": "Standardised on Playwright.",
                "replacement_slug": "playwright",
            },
        )
        skill.refresh_from_db()

        self.assertEqual(skill.status, SkillStatus.DEPRECATED)
        self.assertEqual(skill.replaced_by.slug, "playwright")

    def test_an_unknown_replacement_slug_is_rejected_by_the_form(self):
        skill = Skill.objects.get(slug="selenium")

        response = self.client.post(
            self.url,
            {
                "action": "deprecate_skills",
                "_selected_action": [skill.pk],
                "confirmed": "1",
                "reason": "Obsolete.",
                "replacement_slug": "no-such-skill",
            },
        )
        skill.refresh_from_db()

        self.assertContains(response, "No skill with slug")
        self.assertTrue(skill.is_active)

    def test_reinstating_returns_a_skill_to_service(self):
        skill = Skill.objects.get(slug="angularjs")

        self.client.post(
            self.url, {"action": "reinstate_skills", "_selected_action": [skill.pk]}, follow=True
        )
        skill.refresh_from_db()

        self.assertTrue(skill.is_active)

    def test_reinstating_a_merged_skill_reports_the_refusal(self):
        duplicate = Skill.objects.create(slug="postgre-sql", name="Postgre SQL")
        self.client.post(
            self.url,
            {
                "action": "merge_skills",
                "_selected_action": [duplicate.pk, Skill.objects.get(slug="postgresql").pk],
                "confirmed": "1",
                "target": Skill.objects.get(slug="postgresql").pk,
            },
        )

        response = self.client.post(
            self.url,
            {"action": "reinstate_skills", "_selected_action": [duplicate.pk]},
            follow=True,
        )

        self.assertContains(response, "Only a deprecated skill can be reinstated")


class DeletionProtectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)
        cls.curator = create_staff(is_superuser=True)

    def setUp(self):
        self.admin = SkillAdmin(Skill, site)
        self.request = _request_from(self.curator)

    def test_a_referenced_skill_cannot_be_deleted(self):
        # Python has aliases and relations. Deleting it discards them and
        # orphans everything that claimed the skill.
        python = Skill.objects.get(slug="python")

        self.assertFalse(self.admin.has_delete_permission(self.request, python))

    def test_a_skill_nothing_references_can_still_be_deleted(self):
        stray = Skill.objects.create(slug="typo-skill", name="Typo skill")

        self.assertTrue(self.admin.has_delete_permission(self.request, stray))

    def test_a_skill_something_forwards_to_is_protected(self):
        angular = Skill.objects.get(slug="angular")

        self.assertFalse(self.admin.has_delete_permission(self.request, angular))

    def test_bulk_delete_is_not_offered(self):
        # It cannot explain which row is protected or why.
        self.assertNotIn("delete_selected", self.admin.get_actions(self.request))


class AdminDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)
        cls.curator = create_staff(is_superuser=True)

    def setUp(self):
        self.client.force_login(self.curator)

    def test_the_change_form_shows_the_relationship_map(self):
        django_skill = Skill.objects.get(slug="django")

        response = self.client.get(reverse("admin:taxonomy_skill_change", args=[django_skill.pk]))

        self.assertContains(response, "Python")
        self.assertContains(response, "Implies")

    def test_an_alias_saved_in_admin_gets_its_match_column(self):
        python = Skill.objects.get(slug="python")

        self.client.post(
            reverse("admin:taxonomy_skillalias_add"),
            {"skill": python.pk, "alias": "  CPython  ", "locale": "en"},
        )

        self.assertEqual(SkillAlias.objects.get(alias="CPython").normalised, "cpython")

    def test_import_runs_are_read_only(self):
        run = ImportRun.objects.create(source="esco")
        admin_instance = site._registry[ImportRun]

        request = _request_from(self.curator)
        self.assertFalse(admin_instance.has_add_permission(request))
        self.assertFalse(admin_instance.has_change_permission(request, run))

    def test_an_unfinished_run_says_how_to_resume_it(self):
        run = ImportRun.objects.create(source="esco", last_key="S042")

        response = self.client.get(reverse("admin:taxonomy_importrun_change", args=[run.pk]))

        self.assertContains(response, "S042")


def _request_from(user):
    from django.test import RequestFactory

    request = RequestFactory().get("/admin/")
    request.user = user
    return request


class TermQueueAdminTests(TestCase):
    """The curation queue for unknown terms (ADR-A46)."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)
        cls.curator = create_staff(is_superuser=True)

    def setUp(self):
        self.client.force_login(self.curator)
        self.url = reverse("admin:taxonomy_unresolvedterm_changelist")
        self.term = term_service.record("Postgres DB", source=TermSource.PROFILE)

    def test_the_queue_lists_terms_by_demand(self):
        for _ in range(3):
            term_service.record("Kubeflow", source=TermSource.PROFILE)

        response = self.client.get(self.url)

        self.assertContains(response, "Kubeflow")
        self.assertContains(response, "Postgres DB")

    def test_resolving_asks_which_skill_before_doing_anything(self):
        response = self.client.post(
            self.url, {"action": "resolve_terms", "_selected_action": [self.term.pk]}
        )

        self.assertContains(response, "Resolve unknown terms")
        self.term.refresh_from_db()
        self.assertEqual(self.term.status, TermStatus.PENDING)

    def test_confirming_maps_the_term_and_teaches_resolution(self):
        self.client.post(
            self.url,
            {
                "action": "resolve_terms",
                "_selected_action": [self.term.pk],
                "confirmed": "1",
                "skill": Skill.objects.get(slug="postgresql").pk,
                "add_alias": "on",
                "note": "",
            },
        )
        self.term.refresh_from_db()

        self.assertEqual(self.term.status, TermStatus.RESOLVED)
        self.assertTrue(SkillAlias.objects.filter(normalised="postgres db").exists())

    def test_rejecting_takes_it_out_of_the_queue_without_deleting_it(self):
        self.client.post(
            self.url,
            {"action": "reject_terms", "_selected_action": [self.term.pk]},
            follow=True,
        )
        self.term.refresh_from_db()

        self.assertEqual(self.term.status, TermStatus.REJECTED)

    def test_reopening_a_decided_term(self):
        term_service.reject(term=self.term, actor=self.curator)

        self.client.post(
            self.url,
            {"action": "reopen_terms", "_selected_action": [self.term.pk]},
            follow=True,
        )
        self.term.refresh_from_db()

        self.assertEqual(self.term.status, TermStatus.PENDING)

    def test_a_refused_resolution_is_reported_rather_than_silent(self):
        term_service.resolve(
            term=self.term, skill=Skill.objects.get(slug="postgresql"), actor=self.curator
        )

        response = self.client.post(
            self.url,
            {
                "action": "resolve_terms",
                "_selected_action": [self.term.pk],
                "confirmed": "1",
                "skill": Skill.objects.get(slug="mysql").pk,
                "note": "",
            },
            follow=True,
        )

        self.assertContains(response, "has already been resolved")

    def test_terms_cannot_be_added_by_hand(self):
        admin_instance = site._registry[UnresolvedTerm]

        self.assertFalse(admin_instance.has_add_permission(_request_from(self.curator)))
