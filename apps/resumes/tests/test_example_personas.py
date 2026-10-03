"""
Worked examples per kind of CV (CP-2).

Each persona is a complete, renderable CV; none uses a real organisation, a
real contact or an em dash; a template's page shows the persona it was made
for; starting from the example gives the candidate a private copy with their
own name, and the constants are never written to.
"""

from __future__ import annotations

import copy
import json

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.candidates.tests.factories import create_profile
from apps.resumes import example_personas, presentation
from apps.resumes.models import ResumeDocument, ResumeTemplate

#: Organisations and products a sample must never claim experience at.
REAL_NAMES = ("Fonepay", "Khalti", "eSewa", "Google", "Unilever", "Leapfrog", "Nabil", "Daraz")


class PersonaContentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)

    def test_every_persona_is_plain_fictional_and_complete(self):
        for key, persona in example_personas.ALL.items():
            with self.subTest(persona=key):
                text = json.dumps(persona, ensure_ascii=False)
                self.assertNotIn("—", text)
                self.assertTrue(persona["contact"]["email"].endswith("@example.com"))
                for name in REAL_NAMES:
                    self.assertNotIn(name, text)
                keys = [section["key"] for section in persona["sections"]]
                self.assertEqual(keys[:4], ["summary", "experience", "skills", "education"])

    def test_every_persona_renders_in_every_template(self):
        for template in ResumeTemplate.objects.all():
            for key, persona in example_personas.ALL.items():
                with self.subTest(template=template.slug, persona=key):
                    paper = presentation.paper(copy.deepcopy(persona), template)
                    self.assertTrue(paper)

    def test_templates_get_the_person_they_were_made_for(self):
        self.assertIs(example_personas.BY_TEMPLATE["plainspoke"], example_personas.NURSE)
        self.assertIs(example_personas.BY_TEMPLATE["prism"], example_personas.DESIGNER)
        self.assertIs(example_personas.BY_TEMPLATE["lyceum"], example_personas.ACADEMIC)
        self.assertIsNone(example_personas.for_template("atlas"))  # the shared example


class PersonaScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.client.force_login(self.profile.user)

    def test_a_templates_page_shows_its_persona(self):
        page = self.client.get(reverse("resume:template_detail", args=["plainspoke"]))
        self.assertContains(page, "Sabina Thapa")
        self.assertContains(page, "Staff Nurse")

    def test_starting_from_the_example_copies_the_persona_with_the_candidates_name(self):
        before = copy.deepcopy(example_personas.NURSE)
        self.client.post(
            reverse("resume:builder"),
            {"name": "Nursing CV", "template": "plainspoke", "mode": "editor", "start": "example"},
        )
        document = ResumeDocument.objects.get(profile=self.profile)
        content = document.draft.content
        self.assertEqual(content["contact"]["name"], self.profile.user.full_name)
        self.assertIn("Valley District Hospital", json.dumps(content))
        self.assertEqual(example_personas.NURSE, before)  # the constant is untouched
