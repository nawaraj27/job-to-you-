"""
The soft-delete purge (ADR-A12, SRS §5.16 rule 5).

**"Soft delete" without a purge means "grows forever"** — and, after somebody
has asked to be erased, it means we told them we deleted something and did not.
A12 lists the eight tables that carry `deleted_at`; this job is the other half
of that decision.

The table list is written out rather than discovered by scanning the model
registry. A model that gains `deleted_at` next year must be a deliberate
addition here — a purge job that finds its own targets is a job that will one
day find one nobody meant it to.

`Attachment` is deliberately absent: its bytes live in object storage and its
purge verifies removal from the bucket, which is `files.services.purge`'s job
and not a `.delete()` in a loop.
"""

from __future__ import annotations

import datetime as dt
import logging

from celery import shared_task

logger = logging.getLogger(__name__)

#: `(app_label.ModelName, why it is safe to remove)`. Every model that actually
#: carries `deleted_at`, minus `Attachment`, which has its own purge because of
#: the bucket.
#:
#: **A correction of record.** ADR-A12 and SRS §5.1.9 list `Tag` among the
#: soft-deleted tables. It is not one and never was — a tag is a label with a
#: unique constraint per company, and soft-deleting it would mean a company
#: could not re-create a tag it had deleted. `CompanyCandidateNote` *is* soft
#: deleted and is absent from that list. Both are recorded in the SRS rather
#: than quietly reconciled here.
PURGEABLE: tuple[tuple[str, str], ...] = (
    ("resumes.ResumeDocument", "The versions it froze are referenced by applications and survive."),
    ("jobs.Job", "A closed posting's applications hold their own job snapshot."),
    ("companies.Company", "Only reachable once every membership has gone."),
    ("companies.CompanyMembership", "Historic membership is in the audit log."),
    ("applications.ApplicationNote", "Internal and company-owned."),
    ("applications.CompanyCandidateNote", "Internal and company-owned."),
    ("applications.TalentPoolMember", "Pool membership is not evidentiary."),
    ("messaging.Message", "The conversation shell survives."),
)


@shared_task(queue="default")
def purge_soft_deleted(days: int | None = None) -> dict[str, int]:
    """Delete rows soft-deleted longer ago than the retention window.

    Per model rather than in one query, because the cascades differ and a
    failure on one table must not leave the others unswept. Counts are returned
    per table so the log line answers "what did last night actually remove".
    """
    from django.apps import apps as django_apps
    from django.utils import timezone

    from .retention import SOFT_DELETED_DAYS

    cutoff = timezone.now() - dt.timedelta(days=days if days is not None else SOFT_DELETED_DAYS)
    removed: dict[str, int] = {}

    for label, _reason in PURGEABLE:
        model = django_apps.get_model(label)
        doomed = model.objects.filter(deleted_at__lte=cutoff)

        # Counted before the delete, because `.delete()` returns the total
        # across cascades — one document plus its draft reads as "2 documents",
        # which is the kind of number somebody later quotes in an incident
        # review.
        count = doomed.count()
        if count:
            doomed.delete()
            removed[label] = count

    if removed:
        logger.info("purged soft-deleted rows", extra={"removed": removed})
    return removed
