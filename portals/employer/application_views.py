"""
Employer applicant screens, URL-scoped by company (ADR-A18).

Every application is resolved through `selectors.application_for(membership,
public_id)`, which scopes by company *and* by assignment when the company asked
for that. The boundary is in the query; nothing here compensates for a lookup
that could have returned another tenant's row.

Contact details are rendered only when `policies.can_see_contact` says so
(ADR-A34). That check is at the template boundary rather than at the fetch,
because a context that carries a phone number is one `{{ }}` from showing it.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.applications import policies, selectors
from apps.applications.models import Application, StageKind
from apps.applications.services import offers as offer_service
from apps.applications.services import pipeline as pipeline_service
from apps.applications.services import pools as pool_service
from apps.applications.services import review as review_service
from apps.common import restrictions
from apps.common.errors import DomainError, NotFound
from apps.common.ids import as_pk, as_uuid
from apps.companies import policies as company_policies
from apps.companies import selectors as company_selectors
from apps.companies.permissions import Grant
from apps.jobs import policies as job_policies
from apps.jobs import selectors as job_selectors
from apps.matching import scoring as matching_scoring
from apps.matching.services import snapshots


def _error(request: HttpRequest, exc: DomainError, fallback: str) -> HttpResponse:
    restrictions.flash(request, exc)
    return redirect(fallback)


class _ApplicationView(View):
    """Shared resolution: scoped lookup, then a policy."""

    def get_application(self, request: HttpRequest, public_id: str) -> Application:
        """Scoped lookup, then the policy that says the same thing.

        The selector already scopes by company and by assignment, so the policy
        call is belt and braces — and that is the point: the day somebody
        widens the selector, the policy is still the thing that refuses.
        """
        membership = company_policies.require_membership(request.membership)
        application = selectors.application_for(membership, public_id)
        if application is None:
            raise Http404("No such application.")
        policies.require_can_read(application, membership)
        policies.require_can_open_detail(membership)
        return application

    def back(self, company_slug: str, public_id: str) -> str:
        return reverse("employer:application_detail", args=[company_slug, public_id])


def _screening_answers(application: Application) -> list[dict[str, str]]:
    """Each answer beside the question it answers, in the posting's order.

    The screen printed every answer under the same label, "Screening answer",
    so with more than one question nobody could tell which reply belonged to
    which. Answers are keyed by question id; a question deleted since is named
    as such rather than silently dropped.
    """
    answers = dict(application.answers or {})
    if not answers:
        return []
    rows = []
    for question in application.job.questions.all():
        value = answers.pop(str(question.pk), None)
        if value is None:
            continue
        rows.append({"question": question.text, "answer": _answer_text(value)})
    rows.extend(
        {"question": "A question since removed from the posting", "answer": _answer_text(value)}
        for value in answers.values()
    )
    return rows


def _answer_text(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # stored as 4.0; the candidate typed 4
    return str(value)


def _end_of_day(day):
    """The last minute of a chosen date, in the platform's local time."""
    if day is None:
        return None
    import datetime as dt

    from django.utils import timezone

    return timezone.make_aware(dt.datetime.combine(day, dt.time(23, 59)))


def _has_open_times(company) -> bool:
    """Whether a candidate moved to interview would have anything to pick from."""
    from apps.interviews.services import scheduling

    return scheduling.open_slots(company=company).exists()


@method_decorator(login_required, name="dispatch")
class ApplicantListView(View):
    """`/employer/<slug>/applicants/` — the pipeline board."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)

        # "match" reads the score frozen at submission (FR-8.4). Anything else
        # is the default, so a hand-typed query string cannot produce an order
        # the board does not have.
        order = "match" if request.GET.get("order") == "match" else "recent"

        job = None
        job_public_id = request.GET.get("job")
        if job_public_id:
            job = job_selectors.job_for_company(request.company, job_public_id)
            if job is None:
                raise Http404("No such posting.")

        return render(
            request,
            "employer/applications/board.html",
            {
                "company": request.company,
                "membership": membership,
                "board": selectors.pipeline_board(membership, job=job, order=order),
                "order": order,
                "counts": selectors.counts_for(membership),
                # The filter lists only postings this member can read (§2.3).
                "jobs": job_policies.readable_jobs(
                    job_selectors.jobs_for_company(request.company), membership
                ),
                "job": job,
                "stages": selectors.stages_for(request.company),
                # A Viewer or Interviewer reads the board; the bulk bar, the
                # checkboxes and the links into a candidate are for the roles
                # that can use them, instead of being offered and refused.
                "can_bulk": membership.role not in ("viewer", "interviewer"),
                "can_open_detail": policies.can_open_detail(membership),
                "can_pools": company_policies.can_open_section(membership, "pools"),
                "can_open_pipeline": company_policies.can_open_section(membership, "company"),
                "can_edit_pipeline": Grant.COMPANY_EDIT in membership.grants,
                "can_decide": Grant.APPLICATION_DECIDE in membership.grants,
            },
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        """Bulk stage moves and bulk tagging (FR-6.6)."""
        membership = company_policies.require_membership(request.membership)
        back = reverse("employer:applicants", args=[company_slug])

        chosen = request.POST.getlist("application")
        if not chosen:
            messages.error(request, "Choose some applications first.")
            return redirect(back)

        # Scoped in the query. An id from another company resolves to nothing
        # rather than to somebody else's candidate.
        chosen = [value for value in (as_uuid(item) for item in chosen) if value]
        applications = list(selectors.applications_for(membership).filter(public_id__in=chosen))
        # Readable is not writable (SRS §2.3): a Viewer sees the board and an
        # unassigned Recruiter reads every job's applicants, and neither may
        # move them. Refused rows are counted and said, not silently dropped.
        writable = [a for a in applications if policies.can_write(a, membership)]
        not_theirs = len(applications) - len(writable)
        applications = writable
        if not applications:
            messages.error(
                request,
                "You can read these applications but not change them. Changes are made by "
                "the people assigned to the job.",
            )
            return redirect(back)

        try:
            if request.POST.get("action") == "tag":
                count = review_service.tag_many(
                    applications=applications,
                    actor=request.user,
                    company=request.company,
                    name=request.POST.get("tag", ""),
                )
                summary = f"Tagged {count}."
                if not_theirs:
                    summary += f" {not_theirs} not on your jobs were left alone."
                messages.success(request, summary)
            else:
                stage = pipeline_service.stage_by_key(
                    request.company, request.POST.get("stage", "")
                )
                result = pipeline_service.move_many(
                    applications=applications,
                    actor=request.user,
                    membership=membership,
                    stage=stage,
                    reason=request.POST.get("reason", ""),
                    message=request.POST.get("message", ""),
                )
                summary = f"Moved {result['moved']} to {stage.label}."
                if not_theirs:
                    summary += f" {not_theirs} not on your jobs were left alone."
                if result["skipped"]:
                    summary += f" {result['skipped']} were already there."
                if result["refused"]:
                    # Said, not swallowed: a withdrawn candidate cannot be
                    # moved, and a count that quietly left them out looked
                    # like success.
                    summary += f" {result['refused']} could not be moved: " + "; ".join(
                        sorted(set(result["reasons"]))
                    )
                    messages.warning(request, summary)
                else:
                    messages.success(request, summary)
        except DomainError as exc:
            return _error(request, exc, back)

        return redirect(back)


@method_decorator(login_required, name="dispatch")
class ApplicationDetailView(_ApplicationView):
    """`/employer/<slug>/applicants/<public_id>/` — one application."""

    def get(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        application = self.get_application(request, public_id)

        return render(
            request,
            "employer/applications/detail.html",
            {
                "company": request.company,
                "membership": membership,
                "application": application,
                "timeline": selectors.employer_timeline(application),
                "screening_answers": _screening_answers(application),
                "notes": selectors.notes_for(application),
                "ratings": selectors.ratings_for(application),
                "prior": selectors.prior_applications(membership, application),
                "offer": (live_offer := selectors.live_offer(application)),
                "offer_document_url": offer_service.document_url(live_offer, request.user),
                "accepted_offer": application.offers.filter(state="accepted").first(),
                "offers": selectors.offers_for(application),
                "stages": selectors.stages_for(request.company),
                "team": company_selectors.team_for(company=request.company),
                # Offered only to members who may save to pools (SRS §2.3).
                "pools": (
                    selectors.pools_for(request.company)
                    if Grant.CANDIDATE_SEARCH in membership.grants
                    else []
                ),
                # ADR-A34. The template shows contact details only under this.
                "can_see_contact": policies.can_see_contact(application),
                "can_write": (can_write := policies.can_write(application, membership)),
                "can_comment": policies.can_write(application, membership, policies.WRITE_COMMENT),
                "can_message": company_policies.can_open_section(membership, "messages"),
                "read_only_reason": (
                    "Your role can read this application but not move it."
                    if membership.role in ("viewer", "interviewer")
                    else "Changes to this application are made by the people assigned to "
                    f"{application.job.title}. Ask an admin to add you to the job."
                ),
                "can_decide": can_write and Grant.APPLICATION_DECIDE in membership.grants,
                "can_offer": can_write and Grant.OFFER_SEND in membership.grants,
                "my_rating": application.ratings.filter(rater=request.user).first(),
                # Read from the frozen snapshot, never recomputed (FR-8.5).
                "match": _frozen_match(application),
                "match_stale": snapshots.is_stale(
                    application, current_version=matching_scoring.ALGORITHM_VERSION
                ),
            },
        )


@method_decorator(login_required, name="dispatch")
class ApplicationActionView(_ApplicationView):
    """One POST endpoint for everything done to one application.

    They are all the same decision — "what happens to this candidate next" —
    and the services, not the URL router, decide which answers are legal.
    """

    def post(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        application = self.get_application(request, public_id)
        back = self.back(company_slug, public_id)

        try:
            message = self._run(request, application, membership)
        except DomainError as exc:
            return _error(request, exc, back)

        messages.success(request, message)
        return redirect(back)

    #: Which kind of change each action is (SRS §2.3). `pool` is not here: it
    #: saves the candidate, not the application, and has its own grant.
    KINDS = {
        "move": policies.WRITE_ORGANISE,
        "reveal_contact": policies.WRITE_ORGANISE,
        "tag": policies.WRITE_ORGANISE,
        "untag": policies.WRITE_ORGANISE,
        "note": policies.WRITE_COMMENT,
        "delete_note": policies.WRITE_COMMENT,
        "rate": policies.WRITE_COMMENT,
    }

    def _run(self, request: HttpRequest, application: Application, membership) -> str:
        action = request.POST.get("action") or ""
        actor = request.user
        if action in self.KINDS:
            policies.require_can_write(application, membership, self.KINDS[action])

        if action == "move":
            stage = pipeline_service.stage_by_key(request.company, request.POST.get("stage", ""))
            pipeline_service.move(
                application=application,
                actor=actor,
                membership=membership,
                stage=stage,
                reason=request.POST.get("reason", ""),
                message=request.POST.get("message", ""),
            )
            if stage.kind == StageKind.TERMINAL:
                return "Closed. The candidate has been told."
            if stage.kind == StageKind.INTERVIEW and not _has_open_times(request.company):
                # Moved to interview with nothing for them to book. The
                # candidate's screen now says their interview is being
                # arranged, which is true and is not the same as being able to
                # pick a time — and the person who moved them had no idea the
                # calendar was empty.
                return (
                    f"Moved to {stage.label}, but you have no open interview times, "
                    "so they cannot book one yet. Publish some on the Interviews "
                    "screen and they are told."
                )
            return f"Moved to {stage.label}."

        if action == "reveal_contact":
            policies.require_can_decide(membership)
            pipeline_service.reveal_contact(
                application=application,
                actor=actor,
                membership=membership,
                basis="manual",
                reason=request.POST.get("reason", ""),
            )
            return "Contact details unlocked. The candidate has been told."

        if action == "note":
            review_service.add_note(
                application=application,
                actor=actor,
                membership=membership,
                body=request.POST.get("body", ""),
                mentions=request.POST.getlist("mentions"),
            )
            return "Note added. Internal only."

        if action == "delete_note":
            note = application.notes.filter(pk=as_pk(request.POST.get("note_id"))).first()
            if note is None:
                raise NotFound("That note is gone.")
            review_service.delete_note(note=note, actor=actor)
            return "Note deleted."

        if action == "tag":
            review_service.add_tag(
                application=application,
                actor=actor,
                company=request.company,
                name=request.POST.get("tag", ""),
            )
            return "Tagged."

        if action == "untag":
            review_service.remove_tag(
                application=application, actor=actor, tag_id=_as_int(request.POST.get("tag_id"))
            )
            return "Tag removed."

        if action == "rate":
            review_service.rate(
                application=application,
                actor=actor,
                score=_as_int(request.POST.get("score")),
                comment=request.POST.get("comment", ""),
            )
            return "Rating saved."

        if action == "pool":
            company_policies.require_grant(membership, Grant.CANDIDATE_SEARCH)
            pool = (
                selectors.pools_for(request.company)
                .filter(pk=as_pk(request.POST.get("pool_id")))
                .first()
            )
            if pool is None:
                raise NotFound("That pool is gone.")
            pool_service.add_member(pool=pool, candidate=application.candidate, actor=actor)
            return f"Saved to {pool.name}."

        raise Http404("Unknown action.")


@method_decorator(login_required, name="dispatch")
class OfferView(_ApplicationView):
    """`/employer/<slug>/applicants/<public_id>/offer/`."""

    def post(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        application = self.get_application(request, public_id)
        back = self.back(company_slug, public_id)

        try:
            policies.require_can_write(application, membership)
            policies.require_can_offer(membership)
            message = self._run(request, application, membership)
        except DomainError as exc:
            return _error(request, exc, back)

        messages.success(request, message)
        return redirect(back)

    def _run(self, request: HttpRequest, application: Application, membership) -> str:
        action = request.POST.get("action") or "draft"
        actor = request.user

        if action == "draft":
            offer_service.draft_offer(
                application=application,
                actor=actor,
                membership=membership,
                salary=request.POST.get("salary"),
                currency=request.POST.get("currency", ""),
                period=request.POST.get("period", "year"),
                start_date=_as_date(request.POST.get("start_date")),
                expires_at=_end_of_day(_as_date(request.POST.get("expires_on"))),
                message=request.POST.get("message", ""),
                document=request.FILES.get("document"),
            )
            return "Offer drafted. Nothing has been sent yet."

        offer = selectors.live_offer(application)
        if offer is None:
            raise NotFound("There is no live offer.")

        if action == "send":
            offer_service.send(offer=offer, actor=actor, membership=membership)
            return "Offer sent."

        if action == "withdraw":
            offer_service.withdraw(
                offer=offer,
                actor=actor,
                membership=membership,
                reason=request.POST.get("reason", ""),
            )
            return "Offer withdrawn."

        raise Http404("Unknown action.")


@method_decorator(login_required, name="dispatch")
class PipelineSettingsView(View):
    """`/employer/<slug>/pipeline/` — the company's own stages (FR-6.3)."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_section(request.membership, "company")
        return render(
            request,
            "employer/applications/pipeline.html",
            {
                "company": request.company,
                "membership": membership,
                "stages": selectors.stages_for(request.company),
                "kinds": StageKind.choices,
                "is_default": not request.company.pipeline_stages.exists(),
                "can_edit": Grant.COMPANY_EDIT in membership.grants,
            },
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        back = reverse("employer:pipeline", args=[company_slug])

        try:
            company_policies.require_can_edit_company(membership)
            pipeline_service.configure_stages(
                company=request.company, actor=request.user, specs=self._specs(request)
            )
        except DomainError as exc:
            return _error(request, exc, back)

        messages.success(request, "Pipeline saved.")
        return redirect(back)

    def _specs(self, request: HttpRequest) -> list[dict[str, Any]]:
        """One list of parallel arrays from the form, in screen order."""
        keys = request.POST.getlist("key")
        labels = request.POST.getlist("label")
        kinds = request.POST.getlist("kind")
        visible = set(request.POST.getlist("is_visible_to_candidate"))
        reveals = set(request.POST.getlist("reveals_contact"))
        candidate_labels = request.POST.getlist("candidate_label")

        specs = []
        for index, key in enumerate(keys):
            if not key.strip():
                continue
            specs.append(
                {
                    "key": key,
                    "label": labels[index] if index < len(labels) else key,
                    "kind": kinds[index] if index < len(kinds) else StageKind.OPEN,
                    "is_visible_to_candidate": key in visible,
                    "reveals_contact": key in reveals,
                    "candidate_label": (
                        candidate_labels[index] if index < len(candidate_labels) else ""
                    ),
                }
            )
        return specs


@method_decorator(login_required, name="dispatch")
class TalentPoolView(View):
    """`/employer/<slug>/pools/` — people worth remembering (FR-6.9)."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        # SRS §2.3: saving people to pools is a Recruiter-and-above right
        # ("Search, save to pools, message"); a Viewer or Interviewer has no
        # Candidates access, and this page lists candidates by name.
        company_policies.require_grant(membership, Grant.CANDIDATE_SEARCH)
        pools = selectors.pools_for(request.company)

        chosen = None
        if request.GET.get("pool"):
            chosen = pools.filter(pk=as_pk(request.GET["pool"])).first()

        return render(
            request,
            "employer/applications/pools.html",
            {
                "company": request.company,
                "membership": membership,
                "pools": pools,
                "pool": chosen,
                "members": selectors.pool_members(chosen) if chosen else [],
            },
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        back = reverse("employer:pools", args=[company_slug])
        action = request.POST.get("action")

        try:
            company_policies.require_grant(membership, Grant.CANDIDATE_SEARCH)
            if action == "create":
                pool_service.create_pool(
                    company=request.company,
                    actor=request.user,
                    name=request.POST.get("name", ""),
                    description=request.POST.get("description", ""),
                )
                messages.success(request, "Pool created.")
            elif action in {"delete", "remove"}:
                pool = (
                    selectors.pools_for(request.company)
                    .filter(pk=as_pk(request.POST.get("pool_id")))
                    .first()
                )
                if pool is None:
                    raise NotFound("That pool is gone.")
                if action == "delete":
                    pool_service.delete_pool(pool=pool, actor=request.user)
                    messages.success(request, "Pool deleted.")
                else:
                    member = pool.members.filter(
                        candidate__public_id=request.POST.get("candidate")
                    ).first()
                    if member is None:
                        raise NotFound("That candidate is not in this pool.")
                    pool_service.remove_member(
                        pool=pool, candidate=member.candidate, actor=request.user
                    )
                    messages.success(request, "Removed from the pool.")
            else:
                raise Http404("Unknown action.")
        except DomainError as exc:
            return _error(request, exc, back)

        return redirect(back)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_date(value: Any):
    import datetime as dt

    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _frozen_match(application) -> dict | None:
    """The stored match snapshot, shaped for the template.

    `None` for an application submitted before P13, or one whose snapshot
    failed. The template renders nothing rather than a zero — an absent score
    and a score of nought are different facts about a candidate.
    """
    snapshot = application.match_snapshot or {}
    if "score" not in snapshot:
        return None

    return {
        "score": snapshot["score"],
        "band": snapshot.get("band", ""),
        "label": matching_scoring.label_for(snapshot.get("band", "")),
        "components": snapshot.get("components", []),
    }
