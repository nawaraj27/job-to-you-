"""
The staff surfaces (ADR-A5, SRS §19.5, §15.4).

A5 says Django admin for record CRUD and purpose-built screens only where
admin is weak — which is at *workflow*. Three screens, and each exists because
the admin genuinely cannot do it:

  * **Verification review**, because claim-and-lock, side-by-side documents and
    a decision with a reason are a workflow, not a change form (§19.5).
  * **The anomaly queue**, because "who is reading more candidate data than
    they should be" is a question over audit rows that no `list_display`
    answers, and it was deferred out of P15 precisely so it would arrive with
    an operator (§15.4, §21).
  * **The operations board**, because the questions asked during an incident —
    is the outbox draining, are the retention jobs keeping their promises, what
    is AI costing today — are spread across five tables and one settings file.

Everything mutating goes through the same services the rest of the platform
uses, so a staff action is audited and emits its events exactly like a user's.
No screen here writes a model directly.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View

from apps.accounts import staff
from apps.audit import selectors as audit_selectors
from apps.audit import services as audit
from apps.common import ratelimit, restrictions, retention
from apps.common.errors import DomainError
from apps.companies import selectors as company_selectors
from apps.companies.models import VerificationState
from apps.companies.services import company as company_service
from apps.core import selectors as platform_selectors
from apps.files import services as file_service
from apps.files import views as file_views

#: The audit verbs the anomaly queue reads. Named here rather than matched by
#: prefix, so a new verb appears on the queue only when somebody decided it
#: should.
ANOMALY_VERBS = ("candidate.export_anomaly", "candidate.view_anomaly")


@method_decorator(login_required, name="dispatch")
class StaffView(View):
    """Base: every staff screen states the level it needs."""

    level = "support"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated:
            staff.require(request.user, self.level)
        return super().dispatch(request, *args, **kwargs)

    def context(self, request: HttpRequest) -> dict:
        tabs = self._tabs(request)
        return {"staff_level": staff.level_of(request.user), "tabs": tabs}

    def _tabs(self, request: HttpRequest) -> list:
        # Only the screens this level can open: a support agent offered the
        # verification queue got a 403 for clicking a tab they were shown.
        wanted = [
            ("Verification", reverse("staff:verification"), "verified", "moderator"),
            ("Anomalies", reverse("staff:anomalies"), "shield", "moderator"),
            ("Operations", reverse("staff:operations"), "monitor", "support"),
            ("Companies", reverse("staff:companies"), "building", "support"),
            ("People", reverse("staff:people"), "users", "support"),
            ("Staff", reverse("staff:team"), "shield", "superadmin"),
        ]
        return [
            (label, url, icon)
            for label, url, icon, level in wanted
            if staff.at_least(request.user, level)
        ]


class VerificationQueueView(StaffView):
    """`/staff/verification/` — the review queue (§19.5)."""

    level = "moderator"

    def get(self, request: HttpRequest) -> HttpResponse:
        pending = list(company_selectors.verification_queue())
        recent = company_selectors.recently_reviewed()
        # Decision D-04: a reviewer who belongs to, is invited to, or submitted
        # for a company is not offered the claim, and is told why. The service
        # refuses it either way.
        conflicts = company_service.reviewer_conflicts(request.user, pending)
        for row in pending:
            code = conflicts.get(row.company_id)
            row.conflict = company_service.CONFLICT_REASONS[code] if code else ""
            # What the company was asked for and did not send. The queue
            # listed what arrived and nothing about what did not, so a
            # reviewer had to remember the required set and check it by eye
            # on every item — and "needs more information" is the decision
            # that depends on getting that right.
            row.missing_documents = [
                company_service.DOCUMENT_KINDS[kind]
                for kind in company_service.missing_document_kinds(row)
            ]

        return render(
            request,
            "staff/verification.html",
            {
                **self.context(request),
                "pending": pending,
                "recent": recent,
                # Value and label: `{{ state|title }}` printed "More_Info".
                "states": [
                    (state.value, state.label)
                    for state in (
                        VerificationState.VERIFIED,
                        VerificationState.MORE_INFO,
                        VerificationState.REJECTED,
                    )
                ],
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Claim, release, or decide. Every branch calls a service."""
        url = reverse("staff:verification")
        company = company_selectors.by_slug(request.POST.get("company", ""))
        if company is None:
            messages.error(request, "That company no longer exists.")
            return redirect(url)

        action = request.POST.get("action", "")
        try:
            if action == "claim":
                company_service.claim_verification(actor=request.user, company=company)
                messages.success(request, f"{company.name} is yours for the next 45 minutes.")
            elif action == "release":
                company_service.release_verification(actor=request.user, company=company)
                messages.success(request, "Back on the queue.")
            elif action == "decide":
                company_service.review_verification(
                    actor=request.user,
                    company=company,
                    decision=request.POST.get("decision", ""),
                    reason=request.POST.get("reason", "").strip(),
                )
                messages.success(request, f"{company.name} reviewed.")
            else:
                messages.error(request, "That is not something this screen can do.")
        except DomainError as error:
            restrictions.flash(request, error)

        return redirect(url)


class VerificationDocumentView(StaffView):
    """`/staff/verification/documents/<id>/` — open one submitted document.

    The review is "documents against company details" (§19.5), and until this
    existed the queue showed a document *count* and no way to read one: a
    reviewer could only verify blind. Verification documents are restricted
    data (§15.3), so every open is written to the file's access log and to
    the audit log before a byte is served.
    """

    level = "moderator"

    def get(self, request: HttpRequest, document_id: int) -> HttpResponseBase:
        document = company_selectors.verification_document(document_id)
        if document is None or not document.attachment.is_servable:
            raise Http404("That document does not exist, or cannot be opened yet.")

        company = document.verification.company
        file_service.record_access(
            attachment=document.attachment,
            actor=request.user,
            purpose="verification_review",
            ip=ratelimit.client_ip(request),
            request_id=getattr(request, "request_id", ""),
        )
        audit.record(
            verb="company.verification_document_opened",
            actor=request.user,
            target=company,
            company=company,
            metadata={"document_id": document.pk, "kind": document.kind},
        )
        return file_views.serve(document.attachment)


#: How each anomaly signal reads to a moderator. The raw kinds are codes.
SIGNAL_LABELS = {
    "export_volume": "Candidates exported today",
    "export_frequency": "Exports run today",
    "view_volume": "Candidate profiles opened today",
}


class AnomalyQueueView(StaffView):
    """`/staff/anomalies/` — who is reading more than they should be (§15.4).

    Reads audit rows rather than a table of its own. The signals are already
    written there by `candidates.services.anomaly`, and a second store would be
    a second answer to "what happened", diverging the first time one write
    succeeded and the other did not.
    """

    level = "moderator"

    def get(self, request: HttpRequest) -> HttpResponse:
        days = _window(request.GET.get("days"))
        since = timezone.now() - dt.timedelta(days=days)

        rows = list(audit_selectors.by_verbs(ANOMALY_VERBS, since=since))
        for row in rows:
            kind = (row.metadata or {}).get("kind", "")
            row.signal_label = SIGNAL_LABELS.get(kind, kind)  # type: ignore[attr-defined]

        return render(
            request,
            "staff/anomalies.html",
            {
                **self.context(request),
                "rows": rows,
                "days": days,
                "windows": (1, 7, 30),
                "alerts": sum(1 for row in rows if row.metadata.get("severity") == "alert"),
            },
        )


class OperationsView(StaffView):
    """`/staff/operations/` — the board somebody opens during an incident."""

    level = "support"

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(
            request,
            "staff/operations.html",
            {
                **self.context(request),
                **platform_selectors.operations_snapshot(),
                "retention": retention.SCHEDULE,
                "tools": _platform_tools(request.user),
            },
        )


#: The §3.12 platform tools that live in Django admin, and the permission that
#: opens each. The board is where somebody starts during an incident; "where is
#: the run inspector" should not be the second question.
PLATFORM_TOOLS = (
    ("AI run inspector", "admin:ai_airun_changelist", "ai.view_airun"),
    ("Audit log", "admin:audit_auditlog_changelist", "audit.view_auditlog"),
    ("Feature flags", "admin:core_featureflag_changelist", "core.view_featureflag"),
)


def _platform_tools(user) -> list[tuple[str, str]]:
    """Only the tools this person's admin permissions actually open.

    `StaffRole` opens this board and `is_staff` plus model permissions open the
    admin — two authorities by design (ADR-A5). A link that answers 403 would
    be the board claiming access the admin then refuses.
    """
    if not getattr(user, "is_staff", False):
        return []
    return [(label, reverse(name)) for label, name, perm in PLATFORM_TOOLS if user.has_perm(perm)]


def _window(value: str | None) -> int:
    try:
        days = int(value or 7)
    except (TypeError, ValueError):
        return 7
    return days if days in (1, 7, 30) else 7
