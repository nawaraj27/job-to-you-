"""
Platform-wide reads for the operations board (SRS §17.4, ADR-A5).

One function, deliberately: the board asks the questions §17.4 names as alerts
and they live in five apps, a broker and a storage backend. Answering them in
the view would put five querysets and two network calls in a template's context
builder. Gathered here so the numbers have one definition — the same one an
alert would use.

Nothing here is tenant-scoped, and that is correct: the reader is platform
staff looking at the platform. Every *product* read stays in its own app's
selectors, scoped to a company or a profile.

**Nothing here may raise.** This is the screen somebody opens when something is
already broken, and a board that 500s because the broker is down has failed at
the one moment it exists for. Every dependency read reports its own failure as
a value.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)


def operations_snapshot() -> dict[str, Any]:
    """What is running, what is late, and what is owed.

    In the order they predict user pain (§17.4): the outbox first, because
    every side effect in the platform flows through it and a stalled one looks
    healthy from the outside for about twenty minutes.
    """
    from apps.accounts.models import User
    from apps.analytics.models import AICostDaily
    from apps.core.models import FeatureFlag
    from apps.notifications.models import OutboxEvent

    waiting = OutboxEvent.objects.filter(processed_at__isnull=True)
    oldest = waiting.order_by("created_at").values_list("created_at", flat=True).first()

    return {
        "outbox_pending": waiting.count(),
        "outbox_oldest": oldest,
        "expired_flags": list(FeatureFlag.objects.expired().values_list("key", flat=True)),
        "ai_yesterday": list(
            AICostDaily.objects.filter(day=timezone.localdate() - dt.timedelta(days=1))
        ),
        "ai_today": ai_today(),
        "queues": queue_depths(),
        "dependencies": dependency_health(),
        "reference_data": reference_data(),
        "erasures_pending": User.objects.filter(
            erasure_requested_at__isnull=False, erased_at__isnull=True
        ).count(),
    }


def ai_today() -> dict[str, Any]:
    """AI spend and failures since local midnight, counted from the runs.

    `AICostDaily` is a rollup of a *finished* day, written at 01:10 — so during
    an incident at two in the afternoon it holds yesterday and nothing else,
    and §17.4 alerts on AI daily spend and AI error rate today. The board used
    to show only the rollup, which meant the one screen built for "what is AI
    costing" could not answer it about the day it was being asked on.

    Counted with the same expressions `analytics.services.rollups.roll_ai_cost`
    uses, so tonight's rollup and this morning's board cannot disagree about
    what a failure is.
    """
    from django.db.models import Count, Q, Sum

    from apps.ai.models import AIRun, RunStatus

    start = timezone.make_aware(dt.datetime.combine(timezone.localdate(), dt.time.min))
    counted = (
        AIRun.objects.filter(created_at__gte=start)
        .values("task_key")
        .annotate(
            runs=Count("id"),
            failures=Count("id", filter=~Q(status=RunStatus.OK)),
            cached=Count("id", filter=Q(cached_from__isnull=False)),
            cost=Sum("cost"),
        )
        .order_by("-cost", "task_key")
    )
    rows: list[dict[str, Any]] = [
        {
            **row,
            "cost": row["cost"] or Decimal("0"),
            # Shown as a share rather than left to a template to divide: a rate
            # computed in markup is a rate nothing can test.
            "failure_rate": round(row["failures"] / row["runs"], 4) if row["runs"] else 0.0,
        }
        for row in counted
    ]

    runs = sum(row["runs"] for row in rows)
    failures = sum(row["failures"] for row in rows)
    return {
        "rows": rows,
        "runs": runs,
        "failures": failures,
        "cost": sum((row["cost"] for row in rows), Decimal("0")),
        "failure_rate": round(failures / runs, 4) if runs else 0.0,
        "since": start,
    }


#: The board's own broker connection, reused across renders. Not the Celery
#: publish pool: that pool's retry policy is the right one for getting a task
#: onto a queue and the wrong one for answering a question about it, and the
#: difference is six seconds of a staff member's incident.
_broker: Any = None


def _broker_connection() -> Any:
    from django.conf import settings
    from kombu import Connection

    return Connection(
        settings.CELERY_BROKER_URL,
        connect_timeout=BROKER_TIMEOUT_SECONDS,
        transport_options={
            "socket_connect_timeout": BROKER_TIMEOUT_SECONDS,
            "socket_timeout": BROKER_TIMEOUT_SECONDS,
            "max_retries": 0,
        },
    )


def _release_broker() -> None:
    global _broker
    if _broker is not None:
        # Releasing a connection that is already dead is not news.
        with contextlib.suppress(Exception):
            _broker.release()
        _broker = None


#: How long the board waits for the broker before calling it unreachable.
#: Seconds, and short on purpose: the answer "the broker is down" is as useful
#: as a number, and a board that takes six seconds to say it is a board nobody
#: opens twice.
BROKER_TIMEOUT_SECONDS = 1.0


def queue_depths() -> dict[str, Any]:
    """How much work is waiting on each Celery queue (§17.4, "queue depth").

    Read from the broker rather than from a table, because the broker is where
    a backlog actually is: a queue that has stopped draining holds messages
    nothing in PostgreSQL knows about, and the outbox count beside it looks
    perfectly healthy while nothing is being delivered.

    Reported as a value, never raised, and bounded by `BROKER_TIMEOUT_SECONDS`.
    An unreachable broker is one of the states this board exists to show, and
    it is exactly the state in which an exception — or a six-second retry
    storm — would take the board down with it.

    `_size` rather than `queue_declare(passive=True)`: the AMQP declare is not
    how the Redis transport answers this, and asking it that way reports every
    queue as an error on the broker this platform actually runs.
    """
    from config.celery import QUEUES

    global _broker
    try:
        connection = _broker or _broker_connection()
        channel = connection.default_channel
        depths = {name: int(channel._size(name)) for name in QUEUES}
        # Kept only once it has answered, so a broken connection is never
        # handed to the next reader.
        _broker = connection
    except Exception as error:  # noqa: BLE001 - an incident board reports, never raises
        _release_broker()
        logger.warning("queue depth unavailable", extra={"error": type(error).__name__})
        return {"available": False, "error": type(error).__name__, "depths": {}, "total": 0}

    return {
        "available": True,
        "error": "",
        # Deepest first: the queue that is in trouble is the one to read.
        "depths": dict(sorted(depths.items(), key=lambda item: (-item[1], item[0]))),
        "total": sum(depths.values()),
    }


def dependency_health() -> list[dict[str, str]]:
    """Database, cache and private storage, as `/readyz` sees them (§17.4).

    The same three probes the readiness endpoint runs. A board that says
    "storage is fine" while the load balancer is taking the process out of
    rotation would send somebody looking in the wrong place during the only
    minutes that matter.
    """
    from apps.common import health

    return [
        {"label": probe.label, "key": probe.key, "status": status}
        for probe, status in health.check_all()
    ]


def reference_data() -> list[dict[str, Any]]:
    """The seeded rows the product cannot work without (DEPLOYMENT.md §2).

    Each is a management command somebody has to remember on a new database,
    and each fails far from its cause when forgotten: no interview template is
    a 404 on the booking page a candidate was just told to open, and no default
    plan is a `LookupError` on the first registration. Counted here so the
    board says which seed is missing instead of a candidate finding out.
    """
    from apps.billing.models import Plan
    from apps.core.models import LegalDocument
    from apps.interviews.models import InterviewKind, InterviewTemplate
    from apps.resumes.models import ResumeTemplate
    from apps.taxonomy.models import Skill

    rows = (
        ("CV templates", "seed_resume_templates", ResumeTemplate.objects.filter(is_active=True)),
        ("Plans", "seed_plans", Plan.objects.all()),
        ("Terms and privacy", "seed_legal", LegalDocument.objects.all()),
        (
            "Live interview template",
            "seed_interview_templates",
            InterviewTemplate.objects.filter(company__isnull=True, kind=InterviewKind.LIVE),
        ),
        ("Skills taxonomy", "import_taxonomy", Skill.objects.all()),
    )
    return [
        {"label": label, "command": command, "count": (count := queryset.count()), "ok": count > 0}
        for label, command, queryset in rows
    ]
