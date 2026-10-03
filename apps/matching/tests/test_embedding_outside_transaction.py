"""
An embedding task works outside a test transaction (EMB-01).

Found in the Celery worker log during the 2026-09-21 verification pass: every
`embed_job` task raised "events.emit('ai.run_completed') was called outside a
transaction". `runner.embed` wrote the run row and announced it without the
`atomic()` block `runner.run` has, and `events.emit` refuses to write an event
outside one. No vector was ever stored by a worker.

The rest of the suite could not see it: a `TestCase` wraps every test in a
transaction, so the check always passed. This file is a `TransactionTestCase`
— autocommit, like a worker — which is the only way to reproduce it.
"""

from __future__ import annotations

from django.test import TransactionTestCase

from apps.ai.models import AIRun, RunStatus
from apps.ai.providers.fake import FakeAdapter
from apps.companies.tests.factories import create_company
from apps.jobs.tests.factories import create_job, publish
from apps.matching.services import embeddings
from apps.notifications.models import OutboxEvent

from .test_embeddings import fake_ai


class EmbeddingOutsideTransactionTests(TransactionTestCase):
    def setUp(self):
        FakeAdapter.reset()
        self.job = publish(
            create_job(
                create_company(), title="Backend Developer", description="Python and Django."
            )
        )

    def test_embedding_a_job_in_autocommit_stores_the_run_and_its_event(self):
        with fake_ai():
            stored = embeddings.embed_job(self.job)

        self.assertTrue(stored)
        run = AIRun.objects.filter(task_key__startswith="embed").latest("pk")
        self.assertEqual(run.status, RunStatus.OK)
        self.assertTrue(
            OutboxEvent.objects.filter(
                type="ai.run_completed", payload__run_public_id=str(run.public_id)
            ).exists()
        )
