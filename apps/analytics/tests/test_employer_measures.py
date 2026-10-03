"""
The three SRS §16.2 employer measures the analytics screen lacked (Loop Closure
ANA-02): source effectiveness, applicant quality distribution, recruiter workload.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.analytics.services import reports
from apps.applications.models import Application
from apps.applications.services import review as review_service
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish


class EmployerMeasureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user(full_name="Asha Owner")
        self.company, self.membership = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company))
        self.applications = [submit(job=self.job, profile=create_profile()) for _ in range(4)]
        for application, score in zip(self.applications, (91, 72, 40, None), strict=True):
            Application.objects.filter(pk=application.pk).update(
                match_snapshot={} if score is None else {"score": score, "band": "x"}
            )
        Application.objects.filter(pk=self.applications[0].pk).update(source="invited")

    def test_quality_bands_follow_fr_8_2_and_count_unscored(self):
        rows = {row["band"]: row["count"] for row in reports.quality_distribution(self.company)}
        self.assertEqual(
            rows, {"Strong": 1, "Good": 1, "Partial": 0, "Stretch": 1, "Not scored": 1}
        )

    def test_sources_are_counted_with_their_labels(self):
        rows = {
            row["source"]: row["applications"] for row in reports.source_effectiveness(self.company)
        }
        self.assertEqual(rows["Applied directly"], 3)
        self.assertEqual(rows["Invited by the employer"], 1)

    def test_an_application_with_many_timeline_events_is_counted_once(self):
        from apps.applications.models import ActorKind, ApplicationEvent

        for _ in range(5):
            ApplicationEvent.objects.create(
                application=self.applications[1],
                actor=self.owner,
                actor_kind=ActorKind.EMPLOYER,
                type="application.note_added",
            )
        total = sum(row["applications"] for row in reports.source_effectiveness(self.company))
        self.assertEqual(total, 4)

    def test_workload_counts_notes_by_author(self):
        application = self.applications[1]
        review_service.add_note(
            application=application,
            actor=self.owner,
            membership=self.membership,
            body="Strong SQL.",
        )
        rows = reports.recruiter_workload(self.company)
        self.assertEqual(rows, [{"name": "Asha Owner", "moves": 0, "notes": 1}])

    def test_another_company_s_applications_are_not_counted(self):
        other, _ = company_with_owner(create_user(), slug="other-co")
        submit(job=publish(create_job(other)), profile=create_profile())
        total = sum(row["applications"] for row in reports.source_effectiveness(self.company))
        self.assertEqual(total, 4)

    def test_the_screen_shows_the_new_sections_when_there_is_data(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("employer:analytics", args=["acme-co"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["quality"]), 5)
