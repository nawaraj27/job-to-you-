"""
Real employer surfaces, URL-scoped by company (ADR-A18).

These live under `/employer/<company_slug>/`. The legacy mock portal keeps its
flat `/employers/` URLs until P8 replaces it wholesale — two schemes coexist
deliberately, because the mock has no company to resolve (SRS §6.3).

Views here parse input, call a policy, call one service or selector, and choose
a template. They hold no business rules and never query the ORM directly
(DEVELOPER.md §1.1).
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.applications import policies as application_policies
from apps.common import restrictions
from apps.common.errors import DomainError
from apps.common.ids import as_uuid
from apps.companies import policies, selectors
from apps.companies.middleware import SESSION_HINT_KEY
from apps.companies.models import CompanyMembership
from apps.companies.permissions import Grant, Role
from apps.companies.services import company as company_service
from apps.companies.services import domains as domain_service
from apps.companies.services import membership as membership_service
from apps.core.branding import site_name
from apps.core.features import is_enabled
from apps.jobs import policies as job_policies
from apps.jobs import selectors as job_selectors
from apps.jobs.models import Job, JobStatus
from apps.notifications import email as email_service


def _error(request: HttpRequest, exc: DomainError, fallback: str) -> HttpResponse:
    """One mapping from DomainError to a user-visible outcome (§3.7)."""
    restrictions.flash(request, exc)
    return redirect(fallback)


@method_decorator(login_required, name="dispatch")
class SwitcherView(View):
    """`/employer/` — send the user to a company, or to registration."""

    def get(self, request: HttpRequest) -> HttpResponse:
        companies = list(selectors.companies_for(request.user))

        if not companies:
            return redirect("employer:create")

        hint = request.session.get(SESSION_HINT_KEY)
        chosen = next((c for c in companies if c.slug == hint), companies[0])

        if len(companies) == 1 or hint:
            return redirect("employer:overview", company_slug=chosen.slug)

        return render(request, "employer/switcher.html", {"companies": companies})


#: `/employer/go/<target>/` -> the company-scoped route it stands for.
SHORTCUTS = {
    "post-job": ("employer:job_create", "post jobs"),
    "jobs": ("employer:job_list", "manage jobs"),
    "applicants": ("employer:applicants", "review applicants"),
    "company": ("employer:profile", "edit the company profile"),
}


def _shortcut_allowed(target: str, membership: CompanyMembership | None) -> bool:
    """The same gates the target pages apply, asked before sending anyone there."""
    if target == "post-job":
        return job_policies.can_create_jobs(membership)
    if target in ("jobs", "applicants"):
        return policies.can_open_section(membership, "jobs")
    return policies.can_open_section(membership, "company")


@method_decorator(login_required, name="dispatch")
class ShortcutView(View):
    """`/employer/go/<target>/` — a company page, for a link that has no company.

    The footer is the same on every page and cannot name a company, but "Manage
    jobs" is only meaningful for one. This resolves it the way the company
    switcher does: no company means registration, one (or the last one used)
    means that company's page, several means the chooser. Whether the member's
    role may open the page is decided by the page itself, as it is for any link.
    """

    def get(self, request: HttpRequest, target: str) -> HttpResponse:
        if target not in SHORTCUTS:
            raise Http404
        route, purpose = SHORTCUTS[target]
        companies = list(selectors.companies_for(request.user))
        if not companies:
            messages.info(request, "Create your company account first.")
            return redirect("employer:create")
        hint = request.session.get(SESSION_HINT_KEY)
        chosen = next((c for c in companies if c.slug == hint), None)
        if chosen is None and len(companies) == 1:
            chosen = companies[0]
        if chosen is None:
            return render(request, "employer/switcher.html", {"companies": companies})
        membership = selectors.membership_for(user=request.user, company=chosen)
        if not _shortcut_allowed(target, membership):
            # A footer link must not end in a 403: say what the role allows instead.
            messages.info(
                request, f"Your role at {chosen.name} doesn't include the ability to {purpose}."
            )
            return redirect("employer:overview", company_slug=chosen.slug)
        return redirect(route, company_slug=chosen.slug)


@method_decorator(login_required, name="dispatch")
class CreateCompanyView(View):
    """`/employer/new/` — register a company and become its first owner."""

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, "employer/create.html", {})

    def post(self, request: HttpRequest) -> HttpResponse:
        name = (request.POST.get("name") or "").strip()
        website = (request.POST.get("website") or "").strip()
        values = {"name": name, "website": website}
        if not name:
            messages.error(request, "Give your company a name.")
            return render(request, "employer/create.html", values, status=400)

        try:
            company = company_service.create_company(actor=request.user, name=name, website=website)
        except DomainError as exc:
            # Re-render rather than redirect, so what was typed survives.
            restrictions.flash(request, exc)
            return render(request, "employer/create.html", values, status=400)

        messages.success(request, f"{company.name} is ready.")
        return redirect("employer:overview", company_slug=company.slug)


@method_decorator(login_required, name="dispatch")
class OverviewView(View):
    """`/employer/<slug>/` — what needs attention, then the rest.

    A recruiter opening this screen has one question: what is waiting for me.
    It used to answer with four setup cards and the sentence "applicants and
    hiring analytics arrive in a later release", while the applicant board, the
    pipeline, talent search, interviews, messages and analytics were all built
    and working two clicks away.
    """

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = policies.require_membership(request.membership)
        return render(
            request,
            "employer/overview.html",
            {
                "company": request.company,
                "membership": membership,
                "team_count": selectors.team_for(company=request.company).count(),
                "job_counts": job_selectors.counts_for_company(request.company),
                "can_manage_team": policies.can_manage_team(membership),
                "can_create_jobs": job_policies.can_create_jobs(membership),
                # What this role can open from here (SRS §2.3), so the page
                # offers no link that answers 403.
                "can_open_detail": application_policies.can_open_detail(membership),
                "can_open_team": policies.can_open_section(membership, "team"),
                "can_open_company": policies.can_open_section(membership, "company"),
                "can_edit_company": Grant.COMPANY_EDIT in membership.grants,
                **_attention(membership),
            },
        )


#: How many rows the "waiting on you" lists show. Beyond this it is a board,
#: and there is a board.
ATTENTION_LIMIT = 5


def _attention(membership) -> dict:
    """What is waiting on this recruiter, right now.

    Three things, in the order a hiring day actually runs: people who have
    applied and nobody has looked at, interviews happening next, and any
    posting still sitting in draft.
    """
    from apps.applications import selectors as application_selectors

    counts = application_selectors.counts_for(membership)
    new_applicants = (
        application_selectors.applications_for(membership)
        .filter(stage__key="new", withdrawn_at__isnull=True, closed_at__isnull=True)
        .select_related("job", "candidate__user")
        .order_by("-applied_at")[:ATTENTION_LIMIT]
    )

    upcoming: list = []
    if is_enabled("interview"):
        from apps.applications.models import Application
        from apps.interviews.services import scheduling

        # The same reach as the interviews screen (D-13): a panel seat on a job
        # this member cannot read named an applicant the list then refused.
        upcoming = list(
            scheduling.upcoming_for(membership=membership).filter(
                application__in=Application.objects.visible_to(membership).values("pk")
            )[:ATTENTION_LIMIT]
        )

    # Drafts this member can finish: "Finish it" is a write, and the list was
    # every draft in the company, for every role.
    drafts = [
        job
        for job in Job.objects.for_company(membership.company)
        .filter(status=JobStatus.DRAFT)
        .order_by("-updated_at")[:50]
        if job_policies.can_write(job, membership)
    ][:ATTENTION_LIMIT]

    return {
        "application_counts": counts,
        "new_applicants": new_applicants,
        "upcoming_interviews": upcoming,
        "draft_jobs": drafts,
    }


@method_decorator(login_required, name="dispatch")
class TeamView(View):
    """`/employer/<slug>/team/` — members and open invitations."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = policies.require_section(request.membership, "team")
        is_owner = membership.role == Role.OWNER
        return render(
            request,
            "employer/team.html",
            {
                "company": request.company,
                "membership": membership,
                "members": selectors.team_for(company=request.company),
                "invites": selectors.open_invites_for(company=request.company),
                "roles": Role.choices,
                # Only an owner can make an owner (§2.4), so nobody else is
                # offered the choice only to be refused after choosing it.
                "assignable_roles": [
                    (value, label)
                    for value, label in Role.choices
                    if is_owner or value != Role.OWNER
                ],
                "can_manage_team": policies.can_manage_team(membership),
                "is_owner": is_owner,
                "nomination": membership_service.pending_nomination(request.company),
            },
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = policies.require_membership(request.membership)
        action = request.POST.get("action")
        back = reverse("employer:team", args=[company_slug])

        try:
            if action == "invite":
                self._invite(request, membership)
            elif action == "revoke":
                self._revoke(request, membership)
            elif action == "role":
                self._change_role(request, membership)
            elif action == "remove":
                self._remove(request, membership)
            elif action == "nominate":
                target = self._target(request, membership)
                membership_service.nominate_owner(
                    actor=request.user, actor_membership=membership, target=target
                )
                messages.success(
                    request,
                    f"{target.user.get_full_name()} has been asked to take over. "
                    "Nothing changes until they accept.",
                )
            elif action == "cancel_nomination":
                membership_service.cancel_nomination(
                    actor=request.user, actor_membership=membership
                )
                messages.success(request, "Nomination withdrawn. Ownership has not changed.")
            elif action in {"accept_ownership", "decline_ownership"}:
                accepted = action == "accept_ownership"
                membership_service.respond_to_nomination(
                    actor=request.user, actor_membership=membership, accept=accepted
                )
                messages.success(
                    request,
                    f"You now own {request.company.name}."
                    if accepted
                    else "You declined. Ownership has not changed.",
                )
            else:
                raise Http404("Unknown action.")
        except DomainError as exc:
            return _error(request, exc, back)

        return redirect(back)

    def _revoke(self, request: HttpRequest, membership: CompanyMembership) -> None:
        invite = membership_service.revoke_invite(
            actor=request.user, actor_membership=membership, invite_id=request.POST.get("invite_id")
        )
        messages.success(
            request, f"Invitation to {invite.email} withdrawn. The link no longer works."
        )

    def _change_role(self, request: HttpRequest, membership: CompanyMembership) -> None:
        target = self._target(request, membership)
        updated = membership_service.change_role(
            actor=request.user,
            actor_membership=membership,
            target=target,
            role=request.POST.get("role", ""),
        )
        messages.success(
            request, f"{updated.user.get_full_name()} is now {updated.get_role_display()}."
        )

    def _invite(self, request: HttpRequest, membership: CompanyMembership) -> None:
        invite, token = membership_service.invite(
            actor=request.user,
            actor_membership=membership,
            company=request.company,
            email=request.POST.get("email", ""),
            role=request.POST.get("role", Role.RECRUITER),
        )
        link = request.build_absolute_uri(reverse("employer:accept_invite", args=[token]))
        email_service.send(
            to=invite.email,
            subject=f"You have been invited to {request.company.name} on {site_name()}",
            body=(
                f"{request.user.get_full_name()} invited you to join "
                f"{request.company.name} as {invite.get_role_display()}.\n\n"
                f"Accept: {link}\n\nThis invitation expires in 14 days."
            ),
        )
        messages.success(request, f"Invitation sent to {invite.email}.")

    def _remove(self, request: HttpRequest, membership: CompanyMembership) -> None:
        target = self._target(request, membership)
        membership_service.remove_member(
            actor=request.user, actor_membership=membership, target=target
        )
        messages.success(request, "Member removed.")

    def _target(self, request: HttpRequest, membership: CompanyMembership) -> CompanyMembership:
        """Resolve the target membership **within this company only**.

        Scoped by company, not looked up by primary key alone: an unscoped
        lookup here would let a member of one company act on a membership in
        another by guessing an id.
        """
        target = selectors.member_of(
            company=request.company, membership_id=request.POST.get("membership_id")
        )
        if target is None:
            raise Http404("No such member.")
        return target


@method_decorator(login_required, name="dispatch")
class AcceptInviteView(View):
    """`/employer/invites/<token>/` — bound to the invited address (SRS §2.6)."""

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        try:
            invite = membership_service.find_invite(token)
        except DomainError as exc:
            return _error(request, exc, reverse("employer:index"))

        return render(
            request,
            "employer/accept_invite.html",
            {
                "invite": invite,
                "token": token,
                "signed_in_as": request.user.email,
                # The same rule the POST enforces. The page compared the main
                # address only, so an account whose verified second address was
                # the invited one was told "different address" and shown no
                # button, though accepting would have succeeded.
                "address_matches": policies.invite_matches(invite=invite, user=request.user),
            },
        )

    def post(self, request: HttpRequest, token: str) -> HttpResponse:
        try:
            membership = membership_service.accept_invite(user=request.user, token=token)
        except DomainError as exc:
            restrictions.flash(request, exc)
            return redirect("employer:accept_invite", token=token)

        messages.success(request, f"You have joined {membership.company.name}.")
        return redirect("employer:overview", company_slug=membership.company.slug)


@method_decorator(login_required, name="dispatch")
class ProfileView(View):
    """`/employer/<slug>/profile/` — the public company page, from the inside.

    A company page carrying only a name is the reason candidates leave for the
    employer's own website, and every one of those is an application the
    platform never sees. Editing is gated on `company.edit`, and the write goes
    through the same optimistic lock everything else does.
    """

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = policies.require_section(request.membership, "company")
        return render(request, "employer/profile.html", self._context(request, membership))

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = policies.require_membership(request.membership)
        back = reverse("employer:profile", args=[company_slug])

        try:
            policies.require_can_edit_company(membership)
            company_service.update_company(
                actor=request.user,
                company=request.company,
                row_version=int(request.POST.get("row_version") or -1),
                name=(request.POST.get("name") or request.company.name).strip(),
                legal_name=(request.POST.get("legal_name") or "").strip(),
                website=(request.POST.get("website") or "").strip(),
                tagline=(request.POST.get("tagline") or "").strip(),
                size_band=(request.POST.get("size_band") or "").strip(),
                founded=(request.POST.get("founded") or "").strip(),
                about=request.POST.get("about") or "",
                industry_id=request.POST.get("industry_id"),
                location_id=request.POST.get("location_id"),
            )
        except DomainError as exc:
            return _error(request, exc, back)

        messages.success(request, "Company profile updated.")
        return redirect(back)

    def _context(self, request: HttpRequest, membership: CompanyMembership) -> dict:
        from apps.taxonomy import selectors as taxonomy_selectors

        return {
            "company": request.company,
            "membership": membership,
            "can_edit": Grant.COMPANY_EDIT in membership.grants,
            "industries": taxonomy_selectors.industries(),
            "locations": taxonomy_selectors.locations_in("NP"),
            "about_text": "\n\n".join(request.company.about or []),
        }


@method_decorator(login_required, name="dispatch")
class UpdatesView(View):
    """`/employer/<slug>/updates/` — news and events for followers (FOL-03).

    Readable by anybody who may open the company section; writable with
    `company.edit`, the same grant as the public profile the updates appear on.
    """

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        from apps.companies.models import CompanyFollow, CompanyUpdate

        membership = policies.require_section(request.membership, "company")
        return render(
            request,
            "employer/updates.html",
            {
                "company": request.company,
                "can_edit": Grant.COMPANY_EDIT in membership.grants,
                "updates": CompanyUpdate.objects.filter(company=request.company)[:30],
                "followers": CompanyFollow.objects.filter(company=request.company).count(),
            },
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        import datetime as dt

        from django.utils import timezone

        from apps.companies.models import CompanyUpdate
        from apps.companies.services import updates as update_service

        membership = policies.require_membership(request.membership)
        back = reverse("employer:updates", args=[company_slug])
        try:
            if request.POST.get("command") == "withdraw":
                update = CompanyUpdate.objects.filter(
                    company=request.company, public_id=as_uuid(request.POST.get("update"))
                ).first()
                if update is None:
                    raise Http404
                update_service.withdraw(actor=request.user, membership=membership, update=update)
                messages.success(request, "Update withdrawn from your company page.")
                return redirect(back)

            starts_at = None
            raw = (request.POST.get("starts_at") or "").strip()
            if raw:
                try:
                    starts_at = timezone.make_aware(dt.datetime.fromisoformat(raw))
                except ValueError as exc:
                    raise DomainError("Enter the event's date and time.") from exc
            update_service.publish(
                actor=request.user,
                membership=membership,
                company=request.company,
                kind=request.POST.get("kind") or "",
                title=request.POST.get("title") or "",
                body=request.POST.get("body") or "",
                starts_at=starts_at,
                location=request.POST.get("location") or "",
            )
        except DomainError as exc:
            return _error(request, exc, back)
        messages.success(request, "Posted. Your followers will be told.")
        return redirect(back)


@method_decorator(login_required, name="dispatch")
class VerificationView(View):
    """`/employer/<slug>/verification/` — submit documents for review."""

    def get(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        from django.conf import settings

        membership = policies.require_section(request.membership, "company")
        # With its documents and their files: the checklist below asks each
        # one whether a reviewer could open it.
        verification = selectors.verification_for(company=request.company)
        can_edit = Grant.COMPANY_EDIT in membership.grants
        # Includes a rejected company once its thirty days have passed (D-02).
        in_editable_state = company_service.is_editable(verification)
        checklist = company_service.document_checklist(verification)
        return render(
            request,
            "employer/verification.html",
            {
                "company": request.company,
                "verification": verification,
                "documents": list(selectors.verification_documents(company=request.company)),
                "kinds": list(company_service.DOCUMENT_KINDS.items()),
                # What a reviewer needs, and what is still outstanding. A
                # company that cannot see which certificate is missing waits
                # for a review whose only possible outcome is being asked for
                # it (decision D-02: that answer is the same attempt).
                # One checklist, read twice: the prose list is derived from it
                # rather than recounting the documents.
                "checklist": checklist,
                "missing_kinds": [item["label"] for item in checklist if not item["present"]],
                "max_upload_mb": settings.FILES_MAX_UPLOAD_BYTES // (1024 * 1024),
                "can_edit": can_edit,
                "in_editable_state": in_editable_state,
                "editable": can_edit and in_editable_state,
                "reapply_opens_at": company_service.reapply_opens_at(verification),
                "reapply_days": company_service.REAPPLY_AFTER_DAYS,
                "history": selectors.verification_history(company=request.company),
                "domain": domain_service.website_domain(request.company),
                "proven_domain": domain_service.proven_domain(request.company),
                "work_address": domain_service.verified_address_for(request.user, request.company),
            },
        )

    def post(self, request: HttpRequest, company_slug: str) -> HttpResponse:
        membership = policies.require_membership(request.membership)
        back = reverse("employer:verification", args=[company_slug])
        action = request.POST.get("action", "submit")
        try:
            if action == "upload":
                document = company_service.add_verification_document(
                    actor=request.user,
                    membership=membership,
                    company=request.company,
                    kind=request.POST.get("kind", ""),
                    uploaded_file=request.FILES.get("document"),
                )
                messages.success(request, f"{document.label} added.")
            elif action == "remove":
                company_service.remove_verification_document(
                    actor=request.user,
                    membership=membership,
                    company=request.company,
                    document_id=request.POST.get("document_id"),
                )
                messages.success(request, "Document removed.")
            elif action == "domain_email":
                address, token = domain_service.add_work_email(
                    actor=request.user,
                    membership=membership,
                    company=request.company,
                    email=request.POST.get("email", ""),
                )
                link = request.build_absolute_uri(reverse("accounts:verify_email", args=[token]))
                email_service.send(
                    to=address.email,
                    subject=f"Confirm {address.email} for {request.company.name} on {site_name()}",
                    body=(
                        f"Open this link to confirm that {address.email} is yours. It proves "
                        f"{request.company.name}'s domain for verification.\n\n{link}\n\n"
                        "If you did not ask for this, ignore this email."
                    ),
                )
                messages.success(
                    request,
                    f"We sent a link to {address.email}. Open it, then come back and confirm the domain.",
                )
            elif action == "prove_domain":
                record = domain_service.prove_domain(
                    actor=request.user, membership=membership, company=request.company
                )
                messages.success(request, f"{record.domain} is proven for {request.company.name}.")
            elif action == "submit":
                policies.require_can_edit_company(membership)
                company_service.submit_verification(actor=request.user, company=request.company)
                messages.success(request, "Your documents are with a reviewer.")
            else:
                raise Http404("Unknown action.")
        except DomainError as exc:
            return _error(request, exc, back)

        return redirect(back)
