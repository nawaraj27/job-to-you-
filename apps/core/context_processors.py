from typing import Any

from django.conf import settings

from .features import get_features
from .navigation import footer_nav, primary_nav, rail_nav


def _features_for(request) -> dict:
    """Resolve the flag set once per request.

    Every consumer in this module shares the result, so the navigation, the
    rail counts and `site.features` can never disagree about whether a module
    is on for this viewer — which is what a partial rollout would otherwise
    cause (a hidden link to a page the user can reach).
    """
    cached = getattr(request, "_careerai_features", None)
    if cached is None:
        cached = get_features(getattr(request, "user", None))
        request._careerai_features = cached
    return cached


def site(request):
    """Site-wide identity values, from settings (docs/BRANDING.md).

    The logo artwork lives in static/img/brand/ and is drawn only by
    components/brand_mark.html.
    """
    return {
        "site": {
            "name": settings.SITE_NAME,
            "tagline": settings.SITE_TAGLINE,
            "support_email": settings.SITE_SUPPORT_EMAIL,
        },
        "features": _features_for(request),
        "debug": settings.DEBUG,
    }


def _viewer(request, *, role: str | None = None) -> dict:
    """Who this page is being rendered for.

    Replaces the P0 persona stub (`apps/core/demo.py`), which resolved a
    *fictional* person from `?as=` and `?state=` query parameters and remembered
    the answer in the session. Two things were wrong with that by the time real
    accounts existed, and the launch audit found both:

      * an anonymous visitor could write to their own session by passing
        `?as=employer`, so any crawler that followed such a link created a
        session row and changed what the shell rendered;
      * the candidate dashboard greeted a signed-in person with the stub's
        hard-coded headline — "Backend Engineer · 3 years" — because only the
        name and email were ever overwritten with real values. Fabricated data,
        rendered as the reader's own.

    `role` forces the answer so a candidate screen stays a candidate screen for
    somebody who signed up to hire — otherwise the CV page greets the reader by
    one identity while rendering another's document.
    """
    user = getattr(request, "user", None)
    authenticated = user is not None and user.is_authenticated

    if role not in {"candidate", "employer"}:
        role = (
            "employer"
            if authenticated and getattr(user, "signed_up_to_hire", False)
            else "candidate"
        )

    if not authenticated or user is None:
        return {
            "role": role,
            "authenticated": False,
            "name": "",
            "first_name": "",
            "initials": "",
            "email": "",
            "headline": "",
        }

    name = user.get_full_name()
    return {
        "role": role,
        "authenticated": True,
        "name": name,
        "first_name": name.split(" ")[0],
        "initials": user.initials,
        "email": user.email,
        # The candidate's own headline, or nothing. An employer has no headline
        # and gets an empty string rather than a borrowed one.
        "headline": _headline_for(request) if role == "candidate" else "",
        **_portals_for(request, user),
    }


def _portals_for(request, user) -> dict:
    """Which portals this person can open, in one query per request (SRS §2.1).

    "A user with a CandidateProfile is a candidate; a user with an active
    membership is an employer-side user; both can be true." The account menu
    branched on `signed_up_to_hire` instead, so a candidate who later joined a
    company was never offered the hiring side, and platform staff were never
    offered the staff area at all.
    """
    cached = getattr(request, "_careerai_portals", None)
    if cached is not None:
        return cached

    membership = getattr(request, "membership", None)
    if membership is not None:
        has_company = True
        is_platform_staff = getattr(user, "staff_role", None) is not None
    else:
        from django.db.models import Exists, OuterRef

        from apps.accounts.models import StaffRole, User
        from apps.companies.models import CompanyMembership

        row = (
            User.objects.filter(pk=user.pk)
            .annotate(
                has_company=Exists(CompanyMembership.objects.active().filter(user=OuterRef("pk"))),
                is_platform_staff=Exists(StaffRole.objects.filter(user=OuterRef("pk"))),
            )
            .values_list("has_company", "is_platform_staff")
            .first()
        ) or (False, False)
        has_company, is_platform_staff = row

    cached = {
        "has_company": bool(has_company),
        "hires": bool(has_company) or bool(getattr(user, "signed_up_to_hire", False)),
        "is_platform_staff": bool(is_platform_staff),
    }
    request._careerai_portals = cached
    return cached


def _headline_for(request) -> str:
    from apps.candidates import selectors as candidate_selectors

    profile = candidate_selectors.profile_for_request(request)
    return profile.headline if profile is not None else ""


def _rail_counts(viewer, request=None) -> dict:
    """Counts shown against rail items.

    Kept out of navigation.py so the nav stays pure structure: these are the
    numbers a real view will read from the database, and they are keyed by URL
    name so adding one never touches a template.
    """
    features = _features_for(request) if request is not None else get_features()
    counts: dict[str, int] = {}

    user = getattr(request, "user", None) if request is not None else None
    if user is not None and user.is_authenticated:
        # A real count on real rows (P10). Muted groups are excluded, so the
        # bell never counts things the person switched off — otherwise "turn
        # this off" would mean "keep sending it, quietly".
        from apps.notifications.services import feed as feed_service

        unread = feed_service.unread_count(user)
        if unread:
            counts["notifications:index"] = unread

    if viewer["role"] == "employer":
        # Real counts on real rows (P8), and only for the company this URL
        # resolved: a badge counting another tenant's applicants would be a
        # disclosure, not a number.
        membership = getattr(request, "membership", None) if request is not None else None
        if features.get("employers") and membership is not None:
            from apps.applications import selectors as application_selectors
            from apps.jobs import selectors as job_selectors

            company = membership.company
            counts["employer:applicants"] = application_selectors.counts_for(membership)["live"]
            counts["employer:job_list"] = job_selectors.counts_for_company(company)["published"]

            from apps.messaging import selectors as message_selectors

            counts["employer:messages"] = message_selectors.unread_count_for_company(membership)
        return counts

    if features.get("jobs") and request is not None:
        # A real count on a real table (P7). The `is_new` gate that used to sit
        # here came from the persona stub — a demo switch deciding whether a
        # real badge was drawn — and went with it. An account with nothing
        # saved has no `jobs:saved` key at all, which is the honest way to
        # render no badge.
        from apps.candidates import selectors as candidate_selectors
        from apps.jobs import selectors as job_selectors

        # The same profile the greeting read, and the same one the view that
        # owns the page resolved: looked up once per request, not once per
        # reader of it.
        profile = candidate_selectors.profile_for_request(request)
        if profile is not None:
            counts["jobs:saved"] = len(job_selectors.saved_job_ids(profile))

            from apps.messaging import selectors as message_selectors

            counts["messages:inbox"] = message_selectors.unread_count_for_candidate(profile)
    return counts


#: Which product each URL namespace belongs to. The rail follows the page, not
#: the session — an employer who lands on a candidate screen should see how to
#: get back, not a hiring menu wrapped around someone else's CV.
_RAIL_BY_NAMESPACE = {
    "employer": "employer",
    "applications": "candidate",
    "messages": "candidate",
    "resume": "candidate",
    "jobs": "candidate",
    "dashboard": "candidate",
}


def _company_slug(request) -> str | None:
    """The company this URL is about, if any (ADR-A18)."""
    company = getattr(request, "company", None)
    return company.slug if company is not None else None


def _hidden_routes(request) -> frozenset[str]:
    """Employer routes this member's role cannot use, so the rail does not offer them."""
    membership = getattr(request, "membership", None)
    if membership is None:
        return frozenset()
    return _hidden_routes_for(membership)


def _hidden_routes_for(membership) -> frozenset[str]:
    from apps.jobs import policies as job_policies

    hidden = set()
    if not job_policies.can_create_jobs(membership):
        hidden.add("employer:job_create")
    # The same section gates the views enforce (SRS §2.3), so the rail never
    # offers a page that answers 403.
    from apps.companies import policies as company_policies

    sections = {
        "jobs": ("employer:job_list", "employer:job_create"),
        "team": ("employer:team",),
        "company": (
            "employer:profile",
            "employer:updates",
            "employer:pipeline",
            "employer:verification",
        ),
        "billing": ("employer:billing",),
        "messages": ("employer:messages",),
        "talent": ("employer:talent",),
        "pools": ("employer:pools",),
        "analytics": ("employer:analytics",),
    }
    for section, routes in sections.items():
        if not company_policies.can_open_section(membership, section):
            hidden.update(routes)
    return frozenset(hidden)


#: Account-level pages: they belong to the person, not to either product, so
#: the rail follows who the person is (decision D-09).
_ACCOUNT_NAMESPACES = frozenset({"settings", "notifications"})


def _account_context(request) -> tuple[str, Any]:
    """The rail an account-level page shows, and the membership it is for.

    Decision D-09: notification settings stay on the account, but the page is
    drawn in the side the person works on. Somebody with a company and no
    candidate profile got the whole candidate rail — CV builder, matched jobs,
    applications — and no way back to their company. An employer gets the
    employer rail: for their one company, or with the company chooser when
    they belong to several. Somebody with both keeps the side they signed up
    for.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return "candidate", None
    from apps.candidates import selectors as candidate_selectors
    from apps.companies.models import CompanyMembership

    memberships = list(
        CompanyMembership.objects.active().filter(user=user).select_related("company")[:2]
    )
    if not memberships:
        return "candidate", None
    has_profile = candidate_selectors.profile_for_request(request) is not None
    if has_profile and not getattr(user, "signed_up_to_hire", False):
        return "candidate", None
    return "employer", memberships[0] if len(memberships) == 1 else None


def _rail_role(request, viewer) -> str:
    match = getattr(request, "resolver_match", None)
    if match is not None:
        role = _RAIL_BY_NAMESPACE.get(match.app_name)
        if role:
            return role
        if match.app_name in _ACCOUNT_NAMESPACES:
            return _account_context(request)[0]
    return viewer["role"]


def navigation(request):
    # Resolve once to learn the account's own role, then re-resolve for the role
    # the page actually belongs to, so the name, the rail and the content agree.
    role = _rail_role(request, _viewer(request))
    viewer = _viewer(request, role=role)
    counts = _rail_counts(viewer, request)
    features = _features_for(request)
    company_slug = _company_slug(request)
    hidden = _hidden_routes(request)
    match = getattr(request, "resolver_match", None)
    on_account_page = match is not None and match.app_name in _ACCOUNT_NAMESPACES
    if role == "employer" and company_slug is None and on_account_page:
        # An account page has no company in its URL; the rail is for the
        # person's company, gated exactly as it is on that company's pages.
        _role, membership = _account_context(request)
        if membership is not None:
            company_slug = membership.company.slug
            hidden = _hidden_routes_for(membership)
    return {
        "viewer": viewer,
        "rail_role": role,
        "primary_nav": primary_nav(features),
        # The employer rail is company-scoped (ADR-A18); the slug comes from
        # whichever company the URL resolved, never from the session.
        "rail_nav": rail_nav(role, features, company_slug=company_slug, hidden=hidden),
        "rail_counts": counts,
        "unread_notifications": counts.get("notifications:index", 0),
        "footer_nav": footer_nav(features),
        # Candidates only: nobody else uploads a CV to follow, and the staff
        # and employer screens sit under a query budget (NFR-2).
        "cv_progress": _cv_progress(request) if role == "candidate" else None,
    }


#: Pages that show the upload's own live status; the top-bar chip would say the
#: same thing twice and poll twice (CVP-04, "no duplicate polling").
_PROGRESS_OWN_PAGES = {"resume:upload", "resume:processing"}


def _cv_progress(request):
    """The top-bar chip for a CV this session is watching (CVP-04)."""
    match = getattr(request, "resolver_match", None)
    if match is not None and match.view_name in _PROGRESS_OWN_PAGES:
        return None
    from apps.resumes import progress

    return progress.current(request)


def preferences(request):
    """Accessibility choices, rendered onto <html> before the first paint.

    Lives here rather than in a script so the preference is in effect when the
    page appears, instead of arriving a frame later as a visible reflow.

    An anonymous visitor has no preference row and gets the defaults. Creating
    one on a marketing page view would put a row in the table for every crawler.
    """
    from apps.accounts.models import UserPreferences
    from apps.accounts.services import preferences as preference_service

    user = getattr(request, "user", None)
    stored = None
    if user is not None and user.is_authenticated:
        stored = UserPreferences.objects.filter(user=user).first()

    return {
        "prefs_classes": preference_service.document_classes(stored),
        "prefs_theme": stored.theme if stored else UserPreferences.Theme.SYSTEM,
    }
