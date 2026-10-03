"""
The candidate's "Download my data" file held only profile sections.

FR-7.6 requires messages in the export, and the SRS names the same file as the
candidate's access and portability right — but applications, cover letters,
messages and the record of who opened the profile were all missing.
"""

from __future__ import annotations

import json

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.applications.models import PipelineStage
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.models import ProfileView, ViewSource
from apps.candidates.services import erasure as erasure_service
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.messaging.models import Message, SenderKind
from apps.messaging.services import conversations


class DataExportActivityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company, title="Fraud Analyst"))
        self.profile = create_profile()
        self.application = submit(job=self.job, profile=self.profile)
        self.thread = conversations.open_conversation(
            company=self.company,
            candidate=self.profile,
            actor=self.owner,
            membership=self.membership,
            job=self.job,
            application=self.application,
        )
        conversations.send(
            conversation=self.thread,
            actor=self.owner,
            sender_kind=SenderKind.COMPANY,
            body="Are you free Thursday?",
        )
        ProfileView.objects.create(
            candidate=self.profile,
            company=self.company,
            actor=self.owner,
            source=ViewSource.APPLICATION,
        )

    def export(self):
        data = erasure_service.export_profile(self.profile)
        json.dumps(data)  # the file must serialise
        return data

    def test_messages_are_in_the_export_without_naming_the_recruiter(self):
        data = self.export()
        self.assertEqual(len(data["messages"]), 1)
        message = data["messages"][0]
        self.assertEqual(message["company"], self.company.name)
        self.assertEqual(message["about"], "Fraud Analyst")
        self.assertEqual(message["from"], "employer")
        self.assertEqual(message["body"], "Are you free Thursday?")
        self.assertNotIn(self.owner.email, json.dumps(data))

    def test_a_deleted_message_is_listed_without_its_text(self):
        Message.objects.filter(conversation=self.thread).update(deleted_at=timezone.now())
        message = self.export()["messages"][0]
        self.assertTrue(message["deleted"])
        self.assertIsNone(message["body"])

    def test_applications_use_the_stage_name_the_candidate_is_told(self):
        PipelineStage.objects.filter(pk=self.application.stage_id).update(
            label="Secret shortlist", is_visible_to_candidate=False, candidate_label="Under review"
        )
        data = self.export()
        self.assertEqual(data["applications"][0]["job"], "Fraud Analyst")
        self.assertEqual(data["applications"][0]["stage"], "Under review")
        self.assertNotIn("Secret shortlist", json.dumps(data))

    def test_views_and_access_are_in_the_export(self):
        data = self.export()
        self.assertEqual(data["profile_views"][0]["company"], self.company.name)
        self.assertEqual(data["profile_views"][0]["sources"], [ViewSource.APPLICATION])
        self.assertIn(self.company.name, [row["company"] for row in data["access"]])

    def test_the_download_and_the_data_screen_show_the_activity(self):
        self.client.force_login(self.profile.user)
        payload = json.loads(self.client.get(reverse("settings:export")).content)
        self.assertEqual(len(payload["messages"]), 1)

        response = self.client.get(reverse("settings:data"))
        self.assertContains(response, "Messages")
        self.assertContains(response, "Profile views")
        self.assertEqual(response.context["counts"]["applications"], 1)
        self.assertEqual(response.context["counts"]["profile"], 0)
