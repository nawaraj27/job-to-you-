"""
Celery application.

Queues are declared here rather than inferred, because the whole point of the
split is isolation: a burst of PDF renders or a slow AI provider must not be
able to consume the capacity that ordinary background work needs (SRS §17.5).

    default   short work with no special resource profile
    ai        provider calls — slow, rate-limited, expensive
    render    PDF/DOCX generation — CPU-bound
    files     scanning, checksums, purges — IO-bound
    email     outbound delivery
    match     scoring batches

Tasks declare their queue explicitly (DEVELOPER.md §3.9); a task with no queue
lands on `default`, which is the safe place for it to be noticed.
"""

import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import before_task_publish, task_postrun, task_prerun
from kombu import Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("careerai")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

QUEUES = ("default", "ai", "render", "files", "email", "match")

app.conf.task_queues = tuple(Queue(name, routing_key=name) for name in QUEUES)
app.conf.task_default_routing_key = "default"


# --- scheduled work ------------------------------------------------------
#
# Retention promises are kept by a schedule or they are not kept at all. Both
# entries here delete data the platform has told someone it would delete:
# unclaimed guest CVs after 24 hours (SRS §5.5.2, ADR-A32), and the bytes of
# attachments already soft-deleted or expired (SRS §14).
app.conf.beat_schedule = {
    "sweep-unclaimed-resume-uploads": {
        "task": "apps.resumes.tasks.sweep_unclaimed_uploads",
        # Hourly rather than daily: the window is 24 hours, and a daily sweep
        # makes the real retention anywhere between 24 and 48.
        "schedule": crontab(minute=17),
        "options": {"queue": "default"},
    },
    "purge-deleted-attachments": {
        "task": "apps.files.tasks.purge_attachments",
        "schedule": crontab(hour=3, minute=30),
        "options": {"queue": "files"},
    },
    "expire-due-jobs": {
        "task": "apps.jobs.tasks.expire_due_jobs",
        # Hourly. The gap between the closing date an employer set and the
        # moment the posting stops taking applications is a promise the
        # listing makes on screen; a daily sweep makes it up to 24 hours wide.
        "schedule": crontab(minute=7),
        "options": {"queue": "default"},
    },
    "run-daily-job-alerts": {
        "task": "apps.jobs.tasks.run_job_alerts",
        "kwargs": {"frequency": "daily"},
        # Early morning local time: an alert that lands at 03:00 is read at
        # breakfast, and one that lands at 18:00 competes with the evening.
        "schedule": crontab(hour=6, minute=0),
        "options": {"queue": "default"},
    },
    "expire-due-offers": {
        "task": "apps.applications.tasks.expire_due_offers",
        # Hourly. An offer that says "expires Friday" and is still acceptable
        # on Monday has taught the candidate that the date was decoration.
        "schedule": crontab(minute=23),
        "options": {"queue": "default"},
    },
    "sweep-stale-applications": {
        "task": "apps.applications.tasks.sweep_stale_applications",
        # Counts; closes nothing. Auto-rejecting on an employer's behalf is not
        # a thing this platform may do (FR-6.7).
        "schedule": crontab(hour=7, minute=30),
        "options": {"queue": "default"},
    },
    "requeue-stalled-scans": {
        "task": "apps.files.tasks.requeue_stalled_scans",
        # A lost scan task left a file "pending" for ever (STALL-01).
        "schedule": crontab(minute="*/10"),
        "options": {"queue": "files"},
    },
    "recover-stalled-resume-work": {
        "task": "apps.resumes.tasks.recover_stalled_resume_work",
        "schedule": crontab(minute="5-59/10"),
        "options": {"queue": "default"},
    },
    "recover-unscored-interviews": {
        "task": "apps.interviews.tasks.recover_unscored_sessions",
        # Offset from the CV recovery sweep so the two do not queue together.
        "schedule": crontab(minute="8-59/10"),
    },
    "run-notification-fanout": {
        "task": "apps.notifications.tasks.run_fanout",
        # Every minute. Stage two of A25 is where the rows actually get
        # written, and it has to be able to fall behind without holding the
        # outbox — or anything else — up.
        "schedule": crontab(minute="*"),
        "options": {"queue": "default"},
    },
    "deliver-webhooks": {
        "task": "apps.notifications.tasks.deliver_webhooks",
        "schedule": crontab(minute="*/2"),
        "options": {"queue": "default"},
    },
    "send-weekly-digests": {
        "task": "apps.notifications.tasks.send_digests",
        # Monday morning, on the email queue: a digest run is thousands of
        # messages at once and must not sit in front of a password reset.
        "schedule": crontab(hour=8, minute=0, day_of_week=1),
        "options": {"queue": "email"},
    },
    "purge-ai-run-snapshots": {
        "task": "apps.ai.tasks.purge_run_snapshots",
        # The run rows stay — cost history and the eval corpus outlive the text
        # that produced them. Only the personal data in the snapshot goes.
        "schedule": crontab(hour=4, minute=15),
        "options": {"queue": "default"},
    },
    "run-weekly-job-alerts": {
        "task": "apps.jobs.tasks.run_job_alerts",
        "kwargs": {"frequency": "weekly"},
        "schedule": crontab(hour=6, minute=15, day_of_week=1),
        "options": {"queue": "default"},
    },
    # --- analytics (P17) --------------------------------------------------
    #
    # Built in P17 and left unscheduled until P19, which meant a dashboard
    # whose rollups nothing computed. Ordered deliberately: roll up the day
    # first, then measure the four steering questions against the rows that
    # roll-up just wrote.
    "roll-up-yesterday": {
        "task": "apps.analytics.tasks.run_rollups",
        # After midnight local time, on the day that has actually finished.
        "schedule": crontab(hour=1, minute=10),
        "options": {"queue": "default"},
    },
    "measure-steering": {
        "task": "apps.analytics.tasks.measure_steering",
        "schedule": crontab(hour=1, minute=40),
        "options": {"queue": "default"},
    },
    "prune-analytics-events": {
        "task": "apps.analytics.tasks.prune_events",
        "schedule": crontab(hour=3, minute=50),
        "options": {"queue": "default"},
    },
    # --- billing (P18) ----------------------------------------------------
    #
    # The order is the point, and it is stated in `apps/billing/tasks.py`:
    # renew what is due, chase what is unpaid, end what was cancelled, end
    # what was never paid. A subscription that is both cancelled and overdue
    # then ends once, as a cancellation, which is the truthful reason.
    "renew-subscriptions": {
        "task": "apps.billing.tasks.renew_due",
        "schedule": crontab(hour=2, minute=0),
        "options": {"queue": "default"},
    },
    "chase-overdue-invoices": {
        "task": "apps.billing.tasks.chase_overdue",
        "schedule": crontab(hour=2, minute=15),
        "options": {"queue": "default"},
    },
    "end-cancelled-subscriptions": {
        "task": "apps.billing.tasks.end_cancelled",
        "schedule": crontab(hour=2, minute=30),
        "options": {"queue": "default"},
    },
    "end-unpaid-subscriptions": {
        "task": "apps.billing.tasks.end_unpaid",
        "schedule": crontab(hour=2, minute=45),
        "options": {"queue": "default"},
    },
    # ADR-A58: a gateway payment whose confirmation never arrived (browser
    # closed, status service down) is asked about again until it settles.
    # ADR-A59: a plan chosen and never paid for is closed with its invoice.
    # docs/CV-Lifecycle.md: unsaved drafts abandoned past their retention.
    "purge-abandoned-cv-drafts": {
        "task": "apps.resumes.tasks.purge_abandoned_drafts",
        "schedule": crontab(hour=3, minute=20),
        "options": {"queue": "default"},
    },
    "expire-unpaid-checkouts": {
        "task": "apps.billing.tasks.expire_checkouts",
        "schedule": crontab(hour=2, minute=50),
        "options": {"queue": "default"},
    },
    "reconcile-gateway-payments": {
        "task": "apps.billing.tasks.reconcile_payments",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "default"},
    },
    "prune-usage-counters": {
        "task": "apps.billing.tasks.prune_usage",
        "schedule": crontab(hour=4, minute=45, day_of_month=1),
        "options": {"queue": "default"},
    },
    # --- retention and erasure (P19) --------------------------------------
    #
    # Every entry below deletes data the platform has told somebody it would
    # delete (SRS §15.3). `apps/common/retention.py` is the table these keep.
    "run-due-erasures": {
        "task": "apps.accounts.tasks.run_due_erasures",
        # Hourly, so "fourteen days" means fourteen days rather than up to
        # fifteen. It is the promise with the least room for slack in it.
        "schedule": crontab(minute=41),
        "options": {"queue": "default"},
    },
    "resume-interrupted-erasures": {
        "task": "apps.accounts.tasks.resume_interrupted_erasures",
        # A worker killed mid-erasure leaves an account half-walked and gated.
        # This finishes it rather than waiting for somebody to notice.
        "schedule": crontab(hour=5, minute=5),
        "options": {"queue": "default"},
    },
    "purge-expired-renders": {
        "task": "apps.resumes.tasks.purge_expired_renders",
        # Before `purge-deleted-attachments` at 03:30, so a render released
        # tonight has its bytes removed tonight rather than tomorrow.
        "schedule": crontab(hour=3, minute=5),
        "options": {"queue": "default"},
    },
    "purge-soft-deleted": {
        "task": "apps.common.tasks.purge_soft_deleted",
        "schedule": crontab(hour=3, minute=10),
        "options": {"queue": "default"},
    },
    "purge-old-transcripts": {
        "task": "apps.interviews.tasks.purge_old_transcripts",
        "schedule": crontab(hour=4, minute=30, day_of_week=0),
        "options": {"queue": "default"},
    },
    "purge-closed-applications": {
        "task": "apps.applications.tasks.purge_closed_applications",
        "schedule": crontab(hour=4, minute=50, day_of_week=0),
        "options": {"queue": "default"},
    },
    "report-expired-flags": {
        "task": "apps.core.tasks.report_expired_flags",
        # Weekly, per §17: a flag that outlives its feature is a permanent dead
        # branch, and the report is what stops it becoming one.
        "schedule": crontab(hour=8, minute=30, day_of_week=1),
        "options": {"queue": "default"},
    },
}


# --- correlation context across the process boundary (ADR-A37) ----------
#
# A ContextVar does not survive a process boundary, so propagation is explicit:
# the publisher writes the envelope into task headers, the worker restores it,
# and the worker CLEARS it afterwards. The clear is the part that matters —
# without it a value set by one task persists into the next task on the same
# worker, and one user's work is logged under another user's identifiers.


@before_task_publish.connect
def _attach_log_context(headers=None, **kwargs):
    from apps.common.logging import HEADER_KEY, get_context

    if headers is not None:
        context = get_context().as_dict()
        if context:
            headers[HEADER_KEY] = context


@task_prerun.connect
def _restore_log_context(task_id=None, task=None, **kwargs):
    from apps.common.logging import HEADER_KEY, set_context

    carried = {}
    request = getattr(task, "request", None)
    if request is not None:
        carried = getattr(request, HEADER_KEY, None) or {}
        if not carried:
            headers = getattr(request, "headers", None) or {}
            carried = headers.get(HEADER_KEY) or {}

    set_context(**{**carried, "task_id": task_id})


@task_postrun.connect
def _clear_log_context(**kwargs):
    from apps.common.logging import clear

    clear()


@app.task(bind=True, queue="default")
def ping(self) -> str:
    """Liveness probe for the worker itself. Used by readiness checks and CI."""
    return "pong"
