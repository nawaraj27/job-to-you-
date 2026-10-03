"""
Screening answers beside their questions on the employer screen (Loop Closure
JOB-03, found driving the screening-question journey in a browser).

Every answer was printed under one label, "Screening answer", so with more than
one question the employer could not tell which reply answered which question.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.factories import create_user
from apps.applications.tests.factories import seed_templates, submit
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.models import JobScreeningQuestion
from apps.jobs.tests.factories import create_job, publish


class ScreeningAnswerDisplayTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_templates()

    def setUp(self):
        self.owner = create_user()
        self.company, _ = company_with_owner(self.owner, slug="acme-co")
        self.job = publish(create_job(self.company))
        self.years = JobScreeningQuestion.objects.create(
            job=self.job, kind="number", text="How many years with PostgreSQL?", order=1
        )
        self.visa = JobScreeningQuestion.objects.create(
            job=self.job,
            kind="boolean",
            text="Do you need a work permit?",
            order=2,
            is_required=False,
        )
        self.tools = JobScreeningQuestion.objects.create(
            job=self.job,
            kind="multi_choice",
            text="Which queues have you run?",
            options=["Celery", "RabbitMQ", "Kafka"],
            order=3,
        )
        self.application = submit(
            job=self.job,
            profile=create_profile(),
            answers={
                str(self.years.pk): "4",
                str(self.visa.pk): "on",
                str(self.tools.pk): ["Celery", "Kafka"],
            },
        )
        self.url = reverse(
            "employer:application_detail", args=["acme-co", self.application.public_id]
        )
        self.client.force_login(self.owner)

    def test_each_answer_is_labelled_with_its_question_in_posting_order(self):
        rows = self.client.get(self.url).context["screening_answers"]

        self.assertEqual(
            [row["question"] for row in rows],
            [
                "How many years with PostgreSQL?",
                "Do you need a work permit?",
                "Which queues have you run?",
            ],
        )
        self.assertEqual(rows[2]["answer"], "Celery, Kafka")

    def test_the_page_shows_the_question_text(self):
        response = self.client.get(self.url)
        self.assertContains(response, "How many years with PostgreSQL?")
        self.assertContains(response, "Which queues have you run?")
        self.assertNotContains(response, ">Screening answer<")

    def test_an_answer_to_a_deleted_question_is_named_as_such(self):
        self.tools.delete()
        rows = self.client.get(self.url).context["screening_answers"]
        self.assertEqual(rows[-1]["question"], "A question since removed from the posting")
