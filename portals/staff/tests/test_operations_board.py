"""
The staff operations board as a screen (SRS §3.12, §17.4, OPS-01).

The board had been checked for one thing — that a company owner without a
`StaffRole` gets a 404 — and never looped against what §17.4 says an operator
must be able to see. These tests are that loop: who may open it, what it must
answer, and what it must still do when the thing it is reporting on is down.

The access rows are stated from the failing side for every kind of visitor,
because "who can read the platform's operational numbers" is a question that
must never be answered by accident.
"""

from __future__ import annotations

import datetime as dt
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StaffRole
from apps.ai.models import AIRun, RunStatus
from apps.analytics.models import AICostDaily
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.core import selectors
from apps.core.models import FeatureFlag

#: What a healthy broker looks like to the board, so a test machine without a
#: running Redis still exercises the same rendering path.
BROKER_UP = {
    "available": True,
    "error": "",
    "depths": {"ai": 12, "default": 0, "email": 0, "files": 0, "match": 0, "render": 0},
    "total": 12,
}
BROKER_DOWN = {"available": False, "error": "OperationalError", "depths": {}, "total": 0}


class BoardFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.url = reverse("staff:operations")
        self.support = create_profile().user
        StaffRole.objects.create(user=self.support, level="support")
        selectors._release_broker()
        self.addCleanup(selectors._release_broker)

    def as_support(self, broker=BROKER_UP):
        self.client.force_login(self.support)
        with mock.patch.object(selectors, "queue_depths", return_value=broker):
            return self.client.get(self.url)


class BoardAccessTests(BoardFixture):
    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("accounts:login")))

    def test_a_candidate_gets_a_404_not_a_403(self):
        # A 403 confirms the screen exists, which is a fact a candidate has no
        # business learning.
        self.client.force_login(create_profile().user)

        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_a_company_owner_is_not_platform_staff(self):
        owner = create_profile().user
        company_with_owner(owner)
        self.client.force_login(owner)

        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_django_is_staff_flag_is_not_authority_here(self):
        # `is_staff` governs the admin site. Two systems answering "is this an
        # operator" eventually disagree, and the one that mattered would be
        # whichever the view happened to ask.
        flagged = create_profile().user
        flagged.is_staff = True
        flagged.save(update_fields=["is_staff"])
        self.client.force_login(flagged)

        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_support_is_the_level_the_board_needs(self):
        self.assertEqual(self.as_support().status_code, 200)

    def test_every_level_above_support_can_read_it(self):
        for level in ("moderator", "admin", "superadmin"):
            with self.subTest(level=level):
                user = create_profile().user
                StaffRole.objects.create(user=user, level=level)
                self.client.force_login(user)
                with mock.patch.object(selectors, "queue_depths", return_value=BROKER_UP):
                    self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_board_is_read_only(self):
        # Acting during an incident belongs on the screen that owns the thing
        # being acted on, with its audit trail. A POST here has no handler and
        # must not quietly become a GET.
        self.client.force_login(self.support)

        self.assertEqual(self.client.post(self.url, {}).status_code, 405)

    def test_the_staff_index_is_the_same_board(self):
        self.client.force_login(self.support)
        with mock.patch.object(selectors, "queue_depths", return_value=BROKER_UP):
            response = self.client.get(reverse("staff:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Outbox waiting")


class BoardContentTests(BoardFixture):
    """Every question §17.4 says an operator is alerted on."""

    def test_the_outbox_backlog_and_its_age_are_shown(self):
        from apps.notifications.models import OutboxEvent

        waiting = OutboxEvent.objects.create(type="application.received", payload={})
        OutboxEvent.objects.filter(pk=waiting.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=3)
        )

        response = self.as_support()

        self.assertEqual(response.context["outbox_pending"], 1)
        self.assertContains(response, "Outbox waiting")
        self.assertContains(response, "old")

    def test_an_empty_outbox_says_so(self):
        self.assertContains(self.as_support(), "Nothing waiting")

    def test_queue_depth_is_listed_per_queue(self):
        from config.celery import QUEUES

        response = self.as_support()

        self.assertContains(response, "Queue depth")
        for name in QUEUES:
            with self.subTest(queue=name):
                self.assertContains(response, name)

    def test_an_unreachable_broker_renders_as_the_finding(self):
        # The board must survive the outage it is describing: nothing is being
        # worked at all, and that is the single most important thing on the
        # screen rather than a stack trace.
        response = self.as_support(broker=BROKER_DOWN)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The broker could not be reached")
        self.assertContains(response, "OperationalError")

    def test_the_three_readyz_dependencies_are_reported(self):
        from apps.common import health

        response = self.as_support()

        self.assertContains(response, "Dependencies")
        for probe in health.PROBES:
            with self.subTest(probe=probe.key):
                self.assertContains(response, probe.label)
        self.assertContains(response, "Reachable", count=len(health.PROBES))

    def test_a_failing_dependency_is_named_on_the_board(self):
        from apps.common import health

        def boom() -> None:
            raise RuntimeError("storage is gone")

        with mock.patch.object(
            health, "PROBES", (health.Probe("private_storage", "Private storage", boom),)
        ):
            response = self.as_support()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "error: RuntimeError")

    def test_todays_ai_spend_and_failures_are_shown(self):
        for n in range(4):
            AIRun.objects.create(
                task_key="resume.analysis",
                provider="test",
                model="test-model",
                input_hash=f"{n:064d}",
                status=RunStatus.OK if n else RunStatus.PROVIDER_ERROR,
                cost="3.1250",
            )

        response = self.as_support()

        self.assertContains(response, "AI today")
        self.assertEqual(response.context["ai_today"]["runs"], 4)
        self.assertEqual(response.context["ai_today"]["failures"], 1)
        self.assertContains(response, "12.5000")
        self.assertContains(response, "25% of runs")

    def test_a_one_day_retention_rule_reads_in_the_singular(self):
        body = self.as_support().content.decode()

        self.assertRegex(body, r">\s*1 day\s*<")
        self.assertNotRegex(body, r">\s*1 days\s*<")

    def test_yesterdays_rollup_is_shown_beside_today_and_not_instead_of_it(self):
        # Both, and labelled: the rollup is the complete day and today is the
        # one being lived through. Showing only the rollup was the defect.
        AICostDaily.objects.create(
            day=timezone.localdate() - dt.timedelta(days=1),
            task_key="resume.analysis",
            runs=10,
            failures=2,
            cost="2.0000",
        )
        AIRun.objects.create(
            task_key="interview.evaluate",
            provider="test",
            model="test-model",
            input_hash="a" * 64,
            status=RunStatus.OK,
            cost="9.0000",
        )

        response = self.as_support()

        self.assertContains(response, "AI cost yesterday")
        self.assertContains(response, "2.0000")
        self.assertContains(response, "9.0000")

    def test_a_quiet_day_says_so_rather_than_showing_an_empty_table(self):
        response = self.as_support()

        self.assertContains(response, "No AI runs yet today")
        self.assertContains(response, "No rollup for yesterday yet")

    def test_every_retention_promise_is_listed_with_the_job_that_keeps_it(self):
        from apps.common import retention

        response = self.as_support()

        self.assertEqual(len(response.context["retention"]), len(retention.SCHEDULE))
        for rule in retention.SCHEDULE:
            with self.subTest(rule=rule.key):
                self.assertContains(response, rule.label)
                self.assertContains(response, rule.task)

    def test_a_flag_past_its_expiry_is_named(self):
        FeatureFlag.objects.create(
            key="old_experiment",
            is_enabled=True,
            expires_on=timezone.localdate() - dt.timedelta(days=1),
        )

        response = self.as_support()

        self.assertContains(response, "old_experiment")

    def test_a_scheduled_erasure_is_counted(self):
        from apps.accounts.services import erasure

        candidate = create_profile().user
        erasure.request_erasure(user=candidate, actor=candidate, confirmation=erasure.CONFIRMATION)

        self.assertEqual(self.as_support().context["erasures_pending"], 1)

    def test_the_board_names_no_person(self):
        # Platform aggregates only. The one screen every support agent can open
        # must not become a way to read who is being erased or whose analysis
        # failed — that is what the anomaly queue and the run inspector are
        # for, and both need more than support.
        from apps.accounts.services import erasure

        candidate = create_profile().user
        candidate.full_name = "Priya Sharma"
        candidate.save(update_fields=["full_name"])
        erasure.request_erasure(user=candidate, actor=candidate, confirmation=erasure.CONFIRMATION)
        AIRun.objects.create(
            task_key="resume.analysis",
            provider="test",
            model="test-model",
            input_hash="b" * 64,
            status=RunStatus.PROVIDER_ERROR,
            cost="1.0000",
            user=candidate,
        )

        body = self.as_support().content.decode()

        self.assertNotIn("Priya Sharma", body)
        self.assertNotIn(candidate.email, body)


class BoardBudgetTests(BoardFixture):
    def test_the_board_stays_inside_the_page_budget(self):
        # NFR-2, on the screen that gained three new reads this loop.
        for n in range(20):
            AIRun.objects.create(
                task_key=f"task.{n % 4}",
                provider="test",
                model="test-model",
                input_hash=f"{n:064d}",
                status=RunStatus.OK,
                cost="1.0000",
            )
        for n in range(6):
            FeatureFlag.objects.create(
                key=f"flag_{n}",
                is_enabled=True,
                expires_on=timezone.localdate() - dt.timedelta(days=n + 1),
            )

        self.client.force_login(self.support)
        with (
            mock.patch.object(selectors, "queue_depths", return_value=BROKER_UP),
            CaptureQueriesContext(connection) as captured,
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(captured),
            25,
            f"The operations board issues {len(captured)} queries; NFR-2 allows 25.",
        )


class PlatformToolLinkTests(BoardFixture):
    def test_support_without_admin_access_is_offered_no_admin_links(self):
        response = self.as_support()

        self.assertEqual(response.context["tools"], [])
        self.assertNotContains(response, reverse("admin:ai_airun_changelist"))

    def test_an_admin_capable_operator_is_offered_the_tools_they_can_open(self):
        self.support.is_staff = True
        self.support.is_superuser = True
        self.support.save(update_fields=["is_staff", "is_superuser"])

        response = self.as_support()

        for name in (
            "admin:ai_airun_changelist",
            "admin:audit_auditlog_changelist",
            "admin:core_featureflag_changelist",
        ):
            with self.subTest(link=name):
                self.assertContains(response, reverse(name))

    def test_a_link_is_not_offered_for_a_tool_the_admin_would_refuse(self):
        from django.contrib.auth.models import Permission

        self.support.is_staff = True
        self.support.save(update_fields=["is_staff"])
        self.support.user_permissions.add(Permission.objects.get(codename="view_auditlog"))

        response = self.as_support()

        self.assertEqual([label for label, _url in response.context["tools"]], ["Audit log"])


class BoardMarkupTests(BoardFixture):
    def test_each_term_is_a_direct_child_of_its_definition_group(self):
        # axe `dlitem`/`definition-list`: a `dt` two divs deep inside the `dl`
        # is not a definition group to a screen reader (the ANA-03 pattern,
        # found again on this board by the OPS-01 scan).
        import re

        body = self.as_support().content.decode()

        for block in re.findall(r"<dl\b.*?</dl>", body, flags=re.S):
            self.assertNotIn('<div class="card-body">', block)
