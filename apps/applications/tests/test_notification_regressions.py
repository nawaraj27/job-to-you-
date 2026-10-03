"""
Candidate and team notifications from the pipeline, delivered and read.

Reproduced by running the outbox drain and fan-out on the development database:

  * a hired candidate received "Fonepay closed your application" alongside
    "moved your application to: Hired";
  * every rejection arrived twice — a "closed" notification and a stage change;
  * the team's withdrawal notice named no role.
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.applications.services import apply as apply_service
from apps.applications.services import pipeline as pipeline_service
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.notifications.models import Notification
from apps.notifications.services import fanout, outbox


def deliver() -> None:
    for _ in range(10):
        if not outbox.drain(500):
            break
    for _ in range(10):
        if not fanout.run_due(limit=500).get("chunks"):
            break


class PipelineNotificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company, title="Fraud Analyst"))
        self.candidate = create_profile()
        self.application = submit(job=self.job, profile=self.candidate)
        deliver()

    def candidate_feed(self):
        return Notification.objects.filter(recipient=self.candidate.user).exclude(
            kind="application.submitted"
        )

    def move(self, key, reason=""):
        pipeline_service.move(
            application=self.application,
            actor=self.owner,
            membership=self.membership,
            stage=pipeline_service.stage_by_key(self.company, key),
            reason=reason,
        )
        deliver()

    def test_a_rejection_is_one_notification_carrying_the_reason(self):
        self.move("declined", reason="Not enough fraud experience")

        rows = list(self.candidate_feed())
        self.assertEqual(len(rows), 1, [(r.kind, r.title) for r in rows])
        self.assertIn("Not proceeding", rows[0].body)
        self.assertIn("Not enough fraud experience", rows[0].body)

    def test_a_hire_is_never_called_a_closed_application(self):
        self.move("hired", reason="Accepted offer")

        rows = list(self.candidate_feed())
        self.assertEqual(len(rows), 1)
        self.assertIn("Hired", rows[0].body)
        self.assertNotIn("closed", (rows[0].title + rows[0].body).lower())

    def test_an_ordinary_move_carries_no_reason_line(self):
        self.move("reviewing")

        rows = list(self.candidate_feed())
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0].body.strip(), f"{self.company.name} moved your application to: Reviewing."
        )

    def test_the_team_is_told_which_role_a_candidate_withdrew_from(self):
        apply_service.withdraw(application=self.application, actor=self.candidate.user)
        deliver()

        row = Notification.objects.filter(
            recipient=self.owner, kind="application.withdrawn"
        ).first()
        self.assertIsNotNone(row)
        self.assertIn("Fraud Analyst", row.title + row.body)


class PipelineFormLabelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner, slug="acme-co")
        self.client.force_login(self.owner)

    def test_every_pipeline_control_has_an_accessible_name(self):
        import re

        from django.urls import reverse

        html = self.client.get(reverse("employer:pipeline", args=["acme-co"])).content.decode()
        form = html.split('<form method="post"', 1)[1].split("</form>", 1)[0]
        controls = re.findall(r"<(?:input|select)\b[^>]*>", form)
        unnamed = [
            c
            for c in controls
            if 'type="hidden"' not in c and "aria-label=" not in c and 'id="' not in c
        ]
        self.assertTrue(controls)
        self.assertEqual(unnamed, [])
