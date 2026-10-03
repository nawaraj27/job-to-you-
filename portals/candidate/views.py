"""
Candidate screens, on real records (SRS §20, P3).

Replaces `apps/dashboard` and `apps/preferences`, whose views read session-backed
stand-ins. The URL names are unchanged — `dashboard:*` and `settings:*` — because
the templates, the navigation and the user menu all reference them, and renaming
a route is not what this phase is about.

Views parse input, call a policy, call one service, and choose a template. They
hold no rules and never query the ORM directly (DEVELOPER.md §1.1). Every screen
requires a signed-in user: a profile without an owner is not a thing this
product has.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from apps.accounts.services import erasure as account_erasure
from apps.accounts.services import preferences as preference_service
from apps.applications import selectors as application_selectors
from apps.candidates import selectors as candidate_selectors
from apps.candidates.services import erasure as erasure_service
from apps.candidates.services import profile as profile_service
from apps.candidates.services import sections as section_service
from apps.candidates.services import skills as skill_service
from apps.common.errors import DomainError
from apps.core.features import is_enabled
from apps.jobs import selectors as job_selectors
from apps.notifications.services import preferences as notification_preferences
from apps.resumes import selectors as resume_selectors
from apps.resumes.services import builders as builder_service

from . import context as shape

logger = logging.getLogger(__name__)

SETTINGS_PAGES = (
    {"key": "account", "url_name": "settings:index", "label": "Account"},
    {"key": "security", "url_name": "settings:security", "label": "Security"},
    {"key": "notifications", "url_name": "settings:notifications", "label": "Notifications"},
    {"key": "privacy", "url_name": "settings:privacy", "label": "Privacy"},
    {"key": "appearance", "url_name": "settings:appearance", "label": "Appearance"},
    {"key": "data", "url_name": "settings:data", "label": "Your data"},
)


def _tabs(current: str) -> list[dict]:
    return [
        {**page, "url": reverse(page["url_name"]), "current": page["key"] == current}
        for page in SETTINGS_PAGES
    ]


def _announce(
    response: HttpResponse, message: str | None, extra: dict | None = None
) -> HttpResponse:
    """Tell the live region what happened. Same contract as the rest of the app."""
    payload: dict[str, Any] = {}
    if message:
        payload["builder:announce"] = message
    if extra:
        payload.update(extra)
    if payload:
        response["HX-Trigger"] = json.dumps(payload)
    return response


def _fragment(request, template, context, *, message=None, error=None, extra=None):
    from apps.common import fragments

    # An ordinary form post gets a page, not this region alone (`common.fragments`).
    page = fragments.page_answer(
        request, message=message, error=error, fallback_url=reverse("dashboard:index")
    )
    if page is not None:
        return page

    context["flash_error"] = error
    context["flash_message"] = message if not error else None
    response = render(request, template, context)
    return _announce(response, error or message, extra)


class CandidateView(View):
    """Base for every screen here: resolves the profile once, for the owner."""

    @method_decorator(login_required)
    def dispatch(self, request: HttpRequest, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def profile(self, request: HttpRequest, *, full: bool = False):
        # Created on first visit rather than at registration: an employer
        # account never needs one.
        profile_service.get_or_create_profile(user=request.user)
        if full:
            return candidate_selectors.full_profile_for(request.user)
        return candidate_selectors.profile_for(request.user)

    def preferences(self, request: HttpRequest):
        return preference_service.get_or_create(user=request.user)

    def settings_profile(self, request: HttpRequest, *, full: bool = False):
        """The profile an account settings screen reads, or None.

        Decision D-09: settings belong to the account. Opening them used to
        create a candidate profile for anybody without one, so an employer who
        looked at their settings silently became a candidate too, and from
        then on read candidate wording and saw the candidate side. Somebody
        with a company and no profile now gets None; everybody else keeps the
        create-on-first-visit behaviour.
        """
        existing = candidate_selectors.profile_for(request.user)
        if existing is None and _is_employer_only(request.user):
            return None
        return self.profile(request, full=full)


def _is_employer_only(user) -> bool:
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import CompanyMembership

    return (
        CompanyMembership.objects.active().filter(user=user).exists()
        and not CandidateProfile.objects.filter(user=user).exists()
    )


# --- Overview -------------------------------------------------------------


class OverviewView(CandidateView):
    """`/dashboard/` — the candidate's home screen."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        is_new = profile.completeness == 0

        return render(
            request,
            "dashboard/index.html",
            {
                "page_title": "Overview",
                "is_new": is_new,
                "profile": shape.profile_payload(profile),
                "next_action": shape.next_action(profile),
                "onboarding": shape.onboarding_steps(profile),
                # Read from the flow rather than written into the copy. The
                # page advertised "twelve short questions", which was never
                # true of any version of that screen, and a promise about how
                # long something takes is the one line a first-time user checks
                # against reality within a minute of believing it.
                "guided_question_count": len(builder_service.GUIDED_STEPS),
                # Real rows where a phase has landed, empty lists where it has
                # not. Empty rather than sampled: a dashboard that invents an
                # activity feed is a dashboard nobody can trust when it is real.
                "saved": job_selectors.saved_jobs_for(profile)[:5],
                "applications": application_selectors.applications_of(profile).live()[:5],
                # Saved CVs and unsaved drafts are different things and are
                # listed apart (docs/CV-Lifecycle.md).
                "saved_cvs": resume_selectors.saved_documents_for(profile)[:5],
                "drafts": resume_selectors.drafts_for(profile)[:5],
                # A CV still being scanned or read. Home is where somebody lands
                # after leaving the progress screen, so it says where the file
                # got to rather than leaving them to remember where to look.
                "uploads_in_flight": list(resume_selectors.uploads_in_flight(profile)[:3]),
                **_cv_health(profile),
                **_matched(profile),
                "activity": _activity(profile),
            },
        )


#: Rows the home screen shows before it starts being a list.
HOME_LIMIT = 4


def _cv_health(profile) -> dict:
    """The candidate's score, and the first things to fix.

    The template has rendered a score ring, a verdict and a fix list since it
    was written, and the view passed none of them — so the home screen showed
    an empty red ring beside the words "· analysed" with no date and no
    filename. The analysis has existed since P9.
    """
    document = resume_selectors.primary_document_for(profile) or (
        resume_selectors.active_documents_for(profile).first()
    )
    analysis = resume_selectors.latest_analysis_for(document) if document else None
    if analysis is None:
        return {"report": None, "top_fixes": [], "cv_document": document}

    return {
        "report": {
            "score": analysis.score,
            "verdict": analysis.headline or analysis.verdict,
            "filename": document.name,
            "uploaded_on": analysis.created_at,
        },
        # Only what is worth doing something about: a home screen listing a
        # candidate's strengths back at them is a home screen with no next step
        # on it.
        "top_fixes": [
            finding for finding in analysis.findings.all() if finding.severity != "strength"
        ][:3],
        "cv_document": document,
    }


def _matched(profile) -> dict:
    """Roles scored against this profile.

    The section was hardcoded to an empty list with the comment "Matching is
    P13". P13 shipped: the scorer, the feed and the whole `/jobs/matched/`
    screen exist, and the home page was the only place that had not been told.
    """
    if not is_enabled("job_matching"):
        return {"matched": [], "strong_count": 0, "saved_slugs": ()}

    from apps.matching.services import recommendations

    try:
        feed = recommendations.feed_for(profile)
    except Exception:  # noqa: BLE001 - the home screen must render regardless
        logger.warning("match feed unavailable for profile %s", profile.pk, exc_info=True)
        return {"matched": [], "strong_count": 0, "saved_slugs": ()}

    from . import job_context as job_shape

    items = list(feed.items)
    shown = items[:HOME_LIMIT]

    # Shaped through the same helper the matched-jobs screen uses, so a row on
    # the home screen and a row on that screen are the same object. Passing the
    # raw recommendation renders a card with no salary, no location and no
    # score, because `components/job_row.html` reads the shaped dict.
    return {
        "matched": [
            job_shape.with_match(item.job, item.match, skill_names=_skill_names(shown))
            for item in shown
        ],
        # `Match.score` is 0-100, not 0-1. Compared against 0.7 every match in
        # the feed counted as strong, and the heading said "15 above 70%" over
        # a list whose best score was 60.
        "strong_count": sum(1 for item in items if (item.match.score or 0) >= 70),
        "saved_slugs": job_selectors.saved_job_slugs(profile),
    }


def _skill_names(items: list) -> dict[int, str]:
    """Names for every skill these matches mention, in one query."""
    from apps.taxonomy.models import Skill

    wanted: set[int] = set()
    for item in items:
        wanted |= set(item.match.matched_skill_ids)
        wanted |= set(item.match.partial_skill_ids)
        wanted |= set(item.match.missing_skill_ids)

    if not wanted:
        return {}
    return dict(Skill.objects.filter(pk__in=wanted).values_list("pk", "name"))


#: What an application event is called on a candidate's own screen, and which
#: glyph goes beside it. Only the ones a candidate can do something about:
#: everything else is noise on a home page.
ACTIVITY_LABELS: dict[str, tuple[str, str]] = {
    "application.submitted": ("clipboard", "You applied to {job} at {company}"),
    # Neutral, and with the stage as the candidate may see it (FR-6.10): "moved
    # you on" was shown for a move to "Not proceeding" too.
    "application.stage_changed": (
        "arrow-right",
        "{company} moved your {job} application to {stage}",
    ),
    "application.contact_revealed": ("eye", "{company} unlocked your contact details"),
    "application.message_sent": ("message", "{company} sent you a message about {job}"),
    "application.offer_sent": ("star", "{company} made you an offer for {job}"),
    "application.withdrawn": ("close", "You withdrew from {job}"),
}


def _activity(profile) -> list[dict]:
    """What has happened on this account lately.

    Their own applications moving, which is the only activity a candidate can
    act on. Employer-side profile reads live on the "who viewed me" screen,
    where they have the context that makes them mean something.

    Shaped here rather than in the template: the row carries a resolved URL, a
    sentence and a glyph, and working any of those out in a template is how a
    home page ends up with a `{% url %}` that cannot reverse.
    """
    from apps.applications.models import ApplicationEvent

    rows = (
        ApplicationEvent.objects.filter(application__candidate=profile, visible_to_candidate=True)
        .select_related("application__job__company", "to_stage")
        .order_by("-created_at")[: HOME_LIMIT * 2]
    )

    shaped: list[dict] = []
    for event in rows:
        known = ACTIVITY_LABELS.get(event.type)
        if known is None:
            continue
        icon, sentence = known
        job = event.application.job
        shaped.append(
            {
                "icon": icon,
                "text": sentence.format(
                    job=job.title,
                    company=job.company.name,
                    stage=event.to_stage.public_label if event.to_stage_id else "a new stage",
                ),
                "when": event.created_at,
                "url": reverse("applications:detail", args=[str(event.application.public_id)]),
            }
        )
        if len(shaped) >= HOME_LIMIT:
            break
    return shaped


# --- Profile --------------------------------------------------------------


def _profile_context(request, profile, *, editing: str = "") -> dict:
    return {
        "page_title": "Profile",
        "profile": shape.profile_payload(profile),
        "work_types": shape.WORK_TYPE_LABELS,
        "seeking_options": shape.SEEKING_LABELS,
        "visibility_options": shape.VISIBILITY_OPTIONS,
        "suggested_skills": skill_service.suggestions_for(profile),
        "editing": editing,
    }


class ProfileView(CandidateView):
    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request, full=True)
        return render(request, "dashboard/profile.html", _profile_context(request, profile))


class ProfileCommandView(CandidateView):
    """Every edit the profile screen can make.

    One endpoint with a `command` field rather than fifteen routes: the screen
    swaps one region after any change — because almost every change also moves
    the completeness meter — so every command has the same response shape.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request)
        command = (request.POST.get("command") or "").strip()
        handler = getattr(self, f"do_{command}", None)
        if handler is None:
            return HttpResponse(f"Unknown command: {command!r}", status=400)

        message = error = None
        extra: dict[str, Any] = {}
        try:
            message = handler(request, profile)
        except DomainError as exc:
            error = exc.message
            suggestions = exc.details.get("suggestions") if exc.details else None
            if suggestions:
                error = f"{error} Did you mean: {', '.join(suggestions)}?"

        profile = candidate_selectors.full_profile_for(request.user)
        section = request.POST.get("section", "") if error else ""
        return _fragment(
            request,
            "dashboard/_profile_body.html",
            _profile_context(request, profile, editing=section),
            message=message,
            error=error,
            extra=extra,
        )

    # Each handler returns the confirmation sentence, or raises DomainError.

    def do_basics(self, request, profile) -> str:
        profile_service.update_basics(
            profile=profile,
            actor=request.user,
            expected_version=_version(request),
            headline=request.POST.get("headline", ""),
            summary=request.POST.get("summary", ""),
            phone=request.POST.get("phone", ""),
            location_id=request.POST.get("location_id") or None,
        )
        return "Profile updated"

    def do_seeking(self, request, profile) -> str:
        profile_service.update_seeking(
            profile=profile,
            actor=request.user,
            expected_version=_version(request),
            seeking=request.POST.get("seeking", ""),
            notice_period_days=request.POST.get("notice_period_days", ""),
            salary_min=request.POST.get("salary_min", ""),
            salary_max=request.POST.get("salary_max", ""),
            currency=(request.POST.get("currency") or "").upper(),
            period=request.POST.get("period", ""),
        )
        return "Saved"

    def do_open_to(self, request, profile) -> str:
        profile_service.update_seeking(
            profile=profile, actor=request.user, open_to=request.POST.getlist("open_to")
        )
        return "Preferences saved"

    def do_visibility(self, request, profile) -> str:
        profile_service.update_privacy(
            profile=profile, actor=request.user, visibility=request.POST.get("value", "")
        )
        return f"Visibility updated: {shape.visibility_summary(profile)['label']}"

    def do_add_skill(self, request, profile) -> str:
        record = skill_service.add_skill(
            profile=profile,
            actor=request.user,
            text=request.POST.get("skill", ""),
            years=request.POST.get("years", ""),
            level=request.POST.get("level", ""),
        )
        return f"Added {record.skill.name}"

    def do_remove_skill(self, request, profile) -> str:
        skill_service.remove_skill(
            profile=profile, actor=request.user, skill_id=_int(request.POST.get("skill_id"))
        )
        return "Skill removed"

    def do_add_entry(self, request, profile) -> str:
        section = request.POST.get("section", "")
        section_service.add_entry(
            profile=profile,
            actor=request.user,
            section=section,
            **_entry_fields(request),
        )
        return f"{section.capitalize()} added"

    def do_update_entry(self, request, profile) -> str:
        section = request.POST.get("section", "")
        section_service.update_entry(
            profile=profile,
            actor=request.user,
            section=section,
            entry_id=_int(request.POST.get("entry_id")),
            **_entry_fields(request),
        )
        return f"{section.capitalize()} updated"

    def do_remove_entry(self, request, profile) -> str:
        section = request.POST.get("section", "")
        section_service.remove_entry(
            profile=profile,
            actor=request.user,
            section=section,
            entry_id=_int(request.POST.get("entry_id")),
        )
        return f"{section.capitalize()} removed"


#: Fields a section form may post. Anything else is dropped here as well as in
#: the service — the service is the guarantee, this keeps the payload readable.
_ENTRY_FIELDS = (
    "title",
    "org_name",
    "start_date",
    "end_date",
    "is_current",
    "summary",
    "institution",
    "qualification",
    "field_of_study",
    "name",
    "issuer",
    "issued_on",
    "expires_on",
    "credential_id",
    "credential_url",
    "url",
    "kind",
    "label",
    "language_id",
    "proficiency",
    "awarded_on",
    "started_on",
    "finished_on",
)


def _entry_fields(request) -> dict[str, Any]:
    return {
        field: request.POST.get(field)
        for field in _ENTRY_FIELDS
        if request.POST.get(field) is not None
    }


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _version(request) -> int | None:
    raw = request.POST.get("row_version")
    return int(raw) if raw and raw.isdigit() else None


# --- Settings -------------------------------------------------------------


class SettingsView(CandidateView):
    """A settings screen: tabs, a title, and whatever `context()` returns."""

    template_name = ""
    page_key = ""
    page_title = ""
    page_text = ""

    def context(self, request) -> dict:
        return {}

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "page_title": self.page_title,
                "page_text": self.page_text,
                "settings_tabs": _tabs(self.page_key),
                **self.context(request),
            },
        )


class AccountView(SettingsView):
    template_name = "settings/account.html"
    page_key = "account"
    page_title = "Account"
    page_text = (
        "How we reach you, and how the interface is addressed to you. "
        "Your public details live on your profile."
    )

    def context(self, request) -> dict:
        return account_context(request, self.preferences(request), self.settings_profile(request))


def account_context(request, preferences, profile) -> dict:
    return {
        "prefs": shape.preferences_payload(preferences, profile),
        # The phone number lives on the candidate profile and is shown only to
        # employers the person applied to; an account without one has no field.
        "has_profile": profile is not None,
        "languages": preference_service.language_choices(),
        "timezones": preference_service.TIMEZONES,
        "signed_in": request.user.is_authenticated,
        "settings_tabs": _tabs("account"),
    }


class AccountCommandView(CandidateView):
    def post(self, request: HttpRequest) -> HttpResponse:
        preferences = self.preferences(request)
        profile = self.settings_profile(request)
        message = error = None

        if (request.POST.get("command") or "") != "save":
            return HttpResponse("Unknown command", status=400)

        try:
            preference_service.update(
                preferences=preferences,
                actor=request.user,
                group="account",
                locale=request.POST.get("language", ""),
                timezone=request.POST.get("timezone", ""),
            )
            if request.POST.get("phone") is not None and profile is not None:
                profile_service.update_basics(
                    profile=profile, actor=request.user, phone=request.POST.get("phone", "")
                )
            message = "Account details saved"
        except DomainError as exc:
            error = exc.message

        preferences.refresh_from_db()
        if profile is not None:
            profile.refresh_from_db()
        return _fragment(
            request,
            "settings/_account_body.html",
            account_context(request, preferences, profile),
            message=message,
            error=error,
        )


class NotificationsView(SettingsView):
    template_name = "settings/notifications.html"
    page_key = "notifications"
    page_title = "Notifications"
    page_text = (
        "Choose what reaches you and where. Switching something off here stops it "
        "appearing in your notifications too; it isn't collected and hidden."
    )

    def context(self, request) -> dict:
        return notifications_context(request)


def notifications_context(request) -> dict:
    rows = notification_preferences.rows_for(
        user=request.user, role=notification_preferences.role_for(request.user)
    )
    return {
        "rows": rows,
        "channels": [
            {"key": "in_app", "label": "In app"},
            {"key": "email", "label": "Email"},
        ],
        "silent_count": len([row for row in rows if row["silent"]]),
        "all_off": all(row["silent"] for row in rows),
        "quiet": notification_preferences.quiet_hours_for(request.user),
        "settings_tabs": _tabs("notifications"),
    }


class NotificationsCommandView(CandidateView):
    def post(self, request: HttpRequest) -> HttpResponse:
        command = request.POST.get("command", "")
        message = error = None

        try:
            if command == "channel":
                event = request.POST.get("event", "")
                channel = request.POST.get("channel", "")
                on = request.POST.get("on") == "true"
                notification_preferences.set_channel(
                    user=request.user,
                    actor=request.user,
                    event_key=event,
                    channel=channel,
                    on=on,
                )
                label, _text = notification_preferences.wording_for(
                    event, notification_preferences.role_for(request.user)
                )
                where = "email" if channel == "email" else "in app"
                message = f"{label} {where}: {'on' if on else 'off'}"

            elif command in ("quiet_hours", "quiet_clear"):
                clearing = command == "quiet_clear"
                window = notification_preferences.set_quiet_hours(
                    user=request.user,
                    actor=request.user,
                    start="" if clearing else request.POST.get("quiet_from", ""),
                    end="" if clearing else request.POST.get("quiet_to", ""),
                )
                message = (
                    f"Quiet hours: {window['from']} to {window['to']}"
                    if window
                    else "Quiet hours switched off"
                )

            elif command in ("all_on", "all_off"):
                notification_preferences.set_all(
                    user=request.user, actor=request.user, on=command == "all_on"
                )
                message = (
                    "Everything switched on" if command == "all_on" else "Everything switched off"
                )
            else:
                return HttpResponse("Unknown command", status=400)
        except KeyError:
            error = "That isn't a notification we send."
        except DomainError as exc:
            error = exc.message

        return _fragment(
            request,
            "settings/_notifications_body.html",
            notifications_context(request),
            message=message,
            error=error,
        )


class PrivacyView(SettingsView):
    template_name = "settings/privacy.html"
    page_key = "privacy"
    page_title = "Privacy"
    page_text = (
        "Who can see you, and what they see. These settings take effect "
        "immediately. There is nothing to publish."
    )

    def context(self, request) -> dict:
        return privacy_context(request, self.preferences(request), self.settings_profile(request))


def privacy_context(request, preferences, profile) -> dict:
    if profile is None:
        # An employer account with no candidate profile: the template's
        # employer branch, which it always had and nothing ever selected.
        return {
            "prefs": shape.preferences_payload(preferences, None),
            "role": "employer",
            "settings_tabs": _tabs("privacy"),
        }
    # Indexing is meaningless while the profile is not public, so the switch
    # says so rather than sitting there accepting a change that does nothing.
    disable_indexing = ""
    if profile.visibility != shape.Visibility.EVERYONE:
        disable_indexing = "Available once your profile is visible to anyone with the link."

    from apps.candidates.models import BlockedCompany

    return {
        "prefs": shape.preferences_payload(preferences, profile),
        "role": "candidate",
        "visibility_options": shape.VISIBILITY_OPTIONS,
        "visibility": shape.visibility_summary(profile),
        "disable_indexing": disable_indexing,
        # The service could always lift a block; nothing on any screen could,
        # and nothing listed who had been blocked (Talent report item 4).
        "blocked": BlockedCompany.objects.filter(profile=profile)
        .select_related("company")
        .order_by("-created_at"),
        "settings_tabs": _tabs("privacy"),
    }


#: Which switch belongs to which record. Privacy spans two tables — profile
#: visibility and product analytics are genuinely different decisions — and the
#: screen should not have to know that.
_PRIVACY_ON_PROFILE = {"allow_contact", "show_salary", "search_indexing"}


class PrivacyCommandView(CandidateView):
    def post(self, request: HttpRequest) -> HttpResponse:
        preferences = self.preferences(request)
        profile = self.settings_profile(request)
        command = request.POST.get("command", "")
        message = error = None

        if profile is None and not (
            command == "toggle" and request.POST.get("key") == "share_analytics"
        ):
            # Only the account-level switch exists without a candidate profile.
            return HttpResponse("Unknown command", status=400)

        try:
            if command == "visibility":
                profile_service.update_privacy(
                    profile=profile, actor=request.user, visibility=request.POST.get("value", "")
                )
                message = f"Visibility updated: {shape.visibility_summary(profile)['label']}"

            elif command == "toggle":
                key = request.POST.get("key", "")
                if key in _PRIVACY_ON_PROFILE:
                    profile_service.update_privacy(
                        profile=profile, actor=request.user, **{key: not getattr(profile, key)}
                    )
                    now = getattr(profile, key)
                elif key == "share_analytics":
                    preference_service.toggle(
                        preferences=preferences, actor=request.user, field=key
                    )
                    now = preferences.share_analytics
                else:
                    return HttpResponse("Unknown switch", status=400)
                message = f"Saved: {'on' if now else 'off'}"
            elif command == "unblock":
                from apps.candidates.models import BlockedCompany
                from apps.candidates.services import blocking

                # Scoped to this profile, so another candidate's block id finds
                # nothing and changes nothing.
                raw = request.POST.get("block_id", "")
                record = (
                    BlockedCompany.objects.filter(profile=profile, pk=int(raw))
                    .select_related("company")
                    .first()
                    if raw.isdigit()
                    else None
                )
                if record is None:
                    error = "That company is not on your blocked list."
                else:
                    blocking.unblock_company(
                        profile=profile, actor=request.user, company=record.company
                    )
                    message = (
                        f"{record.company.name} is unblocked. They can find your profile "
                        "again only if your visibility allows it; access you withdrew is "
                        "not restored."
                    )
            else:
                return HttpResponse("Unknown command", status=400)
        except DomainError as exc:
            error = exc.message

        if profile is not None:
            profile.refresh_from_db()
        preferences.refresh_from_db()
        return _fragment(
            request,
            "settings/_privacy_body.html",
            privacy_context(request, preferences, profile),
            message=message,
            error=error,
        )


class AppearanceView(SettingsView):
    template_name = "settings/appearance.html"
    page_key = "appearance"
    page_title = "Appearance"
    page_text = "Changes apply as you make them, to this page as well as the rest of the product."

    def context(self, request) -> dict:
        return appearance_context(request, self.preferences(request))


def appearance_context(request, preferences) -> dict:
    return {
        "prefs": shape.preferences_payload(preferences, None),
        "themes": shape.THEME_OPTIONS,
        "accessibility": shape.accessibility_rows(preferences),
        "settings_tabs": _tabs("appearance"),
    }


class AppearanceCommandView(CandidateView):
    def post(self, request: HttpRequest) -> HttpResponse:
        preferences = self.preferences(request)
        command = request.POST.get("command", "")
        message = error = None

        try:
            if command == "theme":
                value = request.POST.get("value", "")
                preference_service.update(
                    preferences=preferences, actor=request.user, group="appearance", theme=value
                )
                label = next(
                    (theme["label"] for theme in shape.THEME_OPTIONS if theme["key"] == value),
                    value,
                )
                message = f"Theme: {label}"

            elif command == "toggle":
                key = request.POST.get("key", "")
                preference_service.toggle(preferences=preferences, actor=request.user, field=key)
                label = next(
                    (item["label"] for item in shape.ACCESSIBILITY if item["key"] == key), key
                )
                message = f"{label}: {'on' if getattr(preferences, key) else 'off'}"
            else:
                return HttpResponse("Unknown command", status=400)
        except DomainError as exc:
            error = exc.message

        preferences.refresh_from_db()
        # <html> carries the accessibility classes and sits outside every swap
        # target, so the page it is on has to be told directly.
        return _fragment(
            request,
            "settings/_appearance_body.html",
            appearance_context(request, preferences),
            message=message,
            error=error,
            extra={
                "prefs:changed": {
                    "classes": preference_service.document_classes(preferences),
                    "theme": preferences.theme,
                }
            },
        )


#: What the account holds and which part of the product owns it. Read by both
#: the export and the delete screen, so the two cannot describe different
#: things — which is the failure that makes an export untrustworthy.
DATA_SOURCES = (
    ("profile", "Your profile", "Headline, contact details, skills, history and links."),
    (
        "applications",
        "Applications",
        "Roles you applied to, your answers, and the stage you were last told.",
    ),
    ("cover_letters", "Cover letters", "Letters you saved or wrote while applying."),
    ("messages", "Messages", "Your conversations with employers, their replies included."),
    ("profile_views", "Profile views", "Which companies opened your profile, and on which days."),
    ("settings", "Settings", "Appearance, notifications and privacy choices."),
)


class DataView(SettingsView):
    template_name = "settings/data.html"
    page_key = "data"
    page_title = "Your data"
    page_text = (
        "Take a copy whenever you like, erase your profile, or close the "
        "account for good. The first two run immediately; closing the account "
        "waits fourteen days so you can change your mind."
    )

    def context(self, request) -> dict:
        return data_context(request, self.settings_profile(request, full=True))


def data_context(request, profile) -> dict:
    export: dict[str, Any]
    if profile is None:
        # An employer account with no candidate profile (D-09): nothing of a
        # candidate's to count or erase, and opening the page creates nothing.
        export = {
            key: []
            for key in (
                *erasure_service.PROFILE_SECTIONS,
                "applications",
                "cover_letters",
                "messages",
                "profile_views",
            )
        }
    else:
        export = erasure_service.export_profile(profile)
    return {
        "has_profile": profile is not None,
        "sources": [
            {"key": key, "label": label, "text": text} for key, label, text in DATA_SOURCES
        ],
        "counts": {
            # Profile sections only: the export now also carries applications,
            # letters, messages and views, and each has its own row.
            "profile": sum(len(export[key]) for key in erasure_service.PROFILE_SECTIONS),
            "applications": len(export["applications"]),
            "cover_letters": len(export["cover_letters"]),
            "messages": len(export["messages"]),
            "profile_views": sum(row["times"] for row in export["profile_views"]),
            "settings": len(shape.ACCESSIBILITY) + 2,
        },
        "settings_tabs": _tabs("data"),
        "wiped": profile is not None and profile.completeness == 0 and not profile.skills.exists(),
        # The account-erasure half of the screen (SRS §19.4). A scheduled date
        # rather than a boolean, because "when" is the thing somebody who has
        # requested it comes back to check.
        "erasure_scheduled_for": (
            account_erasure.erasure_due_on(request.user)
            if request.user.erasure_requested_at
            else None
        ),
        "erasure_confirmation": account_erasure.CONFIRMATION,
        "erasure_grace_days": account_erasure.GRACE_DAYS,
    }


class ExportView(CandidateView):
    """The export itself: a real file, built from the real records."""

    def get(self, request: HttpRequest) -> HttpResponse:
        profile = self.profile(request, full=True)
        response = JsonResponse(
            erasure_service.export_profile(profile), json_dumps_params={"indent": 2}
        )
        response["Content-Disposition"] = 'attachment; filename="careerai-export.json"'
        return response


class ExportArchiveView(CandidateView):
    """The export with the candidate's original files (FR-1.9), as one zip."""

    def get(self, request: HttpRequest) -> HttpResponse:
        from django.http import FileResponse

        profile = self.profile(request, full=True)
        archive = erasure_service.build_archive(
            profile=profile, actor=request.user, request_id=getattr(request, "request_id", "")
        )
        response = FileResponse(
            archive,
            as_attachment=True,
            filename="careerai-export.zip",
            content_type="application/zip",
        )
        response["Cache-Control"] = "private, no-store"
        return response


class DataCommandView(CandidateView):
    """Three commands, and the difference between them is the whole screen.

    `delete` empties the profile and leaves the account open. `erase` closes
    the account itself, after fourteen days in which it can be undone
    (SRS §19.4). A button that says "delete everything" and means "delete some
    of it" is worse than no button, so the two are separate commands with
    separate wording and separate confirmations.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        profile = self.settings_profile(request)
        message = error = None
        command = request.POST.get("command")

        if command not in {"delete", "erase", "cancel_erasure"}:
            return HttpResponse("Unknown command", status=400)
        if command == "delete" and profile is None:
            return HttpResponse("There is no candidate profile to erase.", status=400)

        try:
            if command == "delete" and profile is not None:
                result = erasure_service.erase_profile_data(
                    profile=profile,
                    actor=request.user,
                    confirmation=request.POST.get("confirm", ""),
                )
                message = (
                    f"Profile erased: {result.rows_deleted} record"
                    f"{'s' if result.rows_deleted != 1 else ''} removed. "
                    "Your account is still open."
                )
            elif command == "erase":
                account_erasure.request_erasure(
                    user=request.user,
                    actor=request.user,
                    confirmation=request.POST.get("confirm", ""),
                )
                request.user.refresh_from_db()
                due = account_erasure.erasure_due_on(request.user)
                message = (
                    f"Your account will be erased on {due:%d %b %Y}. "
                    "Nothing has been removed yet, and you can stop this until then."
                )
            else:
                account_erasure.cancel_erasure(user=request.user, actor=request.user)
                request.user.refresh_from_db()
                message = "Your account will not be erased."
        except DomainError as exc:
            error = exc.message

        profile = candidate_selectors.full_profile_for(request.user)
        return _fragment(
            request,
            "settings/_data_body.html",
            data_context(request, profile),
            message=message,
            error=error,
        )


@method_decorator(login_required, name="dispatch")
class InsightsView(View):
    """`/dashboard/insights/` — the candidate's own numbers (SRS §16.2).

    Computed live rather than rolled up, and §5.14 says why: one person has tens
    of applications, not thousands, and a nightly job per candidate would build a
    table larger than the thing it summarises.

    The employer-response figure is the same measurement as §16.3.3 from the
    other side, and it is here because "this employer replies to one in five" is
    a genuinely useful thing to know while job hunting — it turns silence from a
    personal verdict into a fact about a company.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        from apps.analytics.services import reports

        profile = candidate_selectors.profile_for(request.user)
        if profile is None:
            return redirect("dashboard:profile")

        return render(
            request,
            "dashboard/insights.html",
            {
                "page_title": "Your numbers",
                "insights": reports.candidate_insights(profile),
            },
        )
