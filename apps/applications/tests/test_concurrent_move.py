"""
Two people moving the same applicant at the same moment.

Each request loads the application at its old stage, so without a row lock both
passed the "already at" check, both wrote a timeline entry and the candidate was
told twice. `TransactionTestCase`, because the defence is a row lock and a lock
cannot be observed from inside one shared transaction.
"""

from __future__ import annotations

import threading

from django.core.management import call_command
from django.db import connections
from django.test import TransactionTestCase

from apps.accounts.tests.factories import create_user
from apps.applications.models import Application, ApplicationEvent
from apps.applications.services import pipeline as pipeline_service
from apps.applications.tests.factories import submit
from apps.candidates.tests.factories import create_profile
from apps.common.errors import DomainError
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.notifications.models import OutboxEvent


class ConcurrentMoveTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        self.owner = create_user()
        self.company, self.membership = company_with_owner(self.owner)
        self.job = publish(create_job(self.company))
        self.application = submit(job=self.job, profile=create_profile())

    def race(self, keys: list[str]) -> tuple[list[str], list[str]]:
        """Both requests have loaded the application before either moves it."""
        barrier = threading.Barrier(len(keys))
        moved: list[str] = []
        refused: list[str] = []
        errors: list[BaseException] = []

        def attempt(key: str) -> None:
            try:
                loaded = Application.objects.get(pk=self.application.pk)
                stage = pipeline_service.stage_by_key(self.company, key)
                barrier.wait(timeout=5)
                pipeline_service.move(
                    application=loaded, actor=self.owner, membership=self.membership, stage=stage
                )
                moved.append(key)
            except DomainError as exc:
                refused.append(exc.message)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(key,)) for key in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        return moved, refused

    def stage_changes(self):
        return ApplicationEvent.objects.filter(
            application=self.application, type="application.stage_changed"
        ).order_by("created_at", "pk")

    def test_two_identical_moves_tell_the_candidate_once(self):
        moved, refused = self.race(["reviewing", "reviewing"])

        self.assertEqual(moved, ["reviewing"])
        self.assertEqual(refused, ["This application is already at Reviewing."])
        self.assertEqual(self.stage_changes().count(), 1)
        self.assertEqual(OutboxEvent.objects.filter(type="application.stage_changed").count(), 1)

    def test_two_different_moves_are_recorded_in_the_order_they_happened(self):
        moved, refused = self.race(["reviewing", "shortlisted"])

        self.assertEqual(sorted(moved), ["reviewing", "shortlisted"])
        self.assertEqual(refused, [])
        first, second = self.stage_changes()
        # The second move starts from where the first one left the applicant,
        # not from the stage both requests saw when they loaded it.
        self.assertEqual(first.from_stage.key, "new")
        self.assertEqual(second.from_stage.key, first.to_stage.key)
        self.application.refresh_from_db()
        self.assertEqual(self.application.stage_id, second.to_stage_id)
