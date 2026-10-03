"""
Company and account moderation (SRS §2.3, platform roles).

§2.3 gives Platform Support "read user and company records ... resend
verification" and Platform Moderator "unpublish jobs, suspend accounts and
companies". The services existed or were one step away; no screen reached any
of them. The company admin was never registered, the job admin is read-only by
design, and `User.is_active` could only be flipped in the admin change form —
with no reason, no audit entry, and every session left signed in.

These are workflow, not CRUD, which is what ADR-A5 reserves staff screens for:
each action takes a reason, goes through a service, and is audited there.

Reads are Support and above. Actions are Moderator and above, except resending
a verification email, which §2.3 gives Support.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from apps.accounts import staff
from apps.accounts.models import User
from apps.accounts.services import moderation as account_moderation
from apps.common import restrictions
from apps.common.errors import DomainError, NotAllowed
from apps.common.ids import as_pk, as_uuid
from apps.companies.models import Company, CompanyState
from apps.companies.services import company as company_service
from apps.jobs.models import Job, JobStatus
from apps.jobs.services import lifecycle

from .views import StaffView

PAGE_SIZE = 50


def _require_moderator(user) -> None:
    if not staff.at_least(user, "moderator"):
        raise NotAllowed("That action needs moderator access.")


class CompanyListView(StaffView):
    """`/staff/companies/` — find a company."""

    level = "support"

    def get(self, request: HttpRequest) -> HttpResponse:
        query = (request.GET.get("q") or "").strip()[:100]
        state = request.GET.get("state") or ""
        companies = Company.objects.filter(deleted_at__isnull=True).annotate(
            members=Count(
                "memberships", filter=Q(memberships__deleted_at__isnull=True), distinct=True
            ),
            live_jobs=Count(
                "jobs",
                filter=Q(jobs__status=JobStatus.PUBLISHED, jobs__deleted_at__isnull=True),
                distinct=True,
            ),
        )
        if query:
            companies = companies.filter(Q(name__icontains=query) | Q(slug__icontains=query))
        if state in CompanyState.values:
            companies = companies.filter(state=state)
        page = Paginator(companies.order_by("name"), PAGE_SIZE).get_page(request.GET.get("page"))
        return render(
            request,
            "staff/companies.html",
            {
                **self.context(request),
                "page": page,
                "query": query,
                "state": state,
                "states": CompanyState.choices,
            },
        )


class CompanyDetailView(StaffView):
    """`/staff/companies/<slug>/` — one company, and the moderator's actions on it."""

    level = "support"

    def get_company(self, slug: str) -> Company:
        company = Company.objects.filter(slug=slug, deleted_at__isnull=True).first()
        if company is None:
            raise Http404("No such company.")
        return company

    def get(self, request: HttpRequest, slug: str) -> HttpResponse:
        company = self.get_company(slug)
        return render(
            request,
            "staff/company.html",
            {
                **self.context(request),
                "company": company,
                "jobs": Job.objects.filter(company=company, deleted_at__isnull=True).order_by(
                    "-updated_at"
                )[:100],
                "members": company.memberships.filter(deleted_at__isnull=True)
                .select_related("user")
                .order_by("role", "user__email"),
                "can_moderate": staff.at_least(request.user, "moderator"),
                "is_suspended": company.state == CompanyState.SUSPENDED,
            },
        )

    def post(self, request: HttpRequest, slug: str) -> HttpResponse:
        company = self.get_company(slug)
        back = reverse("staff:company", args=[company.slug])
        action = request.POST.get("action", "")
        reason = request.POST.get("reason", "")
        try:
            _require_moderator(request.user)
            if action == "suspend":
                company_service.suspend_company(actor=request.user, company=company, reason=reason)
                messages.success(
                    request, f"{company.name} is suspended. Its editors have been told."
                )
            elif action == "reinstate":
                company_service.reinstate_company(
                    actor=request.user, company=company, reason=reason
                )
                messages.success(request, f"{company.name} is reinstated.")
            elif action == "take_down":
                job = Job.objects.filter(
                    company=company,
                    public_id=as_uuid(request.POST.get("job")),
                    deleted_at__isnull=True,
                ).first()
                if job is None:
                    raise NotAllowed("That posting is not part of this company.")
                lifecycle.take_down(job=job, actor=request.user, reason=reason)
                messages.success(request, f"“{job.title}” is taken down.")
            else:
                messages.error(request, "That is not something this screen can do.")
        except DomainError as exc:
            restrictions.flash(request, exc)
        return redirect(back)


class PeopleListView(StaffView):
    """`/staff/people/` — find an account."""

    level = "support"

    def get(self, request: HttpRequest) -> HttpResponse:
        query = (request.GET.get("q") or "").strip()[:100]
        people = User.objects.all()
        if query:
            people = people.filter(Q(email__icontains=query) | Q(full_name__icontains=query))
        else:
            people = people.none()
        page = Paginator(people.order_by("email"), PAGE_SIZE).get_page(request.GET.get("page"))
        return render(
            request,
            "staff/people.html",
            {**self.context(request), "page": page, "query": query},
        )


class PersonDetailView(StaffView):
    """`/staff/people/<id>/` — one account, and what staff may do to it."""

    level = "support"

    def get_person(self, user_id) -> User:
        person = User.objects.filter(pk=as_pk(user_id)).first()
        if person is None:
            raise Http404("No such account.")
        return person

    def get(self, request: HttpRequest, user_id: int) -> HttpResponse:
        person = self.get_person(user_id)
        address = person.email_addresses.filter(is_primary=True).first()
        return render(
            request,
            "staff/person.html",
            {
                **self.context(request),
                "person": person,
                "address": address,
                "memberships": person.company_memberships.filter(deleted_at__isnull=True)
                .select_related("company")
                .order_by("company__name"),
                "person_staff_level": staff.level_of(person),
                "sessions": person.sessions.filter(revoked_at__isnull=True).count(),
                "can_moderate": staff.at_least(request.user, "moderator"),
            },
        )

    def post(self, request: HttpRequest, user_id: int) -> HttpResponse:
        person = self.get_person(user_id)
        back = reverse("staff:person", args=[person.pk])
        action = request.POST.get("action", "")
        reason = request.POST.get("reason", "")
        try:
            if action == "resend_verification":
                self._resend(request, person)
            elif action == "impersonate":
                from apps.accounts import impersonation

                impersonation.start(request, target=person, reason=reason)
                messages.info(request, f"Viewing as {person.email} for up to 60 minutes.")
                return redirect("dashboard:index")
            elif action == "suspend":
                _require_moderator(request.user)
                ended = account_moderation.suspend_account(
                    actor=request.user, user=person, reason=reason
                )
                messages.success(
                    request, f"{person.email} is suspended and signed out of {ended} session(s)."
                )
            elif action == "reinstate":
                _require_moderator(request.user)
                account_moderation.reinstate_account(actor=request.user, user=person, reason=reason)
                messages.success(request, f"{person.email} is reinstated.")
            else:
                messages.error(request, "That is not something this screen can do.")
        except DomainError as exc:
            restrictions.flash(request, exc)
        return redirect(back)

    def _resend(self, request: HttpRequest, person: User) -> None:
        from apps.accounts.views import SignUpView
        from apps.audit import services as audit

        address = person.email_addresses.filter(is_primary=True).first()
        if address is None or address.verified_at is not None:
            messages.info(request, "That address is already confirmed.")
            return
        SignUpView()._send_verification(request, person)
        audit.record(verb="account.verification_resent_by_staff", actor=request.user, target=person)
        messages.success(request, f"A new confirmation link was sent to {address.email}.")


class StaffTeamView(StaffView):
    """`/staff/team/` — who holds platform authority, and changing it (SRS §2.3).

    Superadmin only. A grant is proposed here and approved by a *different*
    superadmin; a revocation is immediate.
    """

    level = "superadmin"

    def get(self, request: HttpRequest) -> HttpResponse:
        from apps.accounts.models import StaffRole, StaffRoleRequest

        return render(
            request,
            "staff/team.html",
            {
                **self.context(request),
                "roles": StaffRole.objects.select_related("user", "granted_by").order_by(
                    "level", "user__email"
                ),
                "pending": StaffRoleRequest.objects.filter(
                    state=StaffRoleRequest.State.PENDING
                ).select_related("user", "requested_by"),
                "levels": StaffRole.Level.choices,
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        from apps.accounts.models import StaffRoleRequest
        from apps.accounts.services import staff_roles

        action = request.POST.get("action", "")
        try:
            if action == "propose":
                person = User.objects.filter(
                    email__iexact=(request.POST.get("email") or "").strip()
                ).first()
                if person is None:
                    raise NotAllowed("No account uses that email.")
                staff_roles.propose(
                    actor=request.user,
                    user=person,
                    level=request.POST.get("level", ""),
                    reason=request.POST.get("reason", ""),
                )
                messages.success(
                    request, "Proposed. Another superadmin has to approve it before it applies."
                )
            elif action in {"approve", "decline", "withdraw"}:
                proposal = StaffRoleRequest.objects.filter(
                    pk=as_pk(request.POST.get("request"))
                ).first()
                if proposal is None:
                    raise NotAllowed("That proposal does not exist.")
                if action == "withdraw":
                    staff_roles.withdraw(actor=request.user, request=proposal)
                    messages.success(request, "Proposal withdrawn.")
                else:
                    staff_roles.decide(
                        actor=request.user, request=proposal, approve=action == "approve"
                    )
                    messages.success(
                        request, "Role granted." if action == "approve" else "Proposal declined."
                    )
            elif action == "revoke":
                person = User.objects.filter(pk=as_pk(request.POST.get("user"))).first()
                if person is None:
                    raise NotAllowed("That account does not exist.")
                staff_roles.revoke(
                    actor=request.user, user=person, reason=request.POST.get("reason", "")
                )
                messages.success(request, f"{person.email} no longer holds a staff role.")
            else:
                messages.error(request, "That is not something this screen can do.")
        except DomainError as exc:
            restrictions.flash(request, exc)
        return redirect("staff:team")


class ImpersonationStopView(View):
    """`/staff/impersonation/stop/` — reachable from any page, as the banner's button.

    Not a `StaffView`: during impersonation `request.user` is the person being
    viewed, who holds no staff role. The staff member is `request.impersonator`.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        from apps.accounts import impersonation

        target = getattr(request, "impersonation", {}) or {}
        impersonation.stop(request)
        messages.success(request, "You are yourself again.")
        person = target.get("target") if isinstance(target, dict) else None
        if person is not None:
            return redirect("staff:person", user_id=person.pk)
        return redirect("staff:operations")
