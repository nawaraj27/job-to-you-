"""
The CV template and CV improvement screens, through their real routes.

Regression tests for the defects reproduced in the browser on 2026-09-27:

  * "Use this template" and "Start a CV from this example" are ordinary form
    posts to endpoints that answer htmx with a fragment. The browser was given
    the fragment alone — no stylesheet, no shell, an SVG icon the size of the
    window over plain text ("Not included in your plan / Upgrade plan").
  * The gallery, a template's page and the comparison drew the candidate's
    own latest draft instead of the sample CV, so editing a CV changed every
    "template preview".
  * A Pro template offered "Use this template" to a free candidate and
    refused it after the click.
  * The plan page and pricing page were unfinished: a signed-in candidate's
    "Start with Pro" went to sign-up; the free plan ticked "Downloads are part
    of Pro" as a feature; the footer grid was invalid CSS.
  * The analysis ended with a link to the editor; guided and assisted screens
    never showed what the analysis found; an assistant answer ran inside the
    request with no progress and nothing that survived a refresh.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.ai.services import prompts
from apps.billing.tests import factories as billing_factories
from apps.candidates.services import profile as profile_service
from apps.resumes import content as content_rules
from apps.resumes import examples, presentation
from apps.resumes.models import (
    ImprovementJob,
    ImprovementJobState,
    ResumeDocument,
    VersionReason,
)
from apps.resumes.services import analysis as analysis_service
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import versions as version_service

HX = {"HTTP_HX_REQUEST": "true"}
#: What a browser sends with an ordinary form post (a page navigation).
NAV = {"HTTP_ACCEPT": "text/html,application/xhtml+xml", "HTTP_SEC_FETCH_DEST": "document"}


def _is_full_page(response) -> bool:
    """A page with the site's shell, not a bare region."""
    html = response.content.decode()
    return "<html" in html and "css/dist/site" in html and "<nav" in html


class ScreenFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_taxonomy", quiet_summary=True)
        prompts.load_all()

    def setUp(self):
        self.user = create_user(full_name="Arjun Test")
        self.client.force_login(self.user)
        self.profile = profile_service.get_or_create_profile(user=self.user)

    def make_cv(
        self, *, profile=None, actor=None, summary="Product designer, three years."
    ) -> ResumeDocument:
        profile = profile or self.profile
        actor = actor or self.user
        document = document_service.create_document(profile=profile, actor=actor, name="My CV")
        content = document.draft.content
        content["contact"].update({"name": actor.full_name or "Test", "email": actor.email})
        content_rules.section_by_key(content, "summary")["text"] = summary
        experience = content_rules.section_by_key(content, "experience")
        experience["entries"] = [
            content_rules.new_entry(
                title="Product Designer",
                org="Trek Planner",
                dates="2022 - Present",
                bullets=["Responsible for designing booking screens."],
            ),
            content_rules.new_entry(title="Intern", org="Studio", dates="2021 - 2022", bullets=[]),
        ]
        document.draft.content = content
        document.draft.save()
        return document


# --- templates: full pages, never fragments --------------------------------------------


class TemplatePageTests(ScreenFixture):
    def test_gallery_lists_free_and_pro_templates_with_their_tier(self):
        response = self.client.get(reverse("resume:templates"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(_is_full_page(response))
        self.assertContains(response, "Meridian")  # free
        self.assertContains(response, "Vertex")  # pro
        self.assertContains(response, 'class="badge badge-accent shrink-0"')
        self.assertContains(response, 'class="badge badge-neutral shrink-0"')

    def test_free_template_preview_is_a_complete_page(self):
        response = self.client.get(reverse("resume:template_detail", kwargs={"slug": "meridian"}))

        self.assertTrue(_is_full_page(response))
        self.assertContains(response, "Example CV")
        # Each template is shown with its own fictional persona.
        from apps.resumes.example_personas import for_template

        self.assertContains(response, for_template("meridian")["contact"]["name"])
        self.assertNotContains(response, "Part of Pro")

    def test_pro_template_preview_for_a_free_candidate_offers_the_upgrade_up_front(self):
        self.make_cv()
        response = self.client.get(reverse("resume:template_detail", kwargs={"slug": "vertex"}))

        self.assertTrue(_is_full_page(response))
        self.assertContains(response, "Part of Pro")
        self.assertContains(response, reverse("dashboard:plan"))
        # No apply form that would only be refused after the click.
        self.assertNotContains(response, 'value="apply"')

    def test_pro_template_on_pro_is_usable(self):
        billing_factories.put_on_pro(self.profile)
        self.make_cv()
        response = self.client.get(reverse("resume:template_detail", kwargs={"slug": "vertex"}))

        self.assertNotContains(response, "Part of Pro")
        self.assertContains(response, 'value="apply"')

    def test_using_a_template_from_its_page_opens_the_editor_not_a_fragment(self):
        document = self.make_cv()
        response = self.client.post(
            reverse("resume:template_act"),
            {"command": "apply", "slug": "atlas", "document": str(document.public_id)},
            HTTP_REFERER="http://testserver"
            + reverse("resume:template_detail", kwargs={"slug": "atlas"}),
            **NAV,
        )

        self.assertRedirects(
            response,
            reverse("resume:editor", kwargs={"document_id": str(document.public_id)}),
            fetch_redirect_response=False,
        )
        document.refresh_from_db()
        self.assertEqual(document.template.slug, "atlas")
        editor = self.client.get(response["Location"])
        self.assertTrue(_is_full_page(editor))
        self.assertContains(editor, "Atlas applied to My CV")

    def test_the_editor_is_a_complete_page_with_its_preview(self):
        document = self.make_cv()
        response = self.client.get(
            reverse("resume:editor", kwargs={"document_id": str(document.public_id)})
        )

        self.assertTrue(_is_full_page(response))
        self.assertContains(response, 'id="editor-live" class="@container"')
        self.assertContains(response, "Preview ·")

    def test_a_refused_pro_template_by_direct_post_is_a_full_restriction_page(self):
        document = self.make_cv()
        response = self.client.post(
            reverse("resume:template_act"),
            {"command": "apply", "slug": "vertex", "document": str(document.public_id)},
            **NAV,
        )

        self.assertEqual(response.status_code, 402)
        self.assertTrue(_is_full_page(response))
        self.assertContains(response, "Not included in your plan", status_code=402)
        self.assertContains(response, reverse("dashboard:plan"), status_code=402)
        document.refresh_from_db()
        self.assertNotEqual(document.template.slug, "vertex")

    def test_starting_from_a_pro_example_without_pro_is_a_full_page(self):
        response = self.client.post(
            reverse("resume:builder"),
            {"template": "vertex", "start": "example", "mode": "editor"},
            **NAV,
        )

        self.assertEqual(response.status_code, 402)
        self.assertTrue(_is_full_page(response))
        self.assertFalse(ResumeDocument.objects.filter(profile=self.profile).exists())

    def test_htmx_still_gets_the_region(self):
        response = self.client.post(
            reverse("resume:template_act"), {"command": "favourite", "slug": "atlas"}, **HX
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<html", response.content.decode())
        self.assertContains(response, 'id="template-results"')

    def test_the_upgrade_route_is_the_plan_page(self):
        response = self.client.get(reverse("dashboard:plan"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Move to Pro")


# --- templates: sample data is isolated --------------------------------------------------


class SampleIsolationTests(ScreenFixture):
    def test_the_gallery_starts_on_the_sample_and_stays_there_after_an_edit(self):
        document = self.make_cv(summary="My very own summary line.")
        before = self.client.get(reverse("resume:templates")).content.decode()

        summary = content_rules.section_by_key(document.draft.content, "summary")
        draft_service.set_section_text(
            draft=document.draft,
            section_id=summary["id"],
            text="Edited after visiting the gallery.",
        )
        after = self.client.get(reverse("resume:templates")).content.decode()

        for page in (before, after):
            self.assertIn("Priya Sharma", page)
            self.assertNotIn("My very own summary line", page)
            self.assertNotIn("Edited after visiting the gallery", page)

    def test_a_template_page_shows_the_sample_by_default_even_with_a_cv(self):
        self.make_cv(summary="Unmistakably mine.")
        response = self.client.get(reverse("resume:template_detail", kwargs={"slug": "atlas"}))

        self.assertContains(response, "Priya Sharma")
        self.assertNotContains(response, "Unmistakably mine.")
        self.assertContains(response, "Preview with my CV")

    def test_asking_for_my_cv_previews_my_cv_and_says_so(self):
        document = self.make_cv(summary="Unmistakably mine.")
        url = (
            reverse("resume:template_detail", kwargs={"slug": "atlas"})
            + f"?cv={document.public_id}"
        )
        response = self.client.get(url)

        self.assertContains(response, "Unmistakably mine.")
        self.assertContains(response, "Your CV")
        self.assertNotContains(response, "Senior Backend Engineer")

    def test_another_candidates_cv_cannot_be_previewed(self):
        other = create_user(full_name="Someone Else")
        other_doc = self.make_cv(
            profile=profile_service.get_or_create_profile(user=other),
            actor=other,
            summary="Not yours.",
        )
        url = (
            reverse("resume:template_detail", kwargs={"slug": "atlas"})
            + f"?cv={other_doc.public_id}"
        )

        self.assertEqual(self.client.get(url).status_code, 404)

    def test_one_candidates_cv_never_reaches_anothers_gallery(self):
        self.make_cv(summary="Candidate A private words.")
        other = create_user(full_name="Candidate B")
        self.client.force_login(other)

        for name, kwargs in (
            ("resume:templates", {}),
            ("resume:template_detail", {"slug": "atlas"}),
        ):
            with self.subTest(page=name):
                self.assertNotContains(
                    self.client.get(reverse(name, kwargs=kwargs)), "Candidate A private words."
                )

    def test_the_sample_constant_is_never_mutated_by_rendering_or_starting_from_it(self):
        snapshot = json.dumps(examples.EXAMPLE_CONTENT, sort_keys=True)

        self.client.get(reverse("resume:templates"))
        self.client.get(reverse("resume:template_detail", kwargs={"slug": "meridian"}))
        self.client.post(
            reverse("resume:builder"),
            {"template": "meridian", "start": "example", "mode": "editor"},
        )
        document = ResumeDocument.objects.get(profile=self.profile)
        summary = content_rules.section_by_key(document.draft.content, "summary")
        draft_service.set_section_text(
            draft=document.draft, section_id=summary["id"], text="Mine now."
        )

        self.assertEqual(json.dumps(examples.EXAMPLE_CONTENT, sort_keys=True), snapshot)
        self.assertNotContains(self.client.get(reverse("resume:templates")), "Mine now.")

    def test_different_templates_never_share_a_preview_object(self):
        from apps.resumes.models import ResumeTemplate

        rows = presentation.previews(ResumeTemplate.objects.filter(slug__in=("meridian", "atlas")))
        first, second = (presentation.sample_content(), presentation.sample_content())

        self.assertIsNot(first, second)
        self.assertIsNot(first, examples.EXAMPLE_CONTENT)
        self.assertEqual(len(rows), 2)


# --- analysis → guided → assisted ----------------------------------------------------------


class ImprovementScreenTests(ScreenFixture):
    def setUp(self):
        super().setUp()
        self.document = self.make_cv()
        version = version_service.finalize(
            document=self.document, actor=self.user, reason=VersionReason.SAVED
        )
        self.analysis = analysis_service.analyse_version(
            version=version, actor=self.user, use_ai=False
        )
        self.doc_id = str(self.document.public_id)

    def test_the_report_hands_off_to_guided_and_assisted(self):
        response = self.client.get(
            reverse("resume:analysis") + f"?report={self.analysis.public_id}"
        )

        self.assertContains(response, "Improve this CV")
        self.assertContains(response, reverse("resume:guided", kwargs={"document_id": self.doc_id}))
        self.assertContains(
            response, reverse("resume:assistant", kwargs={"document_id": self.doc_id})
        )

    def test_the_analysis_form_says_no_target_is_needed(self):
        response = self.client.get(reverse("resume:analysis"))
        self.assertContains(response, "No target job is needed")

    def test_guided_shows_what_the_analysis_found_with_where_and_what_to_add(self):
        response = self.client.get(reverse("resume:guided", kwargs={"document_id": self.doc_id}))

        self.assertContains(response, "What we found")
        self.assertContains(response, "Intern” says nothing about what you did")
        self.assertContains(response, "Experience — Intern at Studio")
        self.assertContains(response, "What to add")
        self.assertContains(response, "Why it matters")
        self.assertContains(response, 'value="improve_answer"')

    def test_assisted_shows_the_plan_before_anything_is_drafted(self):
        response = self.client.get(reverse("resume:assistant", kwargs={"document_id": self.doc_id}))

        self.assertContains(response, "What we are going to change")
        self.assertContains(response, "Safe rewording")
        self.assertContains(response, "Needs your input")
        self.assertContains(response, "Prepare these improvements")
        self.assertFalse(self.document.draft.suggestions.exists())

    def test_sending_an_answer_queues_it_and_tells_every_panel(self):
        act = reverse("resume:builder_act", kwargs={"document_id": self.doc_id})
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.post(
                act,
                {"command": "turn", "text": "I designed the booking flow.", "token": "t1"},
                **HX,
            )

        self.assertEqual(response.status_code, 200)
        events = json.loads(response["HX-Trigger"])
        self.assertTrue(events["improve:queued"])
        self.assertTrue(events["cv:changed"])
        self.assertEqual(events["builder:announce"], "Answer received. Working on it")
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(ImprovementJob.objects.filter(document=self.document).count(), 1)

        # The same form again: no second job.
        with self.captureOnCommitCallbacks(execute=False) as again:
            self.client.post(
                act,
                {"command": "turn", "text": "I designed the booking flow.", "token": "t1"},
                **HX,
            )
        self.assertEqual(len(again), 0)
        self.assertEqual(ImprovementJob.objects.filter(document=self.document).count(), 1)

    def test_progress_is_read_from_the_row_so_it_survives_a_refresh(self):
        act = reverse("resume:builder_act", kwargs={"document_id": self.doc_id})
        with self.captureOnCommitCallbacks(execute=False):
            self.client.post(act, {"command": "turn", "text": "I designed the booking flow."}, **HX)
        job = ImprovementJob.objects.get(document=self.document)
        ImprovementJob.objects.filter(pk=job.pk).update(state="waiting_ai", stage="drafting")

        for _ in range(2):  # a refresh, then a return visit
            page = self.client.get(reverse("resume:assistant", kwargs={"document_id": self.doc_id}))
            self.assertContains(page, "Working on your answer")
            self.assertContains(page, "Waiting for the AI")
            self.assertContains(page, f"{settings.SITE_NAME} is working on your answer")
            self.assertContains(page, 'hx-trigger="every 2s"')
            # Locked while it runs, and says why.
            self.assertContains(page, "Working on your last answer")

    def test_the_poll_says_done_once_the_watched_job_finishes(self):
        act = reverse("resume:builder_act", kwargs={"document_id": self.doc_id})
        with self.captureOnCommitCallbacks(execute=False):
            self.client.post(act, {"command": "turn", "text": "I designed the booking flow."}, **HX)
        job = ImprovementJob.objects.get(document=self.document)
        url = reverse("resume:improvement_job", kwargs={"document_id": self.doc_id})

        running = self.client.get(url + f"?watching={job.public_id}", **HX)
        self.assertNotIn("HX-Trigger", running)

        ImprovementJob.objects.filter(pk=job.pk).update(
            state=ImprovementJobState.COMPLETED, result={"suggestions": 2, "reply": "ok"}
        )
        from django.utils import timezone

        ImprovementJob.objects.filter(pk=job.pk).update(finished_at=timezone.now())
        done = self.client.get(url + f"?watching={job.public_id}", **HX)
        self.assertEqual(json.loads(done["HX-Trigger"]), {"improve:done": str(job.public_id)})
        self.assertContains(done, "2 suggested changes drafted")

    def test_a_failed_job_offers_retry_without_internals(self):
        act = reverse("resume:builder_act", kwargs={"document_id": self.doc_id})
        with self.captureOnCommitCallbacks(execute=False):
            self.client.post(act, {"command": "turn", "text": "I designed the booking flow."}, **HX)
        job = ImprovementJob.objects.get(document=self.document)
        ImprovementJob.objects.filter(pk=job.pk).update(
            state=ImprovementJobState.FAILED, result={"error": "The AI could not be reached."}
        )

        page = self.client.get(reverse("resume:assistant", kwargs={"document_id": self.doc_id}))
        self.assertContains(page, "That did not finish")
        self.assertContains(page, "Try again")
        self.assertContains(page, '"command": "job_retry"')

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.client.post(act, {"command": "job_retry", "job": str(job.public_id)}, **HX)
        job.refresh_from_db()
        self.assertEqual(job.state, ImprovementJobState.QUEUED)
        self.assertEqual(len(callbacks), 1)

    def test_a_guided_contact_answer_fills_only_an_empty_field(self):
        act = reverse("resume:builder_act", kwargs={"document_id": self.doc_id})
        key = "contact_incomplete:contact.phone"

        self.client.post(
            act, {"command": "improve_contact", "key": key, "value": "+977 9800000000"}, **HX
        )
        self.document.draft.refresh_from_db()
        self.assertEqual(self.document.draft.content["contact"]["phone"], "+977 9800000000")

        # Asked again once it is filled: refused (the point is no longer on the
        # list), keeping its real status, and the number is kept.
        response = self.client.post(
            act, {"command": "improve_contact", "key": key, "value": "000"}, **HX
        )
        self.assertEqual(response.status_code, 409)
        self.document.draft.refresh_from_db()
        self.assertEqual(self.document.draft.content["contact"]["phone"], "+977 9800000000")

    def test_a_guided_answer_is_aimed_at_the_entry_the_finding_is_about(self):
        intern = content_rules.section_by_key(self.document.draft.content, "experience")["entries"][
            1
        ]
        act = reverse("resume:builder_act", kwargs={"document_id": self.doc_id})
        with self.captureOnCommitCallbacks(execute=False):
            self.client.post(
                act,
                {
                    "command": "improve_answer",
                    "key": f"thin_entry:entry.{intern['id']}.bullets",
                    "text": "I made icons.",
                },
                **HX,
            )

        job = ImprovementJob.objects.get(document=self.document)
        self.assertEqual(job.payload["focus_entry"], intern["id"])
        self.assertIn("Intern", job.payload["question"])

    def test_the_improvement_panel_endpoint_redraws_the_plan(self):
        url = reverse("resume:improvement_panel", kwargs={"document_id": self.doc_id})
        response = self.client.get(url + "?mode=assisted", **HX)

        self.assertContains(response, 'id="improvement-plan"')
        self.assertContains(response, "cv:changed from:body")

    def test_the_guided_and_assisted_screens_are_complete_pages(self):
        for name in ("resume:guided", "resume:assistant"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name, kwargs={"document_id": self.doc_id}))
                self.assertTrue(_is_full_page(response))
                self.assertContains(response, 'id="editor-live" class="@container"')

    def test_another_candidate_cannot_read_or_drive_this_cvs_jobs(self):
        other = create_user(full_name="Other")
        self.client.force_login(other)
        for name in (
            "resume:improvement_job",
            "resume:improvement_panel",
            "resume:transcript",
            "resume:editor_live",
        ):
            with self.subTest(route=name):
                self.assertEqual(
                    self.client.get(reverse(name, kwargs={"document_id": self.doc_id})).status_code,
                    404,
                )
        response = self.client.post(
            reverse("resume:builder_act", kwargs={"document_id": self.doc_id}),
            {"command": "turn", "text": "hijack"},
            **HX,
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(ImprovementJob.objects.exists())


# --- plans -----------------------------------------------------------------------------------


class PlanPageTests(ScreenFixture):
    def test_the_plan_page_compares_every_plan_and_marks_the_current_one(self):
        response = self.client.get(reverse("dashboard:plan"))

        self.assertTrue(_is_full_page(response))
        self.assertContains(response, "Compare plans")
        self.assertContains(response, "Your plan")
        self.assertContains(response, "Move to Pro")
        self.assertContains(
            response, "Not included: </span>Downloading your CV as PDF or Word", html=False
        )
        # How paying works is on the page before anybody presses a button.
        self.assertContains(response, "Prices are in Nepali rupees")
        self.assertContains(response, "starts when its first invoice is paid")

    def test_pricing_for_a_signed_in_candidate_points_at_their_plan(self):
        response = self.client.get(reverse("core:pricing"))

        self.assertContains(response, "Your current plan")
        self.assertContains(response, "Upgrade to Pro")
        self.assertNotContains(response, reverse("accounts:signup"))

    def test_pricing_for_a_visitor_offers_sign_up(self):
        self.client.logout()
        response = self.client.get(reverse("core:pricing"))

        self.assertContains(response, reverse("accounts:signup"))
        self.assertContains(response, "Start with Pro")

    def test_the_free_plan_does_not_tick_what_it_withholds(self):
        self.client.logout()
        html = self.client.get(reverse("core:pricing")).content.decode()
        self.assertNotIn("Downloads are part of Pro", html)
        self.assertIn("Not included: </span>Downloading your CV as PDF or Word", html)

    def test_the_footer_grid_is_valid_css(self):
        html = self.client.get(reverse("core:pricing")).content.decode()
        self.assertNotIn("repeat(auto-fit", html)
        footer = html[html.index("<footer") : html.index("</footer>")]
        self.assertIn("lg:grid-cols-[minmax(0,1.5fr)_repeat(4,minmax(0,1fr))]", footer)
        self.assertIn("grid-cols-2", footer)
