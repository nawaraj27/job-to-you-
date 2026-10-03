"""
Who a page is rendered for (P19, replacing the P0 persona stub).

The stub this replaces resolved a *fictional* person from `?as=` and `?state=`
query parameters and remembered the answer in the session. Both halves were
defects by the time real accounts existed:

  * an anonymous visitor could write to their own session by passing
    `?as=employer`, so any crawler following such a link created a session row
    and changed what the shell rendered;
  * a signed-in candidate was greeted with the stub's hard-coded headline,
    because only name and email were ever overwritten with real values.

So the assertions here are mostly about *absence*: no fabricated values, and no
query parameter that changes anything.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.candidates.tests.factories import create_profile
from apps.core.context_processors import navigation


class ViewerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def context_for(self, user=None, path="/dashboard/"):
        request = RequestFactory().get(path)
        request.user = user or _anonymous()
        request.session = {}
        return navigation(request)

    def test_an_anonymous_visitor_gets_no_identity(self):
        viewer = self.context_for()["viewer"]

        self.assertFalse(viewer["authenticated"])
        self.assertEqual(viewer["name"], "")
        self.assertEqual(viewer["headline"], "")

    def test_a_signed_in_candidate_gets_their_own_name(self):
        profile = create_profile()
        profile.user.full_name = "Priya Sharma"
        profile.user.save(update_fields=["full_name"])

        viewer = self.context_for(profile.user)["viewer"]

        self.assertTrue(viewer["authenticated"])
        self.assertEqual(viewer["name"], "Priya Sharma")
        self.assertEqual(viewer["first_name"], "Priya")

    def test_the_headline_is_the_profiles_own_or_nothing(self):
        # The stub rendered "Backend Engineer · 3 years" at everybody.
        profile = create_profile()
        profile.headline = "Django engineer, five years"
        profile.save(update_fields=["headline"])

        self.assertEqual(self.context_for(profile.user)["viewer"]["headline"], profile.headline)

    def test_an_empty_headline_stays_empty(self):
        profile = create_profile()
        profile.headline = ""
        profile.save(update_fields=["headline"])

        self.assertEqual(self.context_for(profile.user)["viewer"]["headline"], "")

    def test_a_query_parameter_cannot_change_the_role(self):
        profile = create_profile()
        context = self.context_for(profile.user, path="/dashboard/?as=employer")

        self.assertEqual(context["viewer"]["role"], "candidate")

    def test_a_query_parameter_writes_nothing_to_an_anonymous_session(self):
        request = RequestFactory().get("/?as=employer&state=new")
        request.user = _anonymous()
        request.session = {}

        navigation(request)
        self.assertEqual(request.session, {})

    def test_the_dashboard_greets_the_real_person(self):
        profile = create_profile()
        profile.user.full_name = "Ram Bahadur"
        profile.user.save(update_fields=["full_name"])
        profile.headline = "Data analyst"
        profile.save(update_fields=["headline"])

        self.client.force_login(profile.user)
        response = self.client.get(reverse("dashboard:index"))

        self.assertContains(response, "Good to see you, Ram")
        self.assertContains(response, "Data analyst")
        self.assertNotContains(response, "Backend Engineer · 3 years")

    def test_the_demo_switch_is_gone(self):
        from django.template import TemplateDoesNotExist
        from django.template.loader import get_template

        with self.assertRaises(TemplateDoesNotExist):
            get_template("partials/demo_switch.html")


def _anonymous():
    from django.contrib.auth.models import AnonymousUser

    return AnonymousUser()
