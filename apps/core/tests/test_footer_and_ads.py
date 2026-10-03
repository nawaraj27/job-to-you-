"""
The site footer and the demo advertisement slots.

Footer: rendered exactly once, every link resolves for a visitor, no link is a
private or staff page, no two links go to one place, and the grid is valid CSS
(the invalid `grid-cols-[1.4fr_repeat(auto-fit,…)]` was the cause of the
one-column footer that ran the height of a phone screen on a desktop).

Demo ads (`core.ads`): three fixed slots, labelled as demo advertisements,
internal "Learn more", no third-party content, hideable per session, switched
off by one feature flag — and absent from every task a candidate or employer
is in the middle of: an interview question, the CV editor, an upload, sign-in,
the plan and billing screens, an employer's job form.
"""

from __future__ import annotations

import re

from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from apps.candidates.tests.factories import create_profile
from apps.core import ads, features
from apps.core.navigation import FOOTER_NAV, footer_nav

PUBLIC_PAGES = ("core:home", "core:pricing", "core:about", "core:contact", "jobs:list")


class FooterTests(TestCase):
    def test_the_footer_renders_exactly_once_on_public_pages(self):
        for name in PUBLIC_PAGES:
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertEqual(html.count("<footer"), 1)

    def test_every_footer_link_resolves_for_a_visitor(self):
        for group in footer_nav():
            for link in group["links"]:
                with self.subTest(link=link["label"]):
                    response = self.client.get(link["url"])
                    # A page, or a signed-in page sending the visitor to sign in.
                    self.assertIn(response.status_code, (200, 302))
                    if response.status_code == 302:
                        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_no_two_footer_links_go_to_the_same_place(self):
        urls = [link["url"] for group in footer_nav() for link in group["links"]]
        self.assertEqual(len(urls), len(set(urls)))

    def test_no_private_or_staff_link_is_in_the_footer(self):
        for _title, items in FOOTER_NAV:
            for item in items:
                with self.subTest(link=item.label):
                    self.assertFalse(str(item.url_name).startswith(("staff:", "admin:")))
                    self.assertNotIn("design_system", str(item.url_name))

    def test_a_link_behind_a_disabled_feature_is_dropped(self):
        labels = [
            link["label"]
            for group in footer_nav({**features.DEFAULT_FEATURES, "interview": False})
            for link in group["links"]
        ]
        self.assertNotIn("Practice interviews", labels)

    def test_the_footer_grid_is_valid_css(self):
        html = self.client.get(reverse("core:home")).content.decode()
        footer = html[html.index("<footer") : html.index("</footer>")]
        self.assertNotIn("auto-fit", footer)
        self.assertIn("grid-cols-2", footer)  # two-up on a phone, not one long column

    def test_footer_groups_are_labelled_navigation_landmarks(self):
        html = self.client.get(reverse("core:home")).content.decode()
        footer = html[html.index("<footer") : html.index("</footer>")]
        for match in re.finditer(r'<nav aria-labelledby="(footer-\d+)"', footer):
            self.assertIn(f'id="{match.group(1)}"', footer)
        self.assertGreaterEqual(footer.count("<nav"), 3)


class FooterEmployerLinkTests(TestCase):
    """The employer column links to company pages without knowing the company."""

    def links(self):
        return {link["label"]: link["url"] for group in footer_nav() for link in group["links"]}

    def test_the_employer_column_has_what_an_employer_looks_for(self):
        labels = set(self.links())
        for label in (
            "Hiring with us",
            "Employer portal",
            "Post a job",
            "Manage jobs",
            "Applicants",
            "Company profile",
            "Create a company account",
        ):
            self.assertIn(label, labels)

    def test_each_shortcut_opens_the_members_own_company_page(self):
        from apps.companies.tests.factories import company_with_owner

        owner = create_profile().user
        company, _ = company_with_owner(owner)
        self.client.force_login(owner)
        expected = {
            "Post a job": reverse("employer:job_create", args=[company.slug]),
            "Manage jobs": reverse("employer:job_list", args=[company.slug]),
            "Applicants": reverse("employer:applicants", args=[company.slug]),
            "Company profile": reverse("employer:profile", args=[company.slug]),
        }
        links = self.links()
        for label, target in expected.items():
            with self.subTest(link=label):
                response = self.client.get(links[label])
                self.assertRedirects(response, target, fetch_redirect_response=False)
                self.assertEqual(self.client.get(target).status_code, 200)

    def test_without_a_company_the_shortcut_offers_registration(self):
        self.client.force_login(create_profile().user)
        response = self.client.get(self.links()["Manage jobs"])
        self.assertRedirects(response, reverse("employer:create"), fetch_redirect_response=False)

    def test_a_member_of_two_companies_chooses_one(self):
        from apps.companies.permissions import Role
        from apps.companies.tests.factories import add_member, company_with_owner

        owner = create_profile().user
        first, _ = company_with_owner(owner)
        second, _ = company_with_owner(create_profile().user)
        add_member(second, owner, role=Role.RECRUITER)
        self.client.force_login(owner)
        response = self.client.get(self.links()["Applicants"])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, first.name)
        self.assertContains(response, second.name)

    def test_a_role_without_the_page_lands_on_the_overview_not_a_403(self):
        from apps.companies.permissions import Role
        from apps.companies.tests.factories import add_member, company_with_owner

        company, _ = company_with_owner(create_profile().user)
        interviewer = create_profile().user
        add_member(company, interviewer, role=Role.INTERVIEWER)
        self.client.force_login(interviewer)
        links = self.links()
        for label in ("Post a job", "Manage jobs", "Company profile", "Applicants"):
            with self.subTest(link=label):
                response = self.client.get(links[label], follow=True)
                self.assertEqual(response.status_code, 200)

    def test_an_unknown_shortcut_is_a_404(self):
        self.client.force_login(create_profile().user)
        self.assertEqual(self.client.get("/employer/go/billing-secrets/").status_code, 404)

    def test_go_cannot_be_a_company_slug(self):
        from apps.companies.middleware import RESERVED_SLUGS

        self.assertIn("go", RESERVED_SLUGS)


class DemoAdUnitTests(TestCase):
    def request(self):
        request = RequestFactory().get("/")
        request.session = self.client.session
        return request

    def test_only_listed_slots_show_an_ad(self):
        request = self.request()
        self.assertIsNotNone(ads.for_slot(request, "jobs"))
        self.assertIsNone(ads.for_slot(request, "interview"))
        self.assertIsNone(ads.for_slot(request, "checkout"))

    def test_every_demo_ad_is_labelled_as_one_and_links_inside_the_site(self):
        for ad in ads.DEMO_ADS:
            with self.subTest(ad=ad.key):
                self.assertIn("Demo", ad.label)
                self.assertTrue(ad.url.startswith("/sponsored/"))
                self.assertTrue(ad.slots <= ads.SLOTS)

    def test_hiding_is_remembered_for_the_session(self):
        request = self.request()
        ad = ads.for_slot(request, "jobs")
        self.assertTrue(ads.hide(request, ad.key))
        self.assertNotEqual(getattr(ads.for_slot(request, "jobs"), "key", None), ad.key)
        self.assertFalse(ads.hide(request, "no-such-ad"))

    @override_settings(CAREERAI_FEATURES={"demo_ads": False})
    def test_the_feature_flag_turns_every_slot_off(self):
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)
        request = self.request()
        for slot in ads.SLOTS:
            self.assertIsNone(ads.for_slot(request, slot))


class DemoAdPlacementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_interview_templates", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.client.force_login(self.profile.user)

    def test_the_jobs_list_shows_one_labelled_demo_ad(self):
        from apps.companies.tests.factories import company_with_owner
        from apps.jobs.tests.factories import create_job, publish

        company, _ = company_with_owner(create_profile().user)
        for _ in range(7):
            publish(create_job(company))

        html = self.client.get(reverse("jobs:list")).content.decode()
        self.assertEqual(html.count('<aside id="demo-ad-'), 1)
        self.assertIn("Advertisement · Demo", html)
        self.assertIn('rel="sponsored"', html)

    def test_the_analysis_page_shows_its_slot(self):
        html = self.client.get(reverse("resume:analysis")).content.decode()
        self.assertIn('id="demo-ad-analysis"', html)

    def test_an_ad_never_carries_third_party_content(self):
        from apps.companies.tests.factories import company_with_owner
        from apps.jobs.tests.factories import create_job, publish

        company, _ = company_with_owner(create_profile().user)
        publish(create_job(company))
        html = self.client.get(reverse("jobs:list")).content.decode()
        block = html[html.index('<aside id="demo-ad-') :]
        block = block[: block.index("</aside>")]
        self.assertNotIn("<script", block)
        self.assertNotIn("<img", block)
        self.assertNotIn("http://", block)
        self.assertNotIn("https://", block)

    def test_no_ad_where_a_task_is_under_way(self):
        from apps.interviews.services import engine
        from apps.interviews.tests.factories import fake_ai, practice_template
        from apps.resumes.services import documents as document_service

        document = document_service.create_document(
            profile=self.profile, actor=self.profile.user, name="CV"
        )
        with fake_ai():
            session = engine.start(
                candidate=self.profile, template=practice_template(), actor=self.profile.user
            )
            interview = self.client.get(
                reverse("interviews:session", kwargs={"public_id": session.public_id})
            )
        pages = {
            "interview question": interview,
            "CV editor": self.client.get(
                reverse("resume:editor", kwargs={"document_id": document.public_id})
            ),
            "guided": self.client.get(
                reverse("resume:guided", kwargs={"document_id": document.public_id})
            ),
            "assisted": self.client.get(
                reverse("resume:assistant", kwargs={"document_id": document.public_id})
            ),
            "upload": self.client.get(reverse("resume:upload")),
            "plan": self.client.get(reverse("dashboard:plan")),
            "pricing": self.client.get(reverse("core:pricing")),
        }
        self.client.logout()
        pages["sign in"] = self.client.get(reverse("accounts:login"))
        pages["sign up"] = self.client.get(reverse("accounts:signup"))
        for name, response in pages.items():
            with self.subTest(page=name):
                self.assertNotIn("demo-ad", response.content.decode())

    def test_hiding_from_a_page_without_scripts_returns_to_it(self):
        response = self.client.post(
            reverse("core:demo_ad_hide"),
            {"key": "example-learning"},
            HTTP_ACCEPT="text/html",
            HTTP_SEC_FETCH_DEST="document",
            HTTP_REFERER="http://testserver" + reverse("jobs:list"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith(reverse("jobs:list")))
        self.assertIn("example-learning", self.client.session[ads.DISMISSED_KEY])

    def test_hiding_with_htmx_swaps_the_ad_for_nothing(self):
        response = self.client.post(
            reverse("core:demo_ad_hide"), {"key": "example-learning"}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def test_the_learn_more_page_says_it_was_a_demo(self):
        response = self.client.get(reverse("core:demo_ad", args=["example-learning"]))
        self.assertContains(response, "This was a demo advertisement")
        self.assertContains(response, "fictional")
        self.assertEqual(self.client.get(reverse("core:demo_ad", args=["nope"])).status_code, 404)
