"""
The template experience end to end: example, edit, switch, keep.

The property under test is the one the architecture is built around and the
one a screen can quietly break — **content and presentation are separate**.
A CV is a draft; a template is how that draft is drawn. So the same document
must survive being drawn ten different ways, the worked example must be a
demonstration rather than a shadow copy of somebody's CV, and neither must be
able to overwrite the other.

These exercise the real routes: the gallery, the detail page, the builder's
own creation endpoint, the editor's template command and the exporter. A test
that called `set_template` directly would pass with the screens unwired.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.billing.tests import factories as billing_factories
from apps.candidates.services import profile as profile_service
from apps.resumes import content as content_rules
from apps.resumes import examples, presentation
from apps.resumes.models import BuilderMode, ResumeDocument, ResumeTemplate
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service

#: Every template the catalogue ships. Named rather than read from the table,
#: so deleting one from the seed fails a test instead of shrinking the loop.
CATALOGUE = (
    "meridian",
    "meridian-compact",
    "atlas",
    "atlas-warm",
    "vertex",
    "regent",
    "circuit",
    "plainspoke",
    "lyceum",
    "prism",
)

#: The example's own summary, written out here so the "a copy never mutates
#: the original" test compares against something other than the object it is
#: checking was not mutated.
EXAMPLE_SUMMARY = (
    "Backend engineer with six years on payment systems. Took a nightly "
    "settlement batch to near-real-time and cut reconciliation disputes "
    "by 40%. Comfortable owning a service end to end."
)


def _words(document) -> set[str]:
    """Every string a flattened document puts on the page, in no order.

    Includes `meta` — an entry's employer and dates live there rather than in
    `text`, and a comparison that skipped it would call two pages identical
    while one of them had lost every organisation name.
    """
    found: set[str] = set()
    for block in (*document.blocks, *document.sidebar):
        found.add(block.text)
        found.update(block.items)
        found.update(str(value) for value in block.meta.values())
    return {part for part in found if part}


def _text_of(document) -> str:
    return " | ".join(sorted(_words(document)))


class TemplateFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.user = create_user(full_name="Anjali Thapa")
        self.client.force_login(self.user)
        self.profile = profile_service.get_or_create_profile(user=self.user)
        # Pro, so the catalogue's Pro templates are reachable through the same
        # screens as the free ones; the entitlement gate itself is asserted in
        # apps/billing/tests.
        billing_factories.put_on_pro(self.profile)


class CatalogueTests(TemplateFixture):
    def test_the_gallery_still_lists_every_template_in_the_catalogue(self):
        response = self.client.get(reverse("resume:templates"))

        for slug in CATALOGUE:
            with self.subTest(slug=slug):
                self.assertContains(response, ResumeTemplate.objects.get(slug=slug).name)

    def test_every_template_renders_the_complete_example(self):
        """Not "renders": renders the whole example, in every structure.

        A sidebar or banded template that silently dropped the sections it
        could not place would still return a sheet with text on it.
        """
        for slug in CATALOGUE:
            template = ResumeTemplate.objects.get(slug=slug)
            drawn = presentation.paper(examples.EXAMPLE_CONTENT, template)
            text = _text_of(drawn["paper"])

            with self.subTest(slug=slug):
                self.assertIn("Priya Sharma", text)
                self.assertIn("Senior Backend Engineer", text)
                for expected in (
                    "Nexa Payments",  # experience
                    "Tribhuvan University",  # education
                    "Kubernetes",  # skills
                    "Nepali (native)",  # languages
                    "khata",  # projects
                    "Certified Kubernetes Application Developer",  # certifications
                ):
                    self.assertIn(expected, text)

    def test_the_example_covers_every_section_a_blank_cv_starts_with(self):
        blank = {section["key"] for section in content_rules.blank_document()["sections"]}
        example = {section["key"] for section in examples.EXAMPLE_CONTENT["sections"]}

        self.assertEqual(blank - example, set())

    def test_a_detail_page_renders_the_example_and_says_it_is_one(self):
        response = self.client.get(reverse("resume:template_detail", kwargs={"slug": "atlas"}))

        self.assertContains(response, "Priya Sharma")
        self.assertContains(response, "example CV")

    def test_a_comparison_of_the_example_says_it_is_one(self):
        for slug in ("meridian", "atlas"):
            self.client.post(reverse("resume:template_act"), {"command": "compare", "slug": slug})

        response = self.client.get(reverse("resume:template_compare"))

        self.assertContains(response, "The same example CV in each layout.")
        self.assertContains(response, "Priya Sharma", count=2)

    def test_a_candidate_with_a_cv_still_compares_the_example(self):
        """Sample data, not their draft (2026-09-27 repair).

        The comparison used to draw the candidate's own CV, so it changed every
        time they edited it. It compares layouts, and the layouts are shown with
        the one example everybody sees; their own CV is one click away on each
        template's page.
        """
        document = document_service.create_document(
            profile=self.profile, actor=self.user, name="Ops CV"
        )
        draft_service.set_contact(draft=document.draft, field="headline", value="Ops Lead Zeta")
        for slug in ("meridian", "atlas"):
            self.client.post(reverse("resume:template_act"), {"command": "compare", "slug": slug})

        response = self.client.get(reverse("resume:template_compare"))

        self.assertContains(response, "The same example CV in each layout.")
        self.assertContains(response, "Priya Sharma", count=2)
        self.assertNotContains(response, "Ops Lead Zeta")

    def test_the_detail_page_offers_a_way_to_try_another_template(self):
        response = self.client.get(reverse("resume:template_detail", kwargs={"slug": "atlas"}))

        self.assertContains(response, "Try another template")
        self.assertContains(response, reverse("resume:templates"))


class ExampleIsNotUserDataTests(TemplateFixture):
    def test_browsing_templates_creates_nothing(self):
        self.client.get(reverse("resume:templates"))
        self.client.get(reverse("resume:template_detail", kwargs={"slug": "prism"}))
        self.client.get(reverse("resume:template_compare"))

        self.assertFalse(ResumeDocument.objects.filter(profile=self.profile).exists())

    def test_the_gallery_shows_the_sample_not_the_candidates_cv_and_touches_neither(self):
        """The gallery is a picture of the templates (2026-09-27 repair).

        It used to draw the candidate's latest draft once they had one, so the
        "fixed" template previews changed whenever they edited their CV.
        """
        document = document_service.create_document(
            profile=self.profile, actor=self.user, name="Ops CV"
        )
        draft_service.set_contact(draft=document.draft, field="headline", value="Ops Lead Zeta")
        document.draft.refresh_from_db()
        before = dict(document.draft.content)

        response = self.client.get(reverse("resume:templates"))
        document.draft.refresh_from_db()

        self.assertContains(response, "Priya Sharma")
        self.assertNotContains(response, "Ops Lead Zeta")
        self.assertEqual(document.draft.content, before)

    def test_a_copy_of_the_example_never_mutates_the_example(self):
        first = examples.example_content(name="Anjali Thapa", email="anjali@example.test")
        first["contact"]["headline"] = "Changed"
        first["sections"][0]["text"] = "Changed"

        second = examples.example_content()

        self.assertEqual(examples.EXAMPLE_CONTENT["contact"]["name"], "Priya Sharma")
        self.assertEqual(second["sections"][0]["text"], EXAMPLE_SUMMARY)

    def test_each_copy_of_the_example_gets_its_own_ids(self):
        first = examples.example_content()
        second = examples.example_content()

        first_ids = {section["id"] for section in first["sections"]}
        second_ids = {section["id"] for section in second["sections"]}

        self.assertEqual(len(first_ids), len(first["sections"]))
        self.assertEqual(first_ids & second_ids, set())

    def test_starting_from_the_example_writes_the_candidates_own_name(self):
        self.client.post(
            reverse("resume:builder"),
            {
                "name": "First CV",
                "template": "atlas",
                "mode": BuilderMode.EDITOR,
                "start": "example",
            },
        )

        document = ResumeDocument.objects.get(profile=self.profile)
        self.assertEqual(document.draft.content["contact"]["name"], "Anjali Thapa")
        self.assertEqual(document.draft.content["contact"]["email"], self.user.email)

    def test_starting_from_the_example_is_a_real_editable_document(self):
        response = self.client.post(
            reverse("resume:builder"),
            {
                "name": "First CV",
                "template": "atlas",
                "mode": BuilderMode.EDITOR,
                "start": "example",
            },
        )
        document = ResumeDocument.objects.get(profile=self.profile)

        self.assertEqual(document.template.slug, "atlas")
        self.assertIn("started=example", response["Location"])

        act = reverse("resume:builder_act", kwargs={"document_id": document.public_id})
        self.client.post(act, {"command": "contact", "field": "headline", "value": "SRE"})
        document.draft.refresh_from_db()

        self.assertEqual(document.draft.content["contact"]["headline"], "SRE")

    def test_starting_blank_stays_blank(self):
        self.client.post(
            reverse("resume:builder"),
            {"name": "Blank CV", "template": "atlas", "mode": BuilderMode.EDITOR, "start": "blank"},
        )

        document = ResumeDocument.objects.get(profile=self.profile)
        self.assertNotIn("Nexa Payments", str(document.draft.content))

    def test_the_example_notice_is_only_shown_on_arrival(self):
        self.client.post(
            reverse("resume:builder"),
            {"template": "atlas", "mode": BuilderMode.EDITOR, "start": "example"},
        )
        document = ResumeDocument.objects.get(profile=self.profile)
        url = reverse("resume:editor", kwargs={"document_id": document.public_id})

        self.assertContains(self.client.get(url, {"started": "example"}), "worked example")
        self.assertNotContains(self.client.get(url), "worked example")


class SwitchingTemplatesTests(TemplateFixture):
    """A to B to C, with the candidate's own content, through the screens."""

    def setUp(self):
        super().setUp()
        self.document = document_service.create_document(
            profile=self.profile,
            actor=self.user,
            name="Ops CV",
            template=ResumeTemplate.objects.get(slug="meridian"),
        )
        draft = self.document.draft
        draft_service.set_contact(draft=draft, field="name", value="Anjali Thapa")
        draft_service.set_contact(draft=draft, field="headline", value="Site Reliability Engineer")
        draft.refresh_from_db()
        section = content_rules.section_by_key(draft.content, "experience")
        entry_id = draft_service.add_entry(draft=draft, section_id=section["id"])
        draft_service.update_entry(
            draft=draft,
            entry_id=entry_id,
            title="SRE",
            org="Kora Labs",
            dates="2021 - Present",
        )
        draft.refresh_from_db()
        skills = content_rules.section_by_key(draft.content, "skills")
        draft_service.set_section_items(
            draft=draft, section_id=skills["id"], items=["Kubernetes", "Terraform"]
        )
        self.document.refresh_from_db()
        self.content = dict(self.document.draft.content)
        self.act = reverse("resume:builder_act", kwargs={"document_id": self.document.public_id})

    def _switch(self, slug: str):
        return self.client.post(self.act, {"command": "template", "template": slug})

    def test_switching_through_three_templates_keeps_one_document_and_one_content(self):
        for slug in ("atlas", "regent", "prism"):
            self._switch(slug)
            self.document.refresh_from_db()

            with self.subTest(slug=slug):
                self.assertEqual(self.document.template.slug, slug)
                self.assertEqual(self.document.draft.content, self.content)

        self.assertEqual(ResumeDocument.objects.filter(profile=self.profile).count(), 1)

    def test_switching_changes_the_drawn_page_and_not_the_words(self):
        def drawn():
            self.document.refresh_from_db()
            paper = presentation.paper(self.document.draft.content, self.document.template)
            return paper["paper"], paper["paper_classes"]

        classic, classic_classes = drawn()
        self._switch("atlas")
        sidebar, sidebar_classes = drawn()

        # Presentation moved: the sidebar structure has a column the classic
        # one does not, and the root classes say so.
        self.assertNotEqual(classic_classes, sidebar_classes)
        self.assertEqual(classic.sidebar, ())
        self.assertNotEqual(sidebar.sidebar, ())

        # Content did not: every word on one page is on the other.
        self.assertEqual(_words(classic), _words(sidebar))

    def test_returning_to_the_first_template_returns_the_first_page(self):
        first = presentation.paper(self.content, ResumeTemplate.objects.get(slug="meridian"))[
            "paper_classes"
        ]

        self._switch("prism")
        self._switch("meridian")
        self.document.refresh_from_db()

        self.assertEqual(self.document.template.slug, "meridian")
        self.assertEqual(
            presentation.paper(self.document.draft.content, self.document.template)[
                "paper_classes"
            ],
            first,
        )
        self.assertEqual(self.document.draft.content, self.content)

    def test_applying_from_the_gallery_is_the_same_document(self):
        self.client.post(
            reverse("resume:template_act"),
            {"command": "apply", "slug": "lyceum", "document": str(self.document.public_id)},
        )
        self.document.refresh_from_db()

        self.assertEqual(self.document.template.slug, "lyceum")
        self.assertEqual(self.document.draft.content, self.content)
        self.assertEqual(ResumeDocument.objects.filter(profile=self.profile).count(), 1)

    def test_the_gallery_draws_the_same_content_in_every_card(self):
        response = self.client.get(reverse("resume:templates"))

        # The example, on every card — never this candidate's draft.
        self.assertGreaterEqual(response.content.decode().count("Priya Sharma"), len(CATALOGUE))
        self.assertNotContains(response, "Kora Labs")
        self.assertContains(response, "preview it with your own CV")

    def test_a_template_page_previews_this_cv_only_when_asked(self):
        url = reverse("resume:template_detail", kwargs={"slug": "atlas"})

        self.assertNotContains(self.client.get(url), "Kora Labs")
        self.assertContains(self.client.get(f"{url}?cv={self.document.public_id}"), "Kora Labs")

    def test_the_editor_offers_a_way_to_try_another_template(self):
        response = self.client.get(
            reverse("resume:editor", kwargs={"document_id": self.document.public_id})
        )

        self.assertContains(response, "Try another template")
        self.assertContains(response, reverse("resume:templates"))

    def test_exporting_still_works_after_switching(self):
        from apps.resumes.models import ResumeRender

        self._switch("regent")
        response = self.client.post(
            reverse("resume:export", kwargs={"document_id": self.document.public_id}),
            {"format": "pdf"},
        )

        self.assertContains(response, "Download")
        render = ResumeRender.objects.get()
        self.assertEqual(render.version.template_snapshot["slug"], "regent")
        self.assertEqual(render.version.content, self.content)


class CreationFlowsSurviveTests(TemplateFixture):
    """Guided and prompt-based creation still reach the same canonical draft."""

    def test_every_builder_mode_can_be_started_from_the_hub(self):
        """All three, because one of them could not be.

        The hub redirected an assistant document to `resume:ai`, which takes
        no document id: reversing it with one raised NoReverseMatch and the
        candidate got a 500 on the way into the only mode they had chosen
        deliberately.
        """
        for mode, expected in (
            (BuilderMode.EDITOR, "resume:editor"),
            (BuilderMode.GUIDED, "resume:guided"),
            (BuilderMode.ASSISTANT, "resume:assistant"),
        ):
            with self.subTest(mode=mode):
                response = self.client.post(
                    reverse("resume:builder"), {"name": f"{mode} CV", "mode": mode}
                )
                document = ResumeDocument.objects.filter(profile=self.profile).latest("created_at")

                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    response["Location"],
                    reverse(expected, kwargs={"document_id": document.public_id}),
                )
                self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_the_guided_flow_writes_the_one_canonical_draft(self):
        self.client.post(
            reverse("resume:builder"),
            {"name": "Guided CV", "template": "vertex", "mode": BuilderMode.GUIDED},
        )
        document = ResumeDocument.objects.get(profile=self.profile)
        act = reverse("resume:builder_act", kwargs={"document_id": document.public_id})

        self.assertEqual(document.builder_mode, BuilderMode.GUIDED)
        self.client.post(act, {"command": "contact", "field": "name", "value": "Anjali Thapa"})
        document.draft.refresh_from_db()
        self.assertEqual(document.draft.content["contact"]["name"], "Anjali Thapa")

        # And that same draft is what the gallery and every template draw.
        self.client.post(
            reverse("resume:template_act"),
            {"command": "apply", "slug": "circuit", "document": str(document.public_id)},
        )
        document.refresh_from_db()

        self.assertEqual(document.template.slug, "circuit")
        self.assertEqual(document.draft.content["contact"]["name"], "Anjali Thapa")

    def test_the_prompt_flow_writes_the_one_canonical_draft(self):
        from apps.ai.services import prompts

        prompts.load_all()
        self.client.post(
            reverse("resume:builder"),
            {"name": "Described CV", "template": "prism", "mode": BuilderMode.ASSISTANT},
        )
        document = ResumeDocument.objects.get(profile=self.profile)
        act = reverse("resume:builder_act", kwargs={"document_id": document.public_id})

        self.assertEqual(document.builder_mode, BuilderMode.ASSISTANT)
        # The assistant proposes and never writes (INV-3), so the draft is
        # written the same way every other mode writes it.
        self.client.post(act, {"command": "turn", "text": "I ran the payments platform."})
        self.client.post(act, {"command": "contact", "field": "name", "value": "Anjali Thapa"})
        document.draft.refresh_from_db()

        self.assertEqual(document.draft.content["contact"]["name"], "Anjali Thapa")
        self.assertEqual(
            document.builder_sessions.get(mode=BuilderMode.ASSISTANT).transcript[0]["role"],
            "candidate",
        )

    def test_a_guided_cv_can_be_seen_in_another_template_without_retyping(self):
        self.client.post(
            reverse("resume:builder"),
            {"name": "Guided CV", "template": "vertex", "mode": BuilderMode.GUIDED},
        )
        document = ResumeDocument.objects.get(profile=self.profile)
        act = reverse("resume:builder_act", kwargs={"document_id": document.public_id})
        self.client.post(act, {"command": "contact", "field": "name", "value": "Anjali Thapa"})
        document.draft.refresh_from_db()
        content = dict(document.draft.content)

        self.client.post(act, {"command": "template", "template": "atlas"})
        document.refresh_from_db()

        self.assertEqual(document.template.slug, "atlas")
        self.assertEqual(document.draft.content, content)
