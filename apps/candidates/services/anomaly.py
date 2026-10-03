"""
Anomaly detection on employer-side candidate access (§15.4, FR-9.4).

What this is for, stated plainly: **a scraper holding a paid employer account.**
Not a hostile stranger — those are stopped by authentication — but a signed-up,
paying company harvesting the candidate corpus. From the inside that looks like
ordinary use at an extraordinary rate, so what is measured is rate, and the
comparison is against what a real hiring team does in a day.

**It reports; it does not block.** Every check emits an event and writes an
audit row, and nothing here refuses a request. That is deliberate:

  * a false positive that blocks a real recruiter during a hiring push is a
    support incident and a lost customer, while a false positive that raises a
    flag costs somebody two minutes of reading;
  * the response to a confirmed scraper is a human decision — suspend the
    account, revoke the grant, call them — and none of those belong in a
    request cycle;
  * a blocking rule teaches an attacker exactly where the threshold is, because
    they can watch for the moment it trips. A reporting rule tells them nothing.

The hard limits that *do* refuse — the export cap, the grant requirement — live
in `talent.py`, where they are business rules rather than detection.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.audit import services as audit

from ..models import ProfileView

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import Company

logger = logging.getLogger(__name__)

#: Profile reads by one company in a day beyond which the rate stops looking
#: like hiring. A team of five recruiters working hard opens perhaps eighty
#: profiles between them; two hundred is a different activity.
DAILY_VIEW_THRESHOLD = 200

#: Rows exported by one company in a day beyond which the same applies. Lower
#: than the view threshold because an export is a copy that leaves, and a
#: hundred is already the per-request cap.
DAILY_EXPORT_THRESHOLD = 300

#: How many exports in a day is itself a signal, regardless of size. Somebody
#: paging a shortlist out in ten-row batches to stay under a cap is a shape
#: worth noticing.
DAILY_EXPORT_COUNT_THRESHOLD = 10


@dataclass(frozen=True)
class Signal:
    """One thing worth a person looking at.

    `severity` is `notice` or `alert`, and the difference is what happens next
    rather than how the number was reached: a notice goes on a staff queue, an
    alert pages somebody. Both are read by a human, which is the whole design.
    """

    kind: str
    severity: str
    detail: str
    observed: int
    threshold: int


def check_export(*, company: Company, actor: User, count: int) -> list[Signal]:
    """Called after every export (FR-9.4).

    Three questions: was this export large, has today's total become large, and
    are there a suspicious number of small exports. The third is the one that
    catches somebody working around the per-request cap.
    """
    since = _start_of_day()
    signals: list[Signal] = []

    exported_today = _exported_today(company, since=since)
    exports_today = _export_count_today(company, since=since)

    if exported_today >= DAILY_EXPORT_THRESHOLD:
        signals.append(
            Signal(
                kind="export_volume",
                severity="alert",
                detail=f"{exported_today} candidate rows exported today.",
                observed=exported_today,
                threshold=DAILY_EXPORT_THRESHOLD,
            )
        )
    if exports_today >= DAILY_EXPORT_COUNT_THRESHOLD:
        signals.append(
            Signal(
                kind="export_frequency",
                severity="notice",
                detail=(
                    f"{exports_today} separate exports today — the shape of somebody "
                    "batching around the per-export limit."
                ),
                observed=exports_today,
                threshold=DAILY_EXPORT_COUNT_THRESHOLD,
            )
        )

    _report(signals, company=company, actor=actor, verb="candidate.export_anomaly")
    return signals


def check_viewing(*, company: Company, actor: User) -> list[Signal]:
    """Called after a profile view, and by the nightly sweep.

    Cheap enough to run inline: one indexed count over a day of one company's
    rows, which the `profileview_company_idx` index serves directly.
    """
    since = _start_of_day()
    viewed = ProfileView.objects.filter(company=company, created_at__gte=since).count()

    signals: list[Signal] = []
    if viewed >= DAILY_VIEW_THRESHOLD:
        signals.append(
            Signal(
                kind="view_volume",
                severity="alert",
                detail=f"{viewed} candidate profiles opened today.",
                observed=viewed,
                threshold=DAILY_VIEW_THRESHOLD,
            )
        )

    _report(signals, company=company, actor=actor, verb="candidate.view_anomaly")
    return signals


def sweep(*, days: int = 1) -> dict[str, int]:
    """Check every company that has been active. For the scheduled job.

    The inline checks catch a company crossing a threshold as it happens; this
    catches one that crossed it while nothing was watching — a long export
    running overnight, or a company that stopped just under the line every day
    for a week.
    """
    from apps.companies.models import Company

    since = timezone.now() - dt.timedelta(days=days)
    company_ids = (
        ProfileView.objects.filter(created_at__gte=since)
        .values_list("company_id", flat=True)
        .distinct()
    )

    checked = flagged = 0
    for company in Company.objects.filter(pk__in=list(company_ids)):
        checked += 1
        if check_viewing(company=company, actor=None):  # type: ignore[arg-type]
            flagged += 1

    return {"checked": checked, "flagged": flagged}


# --- internals ------------------------------------------------------------


def _report(signals: list[Signal], *, company: Company, actor: User | None, verb: str) -> None:
    """Record and announce. Never raises into the caller.

    An anomaly check that could fail an export would mean the detection
    controlled the feature, and the first flaky query would take candidate
    export down for everybody.
    """
    if not signals:
        return

    for signal in signals:
        logger.warning(
            "candidate access anomaly: %s for company %s (%s >= %s)",
            signal.kind,
            company.pk,
            signal.observed,
            signal.threshold,
        )
        audit.record(
            verb=verb,
            actor=actor,
            target=company,
            company=company,
            metadata={
                "kind": signal.kind,
                "severity": signal.severity,
                "observed": signal.observed,
                "threshold": signal.threshold,
            },
        )

    from apps.notifications import events

    events.emit(
        "candidate.access_anomaly",
        company,
        payload={
            "company_id": company.pk,
            "company_slug": company.slug,
            # Kinds and counts. Never which candidates: an alert about somebody
            # reading a list of people should not itself carry the list.
            "signals": [
                {"kind": signal.kind, "severity": signal.severity, "observed": signal.observed}
                for signal in signals
            ],
        },
    )


def _start_of_day() -> dt.datetime:
    """Midnight in the platform's local time (`TIME_ZONE`, Asia/Kathmandu).

    It was UTC midnight, which is 05:45 in Kathmandu: a company's "day" split
    its working morning in two, so views before and after quarter to six were
    counted against different days and a threshold could be straddled.
    """
    local = timezone.localtime(timezone.now())
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _exported_today(company: Company, *, since: dt.datetime) -> int:
    """Rows exported today, from the audit log.

    Read from `AuditLog` rather than from a counter of its own: the audit row is
    written whatever happens and is append-only, so it cannot disagree with what
    occurred — and a second counter would be the thing that drifts.
    """
    from apps.audit.models import AuditLog

    rows = AuditLog.objects.filter(
        verb="candidate.exported", company=company, created_at__gte=since
    ).values_list("metadata", flat=True)
    return sum(int((row or {}).get("count", 0)) for row in rows)


def _export_count_today(company: Company, *, since: dt.datetime) -> int:
    from apps.audit.models import AuditLog

    return AuditLog.objects.filter(
        verb="candidate.exported", company=company, created_at__gte=since
    ).count()
