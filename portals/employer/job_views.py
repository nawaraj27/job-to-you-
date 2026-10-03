"""
Employer job management, URL-scoped by company (ADR-A18).

Every view here resolves its posting through `selectors.job_for_company(
request.company, ...)`. That is the tenant boundary, and it is in the query
rather than in an `if` afterwards: a lookup by public id alone would return
another company's posting and leave authorization to remember to catch it.

Views parse input, call a policy, call one service, and pick a template. The
lifecycle buttons all POST to one action endpoint because they are one
decision — "what happens to this posting next" — and the state machine, not the
URL router, decides which answers are legal.
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

from apps.common import restrictions
from apps.common.errors import DomainError, NotAllowed, NotFound
from apps.companies import policies as company_policies
from apps.companies import selectors as company_selectors
from apps.companies.models import CompanyMembership
from apps.companies.permissions import Grant
from apps.jobs import policies, selectors
from apps.jobs.models import (
    ApplyMode,
    EmploymentType,
    ExperienceLevel,
    JobAssignment,
    JobStatus,
    QuestionKind,
    RemoteScope,
    Requirement,
    Workplace,
)
from apps.jobs.services import lifecycle, postings
from apps.taxonomy import selectors as taxonomy_selectors


def _error(request: HttpRequest, exc: DomainError, fallback: str) -> HttpResponse:
    restrictions.flash(request, exc)
    return redirect(fallback)


def _choices() -> dict[str, Any]:
    """Everything the posting form offers. One place, so the create form and
    the edit form can never drift apart."""
    return {
        "workplaces": Workplace.choices,
        "remote_scopes": RemoteScope.choices,
        "employment_types": EmploymentType.choices,
        "experience_levels": ExperienceLevel.choices,
        "apply_modes": ApplyMode.choices,
        "requirements": Requirement.choices,
        "question_kinds": QuestionKind.choices,
        "categories": taxonomy_selectors.job_categories(),
        "locations": taxonomy_selectors.locations_in("NP"),
    }


#: Fields the form renders as one-per-line text but the model stores as lists.
LIST_FIELDS = ("responsibilities", "requirements", "benefits")


def _form_values(job=None, posted: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the form shows in its inputs.

    Always strings, from one place. A rejected submission comes back exactly as
    it was typed — nobody should retype a description because a salary range was
    the wrong way round — and an edit shows the stored row.
    """
    if posted is not None:
        return dict(posted)
    if job is None:
        return {"salary_visible": True}

    values = {name: getattr(job, name) for name in postings.EDITABLE if hasattr(job, name)}
    for name in LIST_FIELDS:
        values[name] = "\n".join(getattr(job, name) or [])
    values["closes_at"] = job.closes_at.date().isoformat() if job.closes_at else ""
    values["title"] = job.title
    return values


def _posted_fields(request: HttpRequest) -> dict[str, Any]:
    """Pull only what a posting form may write.

    Driven by `postings.EDITABLE` rather than by whatever the request contained,
    so a hand-crafted POST cannot reach `status`, `views_count` or `company`.
    """
    fields: dict[str, Any] = {}
    for name in postings.EDITABLE:
        if name == "remote_countries":
            values = request.POST.getlist("remote_countries")
            if values:
                fields[name] = values
            continue
        if name in request.POST:
            fields[name] = request.POST.get(name)

    # Checkboxes are absent when off, which is indistinguishable from "not
    # submitted" unless the form says which ones it rendered.
    for flag in ("salary_visible",):
        if f"{flag}_present" in request.POST:
            fields[flag] = flag in request.POST
    return fields


@method_decorator(login_required, name="dispatch")
class JobListView(View):
    """`/employer/<slug>/jobs/` — every posting this company owns."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = company_policies.require_section(request.membership, "jobs")

        status = request.GET.get("status") or ""
        jobs = policies.readable_jobs(selectors.jobs_for_company(request.company), membership)
        if status in JobStatus.values:
            jobs = jobs.filter(status=status)
        if request.GET.get("mine"):
            jobs = jobs.filter(assignments__membership=membership).distinct()

        return render(
            request,
            "employer/jobs/list.html",
            {
                "company": request.company,
                "membership": membership,
                "jobs": jobs,
                "counts": selectors.counts_for_company(request.company),
                "statuses": JobStatus.choices,
                "status": status,
                "mine": bool(request.GET.get("mine")),
                "can_publish": Grant.JOB_PUBLISH in membership.grants,
                "can_create": policies.can_create_jobs(membership),
            },
        )


@method_decorator(login_required, name="dispatch")
class JobCreateView(View):
    """`/employer/<slug>/jobs/new/` — a draft, never a live posting.

    Creation and publication are separate on purpose: a half-written job that
    went live because somebody hit Enter is the failure this split prevents.
    """

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        # A role without the jobs section gets a 403 here, not a redirect to a
        # job list that answers 403 itself.
        membership = company_policies.require_section(request.membership, "jobs")
        try:
            policies.require_can_manage_jobs(membership)
        except DomainError as exc:
            return _error(request, exc, reverse("employer:job_list", args=[company_slug]))
        return render(
            request,
            "employer/jobs/form.html",
            {"company": request.company, "job": None, "values": _form_values(), **_choices()},
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        # A role without the jobs section gets a 403 here, not a redirect to a
        # job list that answers 403 itself.
        membership = company_policies.require_section(request.membership, "jobs")
        try:
            policies.require_can_manage_jobs(membership)
        except DomainError as exc:
            return _error(request, exc, reverse("employer:job_list", args=[company_slug]))

        fields = _posted_fields(request)
        title = fields.pop("title", "")
        try:
            job = postings.create_job(
                company=request.company, actor=request.user, title=title, **fields
            )
        except DomainError as exc:
            restrictions.flash(request, exc)
            return render(
                request,
                "employer/jobs/form.html",
                {
                    "company": request.company,
                    "job": None,
                    "values": _form_values(posted={**fields, "title": title}),
                    **_choices(),
                },
                status=400,
            )

        messages.success(request, "Draft saved. Publish it when it is ready.")
        return redirect("employer:job_detail", company_slug=company_slug, public_id=job.public_id)


class _JobView(View):
    """Shared resolution: company-scoped lookup, then a policy."""

    def get_job(self, request: HttpRequest, public_id: str):
        membership = company_policies.require_section(request.membership, "jobs")
        job = selectors.job_for_company(request.company, public_id)
        if job is None or not policies.can_read(job, membership):
            raise Http404("No such posting.")
        return job

    def back(self, company_slug: str, public_id: str) -> str:
        return reverse("employer:job_detail", args=[company_slug, public_id])


@method_decorator(login_required, name="dispatch")
class JobDetailView(_JobView):
    """`/employer/<slug>/jobs/<public_id>/` — the posting's control room."""

    def get(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        job = self.get_job(request, public_id)

        return render(
            request,
            "employer/jobs/detail.html",
            {
                "company": request.company,
                "membership": membership,
                "job": job,
                "team": company_selectors.team_for(company=request.company),
                "can_write": policies.can_write(job, membership),
                "can_manage_assignments": policies.can_manage_assignments(membership),
                "can_publish": Grant.JOB_PUBLISH in membership.grants,
                # What the state machine will actually accept, so the screen
                # offers no button that would be refused on click.
                "next_states": lifecycle.LIFECYCLE.available(job, membership.grants),
                **_choices(),
            },
        )


@method_decorator(login_required, name="dispatch")
class JobEditView(_JobView):
    """`/employer/<slug>/jobs/<public_id>/edit/`."""

    def get(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        job = self.get_job(request, public_id)
        try:
            policies.require_can_write(job, membership)
        except DomainError as exc:
            return _error(request, exc, self.back(company_slug, public_id))

        return render(
            request,
            "employer/jobs/form.html",
            {"company": request.company, "job": job, "values": _form_values(job), **_choices()},
        )

    def post(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        job = self.get_job(request, public_id)

        # Authorization first, and separately. "You may not edit this" is not a
        # validation error, and re-rendering the form with the rejected values
        # would invite someone to fix a field that was never the problem.
        try:
            policies.require_can_write(job, membership)
            policies.require_editable(job)
        except DomainError as exc:
            return _error(request, exc, self.back(company_slug, public_id))

        try:
            postings.update_job(
                job=job,
                actor=request.user,
                row_version=_as_int(request.POST.get("row_version")),
                **_posted_fields(request),
            )
        except DomainError as exc:
            restrictions.flash(request, exc)
            return render(
                request,
                "employer/jobs/form.html",
                {
                    "company": request.company,
                    "job": job,
                    "values": _form_values(posted=_posted_fields(request)),
                    **_choices(),
                },
                status=400,
            )

        messages.success(request, "Posting updated.")
        return redirect(self.back(company_slug, public_id))


@method_decorator(login_required, name="dispatch")
class JobActionView(_JobView):
    """One POST endpoint for every lifecycle move.

    The state machine refuses an illegal transition, so an action that is not
    offered on screen is still refused if somebody posts it by hand.
    """

    def post(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        job = self.get_job(request, public_id)
        action = request.POST.get("action") or ""
        row_version = _as_int(request.POST.get("row_version"))
        back = self.back(company_slug, public_id)

        try:
            policies.require_can_write(job, membership)
            message, back = self._run(request, job, membership, action, row_version, back)
        except DomainError as exc:
            return _error(request, exc, back)

        messages.success(request, message)
        return redirect(back)

    def _run(
        self,
        request: HttpRequest,
        job,
        membership: CompanyMembership,
        action: str,
        row_version: int | None,
        back: str,
    ) -> tuple[str, str]:
        actor = request.user
        company_slug = request.company.slug

        if action == "publish":
            policies.require_can_publish(job, membership)
            lifecycle.publish(job=job, actor=actor, membership=membership, row_version=row_version)
            if not job.skills.exists():
                # Live, and invisible to the thing this platform is for. A
                # posting with no skills cannot be matched on skills: it is
                # scored on location, level and pay alone, and the candidates
                # who would have been right for it are never shown it. The
                # employer had no way of knowing that — the posting went live
                # with a cheerful "The posting is live."
                return (
                    "The posting is live, but it lists no skills, so candidates "
                    "cannot be matched to it on skills. Add them below and it "
                    "starts reaching the right people.",
                    back,
                )
            return "The posting is live.", back

        if action == "pause":
            policies.require_can_publish(job, membership)
            lifecycle.pause(job=job, actor=actor, membership=membership, row_version=row_version)
            return (
                "Paused. Candidates cannot see it or apply; applications already made carry on.",
                back,
            )

        if action == "resume":
            policies.require_can_publish(job, membership)
            lifecycle.resume(job=job, actor=actor, membership=membership, row_version=row_version)
            return "Back on the site.", back

        if action == "unpublish":
            policies.require_can_publish(job, membership)
            lifecycle.unpublish(
                job=job, actor=actor, membership=membership, row_version=row_version
            )
            return "Back to draft. Candidates can no longer see it.", back

        if action == "close":
            lifecycle.close(
                job=job,
                actor=actor,
                membership=membership,
                reason=request.POST.get("reason", ""),
                row_version=row_version,
            )
            return "Closed. It no longer accepts applications.", back

        if action == "archive":
            lifecycle.archive(job=job, actor=actor, membership=membership, row_version=row_version)
            return "Archived.", back

        if action == "restore":
            lifecycle.restore(job=job, actor=actor, membership=membership, row_version=row_version)
            return "Restored as a draft.", back

        if action == "clone":
            copy = postings.clone_job(job=job, actor=actor, title=request.POST.get("title", ""))
            return (
                "Copied. This is a fresh draft.",
                reverse("employer:job_detail", args=[company_slug, copy.public_id]),
            )

        if action == "delete":
            postings.delete_job(job=job, actor=actor)
            return (
                "Posting deleted.",
                reverse("employer:job_list", args=[company_slug]),
            )

        raise Http404("Unknown action.")


@method_decorator(login_required, name="dispatch")
class JobSkillsView(_JobView):
    """`/employer/<slug>/jobs/<public_id>/skills/` — taxonomy only, never text."""

    def post(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        job = self.get_job(request, public_id)
        back = self.back(company_slug, public_id)

        try:
            policies.require_can_write(job, membership)
            policies.require_editable(job)
            if request.POST.get("action") == "remove":
                postings.remove_skill(
                    job=job, actor=request.user, skill_id=int(request.POST.get("skill_id") or 0)
                )
                message = "Skill removed."
            else:
                skill = postings.add_skill(
                    job=job,
                    actor=request.user,
                    text=request.POST.get("text", ""),
                    requirement=request.POST.get("requirement", Requirement.REQUIRED),
                    weight=request.POST.get("weight"),
                    min_years=request.POST.get("min_years"),
                )
                message = f"Added {skill.skill.name}."
        except (DomainError, ValueError) as exc:
            return _error(request, _as_domain(exc), back)

        messages.success(request, message)
        return redirect(back)


@method_decorator(login_required, name="dispatch")
class JobQuestionsView(_JobView):
    """`/employer/<slug>/jobs/<public_id>/questions/` — screening questions.

    Stored here, applied in P8. This phase does not decide what an answer does
    to an application.
    """

    def post(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        job = self.get_job(request, public_id)
        back = self.back(company_slug, public_id)

        try:
            policies.require_can_write(job, membership)
            policies.require_editable(job)
            if request.POST.get("action") == "remove":
                postings.remove_question(
                    job=job,
                    actor=request.user,
                    question_id=int(request.POST.get("question_id") or 0),
                )
                message = "Question removed."
            else:
                options = [
                    line.strip()
                    for line in (request.POST.get("options") or "").split("\n")
                    if line.strip()
                ]
                postings.add_question(
                    job=job,
                    actor=request.user,
                    text=request.POST.get("text", ""),
                    kind=request.POST.get("kind", QuestionKind.SHORT_TEXT),
                    options=options,
                    is_required=bool(request.POST.get("is_required")),
                )
                message = "Question added."
        except (DomainError, ValueError) as exc:
            return _error(request, _as_domain(exc), back)

        messages.success(request, message)
        return redirect(back)


@method_decorator(login_required, name="dispatch")
class JobTeamView(_JobView):
    """`/employer/<slug>/jobs/<public_id>/team/` — who owns this posting.

    Assignment is not decoration: it is what gives a recruiter without
    `job.write_any` the right to edit this particular job (SRS §2.3).
    """

    def post(self, request: HttpRequest, company_slug: str, public_id: str) -> HttpResponse:
        membership = company_policies.require_membership(request.membership)
        job = self.get_job(request, public_id)
        back = self.back(company_slug, public_id)

        try:
            policies.require_can_write(job, membership)
            if not policies.can_manage_assignments(membership):
                # D-13: an assignment opens the job's applicants to its holder.
                raise NotAllowed(
                    "Only an owner or company admin can change who is on this posting."
                )
            target = self._target(request)
            kind = request.POST.get("kind", JobAssignment.Kind.RECRUITER)
            if request.POST.get("action") == "unassign":
                postings.unassign(job=job, actor=request.user, membership=target, kind=kind)
                message = "Removed from this posting."
            else:
                postings.assign(job=job, actor=request.user, membership=target, kind=kind)
                message = f"{target.user.get_full_name()} is on this posting."
        except DomainError as exc:
            return _error(request, exc, back)

        messages.success(request, message)
        return redirect(back)

    def _target(self, request: HttpRequest) -> CompanyMembership:
        """Scoped to this company, so an id from another tenant resolves to
        nothing rather than to somebody else's colleague."""
        target = company_selectors.member_of(
            company=request.company, membership_id=request.POST.get("membership_id")
        )
        if target is None:
            raise NotFound("That person is not on this company's team.")
        return target


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_domain(exc: Exception) -> DomainError:
    """A malformed id in a POST is a bad request, not a crash."""
    if isinstance(exc, DomainError):
        return exc
    return NotFound("That item is no longer there.")
