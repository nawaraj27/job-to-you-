"""
Background work for ingestion.

Tasks load arguments, call one service and handle retries. Every rule about
what may be processed lives in the service, so a task cannot be the place
somebody bypasses ADR-A32 by accident.
"""

from __future__ import annotations

import logging

from celery import shared_task

from apps.common.errors import InvalidState

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(OSError,),
    retry_backoff=True,
)
def parse_upload(self, upload_id: int) -> str:
    """Read one claimed upload."""
    from .models import ResumeUpload
    from .services import parse as parse_service
    from .services import proposals as proposal_service

    upload = (
        ResumeUpload.objects.select_related("attachment", "profile").filter(pk=upload_id).first()
    )
    if upload is None:
        # Deleted between queueing and running. Not an error: the sweep and the
        # candidate's own delete button both do exactly this.
        return "gone"

    try:
        parse = parse_service.parse_upload(upload=upload)
    except InvalidState as exc:
        # A refusal, not a failure — an unclaimed or unscanned upload is a
        # state, and retrying would not change it.
        logger.info("parse refused for %s: %s", upload_id, exc.message)
        return "refused"

    if upload.profile is not None:
        proposal_service.build_from_parse(parse=parse, profile=upload.profile)
    return "parsed"


@shared_task
def sweep_unclaimed_uploads() -> int:
    """Delete guest uploads past their retention window (SRS §5.5.2)."""
    from .services import claim as claim_service

    return claim_service.sweep_unclaimed()


@shared_task(
    queue="render",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
)
def render_version(self, version_id: int, fmt: str = "pdf", template_id: int | None = None) -> str:
    """Render one version off the request path (SRS §17.5).

    On the `render` queue with its own worker and concurrency cap, because
    PDF generation is CPU-heavy and would otherwise eat the same budget as the
    300 ms page target. Idempotent: the render cache means a repeat run returns
    the existing file rather than producing a second one.
    """
    from .models import ResumeTemplate, ResumeVersion
    from .services import renders as render_service

    version = (
        ResumeVersion.objects.select_related("document", "document__template")
        .filter(pk=version_id)
        .first()
    )
    if version is None:
        return "gone"

    template = ResumeTemplate.objects.filter(pk=template_id).first() if template_id else None

    try:
        record = render_service.render_version(version=version, fmt=fmt, template=template)
    except InvalidState as exc:
        # A refusal, not a failure: an unrenderable version is a state, and
        # retrying will not change it.
        logger.info("render refused for version %s: %s", version_id, exc.message)
        return "refused"

    return f"rendered:{record.pk}"


@shared_task(queue="ai", bind=True, max_retries=2, default_retry_delay=30)
def enrich_analysis(self, analysis_id: int, actor_id: int | None = None) -> str:
    """Add the model's half to a report the candidate is already reading.

    Queued the moment a report is written. The score, the checks and the
    measured findings were returned in the same request; this is the verdict
    and the judgement findings, which take a minute and a half on a modest
    model and which nobody should have to sit and watch.

    Retried twice, because the failure mode is a busy provider rather than a
    bad input — and after that the report simply stays as the deterministic
    one, which is a complete report (SRS §9.3.6).
    """
    from apps.accounts.models import User

    from .models import ResumeAnalysis
    from .services import analysis as analysis_service

    analysis = (
        ResumeAnalysis.objects.select_related("version__document__profile", "job")
        .filter(pk=analysis_id)
        .first()
    )
    if analysis is None:
        return "gone"

    actor = User.objects.filter(pk=actor_id).first() if actor_id else None
    try:
        enriched = analysis_service.enrich(analysis=analysis, actor=actor)
    except InvalidState:
        # A refusal is a decision, not a transient fault. Retrying it would
        # ask the same question and get the same answer.
        return "refused"

    return enriched.enrichment


@shared_task(queue="ai", bind=True, max_retries=1, default_retry_delay=30)
def analyse_version(
    self,
    version_id: int,
    actor_id: int | None = None,
    target_role: str = "",
    job_id: int | None = None,
) -> str:
    """Analyse one frozen version off the request path (SRS §9.3.3).

    On the `ai` queue rather than `default`, because a provider having a bad
    afternoon must not consume the capacity ordinary background work needs.

    Ids rather than objects: a Celery argument is JSON, and a serialised model
    is a stale copy of a row that may have changed by the time a worker picks
    it up.
    """
    from apps.accounts.models import User
    from apps.jobs.models import Job

    from .models import ResumeVersion
    from .services import analysis as analysis_service

    version = (
        ResumeVersion.objects.select_related("document", "document__upload")
        .filter(pk=version_id)
        .first()
    )
    if version is None:
        return "gone"

    try:
        analysis = analysis_service.analyse_version(
            version=version,
            actor=User.objects.filter(pk=actor_id).first() if actor_id else None,
            target_role=target_role,
            job=Job.objects.filter(pk=job_id).first() if job_id else None,
        )
    except InvalidState as exc:
        # A refusal, not a failure: a redacted version is a state, and retrying
        # will not change it.
        logger.info("analysis refused for version %s: %s", version_id, exc.message)
        return "refused"

    return f"analysed:{analysis.pk}"


@shared_task(queue="ai")
def run_improvement_job(job_id: int) -> str:
    """Run one queued CV improvement job (`services.improvements`).

    On the `ai` queue because every job waits on a model. No automatic retry:
    the job records its own failure, and the candidate chooses to try again —
    a second model call they did not ask for is not free for anybody.
    """
    from .services import improvements

    return improvements.run(job_id)


@shared_task(queue="default")
def purge_expired_renders() -> int:
    """Remove cached renders past their retention (§15.3, `RENDER_DAYS`)."""
    from .services import renders

    return renders.purge_expired()


#: A read that has not finished in this long is asked for again.
STALLED_PARSE_MINUTES = 15
#: A written review still pending after this long is not coming: the task was
#: lost, or the worker died holding it. The report then offers the free retry.
STALLED_REVIEW_MINUTES = 30


@shared_task(queue="default")
def recover_stalled_resume_work() -> dict[str, int]:
    """Recover CV work whose task was lost (STALL-01).

    The screens read state from the database, which is right — but a task lost
    by the broker left that state "waiting" for ever: "Waiting to be parsed"
    on Uploads, "the written review is taking longer than usual" on the
    report, with nothing the candidate could do. This asks for a stalled read
    again, and turns a review that is not coming into a failed one, which the
    report offers to retry for free.
    """
    import datetime as dt

    from django.utils import timezone

    from apps.files.constants import ScanStatus

    from .models import EnrichmentState, ParseStatus, ResumeAnalysis, ResumeUpload

    now = timezone.now()
    reads = list(
        ResumeUpload.objects.owned()
        .filter(
            parse_status__in=(ParseStatus.PENDING, ParseStatus.RUNNING),
            attachment__scan_status=ScanStatus.CLEAN,
            updated_at__lt=now - dt.timedelta(minutes=STALLED_PARSE_MINUTES),
        )
        .values_list("pk", flat=True)[:500]
    )
    for upload_id in reads:
        parse_upload.delay(upload_id)

    reviews = ResumeAnalysis.objects.filter(
        enrichment=EnrichmentState.PENDING,
        created_at__lt=now - dt.timedelta(minutes=STALLED_REVIEW_MINUTES),
    ).update(enrichment=EnrichmentState.FAILED, updated_at=now)

    from .services import improvements

    return {"reads": len(reads), "reviews": reviews, "jobs": improvements.fail_stalled()}


@shared_task(queue="default")
def purge_abandoned_drafts() -> int:
    """Remove unsaved CV drafts left untouched past the retention period."""
    from .services import documents

    return documents.purge_abandoned_drafts()
