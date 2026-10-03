"""
Single source of truth for site navigation.

Templates never hardcode links. Header, mobile drawer, footer, and both
signed-in rails all render from the structures built here, so adding a module
later means editing one Python file — the shell does not change.

Three surfaces:

    primary_nav()    the public masthead
    rail_nav(role)   the signed-in left rail, which differs completely
                     between a candidate and an employer
    footer_nav()     the site footer

Items whose feature flag is off are dropped entirely (see features.py), and any
item pointing at a URL name that is not registered yet is dropped defensively
so a half-wired route can never render a dead link.
"""

from dataclasses import dataclass, field

from django.urls import NoReverseMatch, reverse

from .features import get_features


@dataclass(frozen=True)
class NavItem:
    label: str
    url_name: str | None = None
    feature: str | None = None
    description: str = ""
    icon: str = ""
    children: tuple["NavItem", ...] = field(default_factory=tuple)
    #: Reverse arguments. Employer routes are company-scoped (ADR-A18), so
    #: their links carry the slug the request resolved.
    args: tuple[str, ...] = ()

    @property
    def url(self) -> str | None:
        if not self.url_name:
            return None
        try:
            return reverse(self.url_name, args=self.args)
        except NoReverseMatch:
            return None


# --- Public navigation --------------------------------------------------
#
# Five items, because a masthead that needs a second row has stopped being
# navigation and become a sitemap. "Home" is not listed: the wordmark is the
# home link on every site the reader has ever used, and repeating it costs a
# slot. The mobile drawer does list it, where there is no wordmark to click.

PRIMARY_NAV: tuple[NavItem, ...] = (
    NavItem(
        label="CV tools",
        feature="resume",
        children=(
            NavItem(
                "Assisted CV",
                "resume:ai",
                "resume",
                "Describe your work; the assistant asks questions and drafts your CV.",
                "lightbulb",
            ),
            NavItem(
                "Analyse my CV",
                "resume:upload",
                "resume",
                "Upload a CV and see what a hiring system sees.",
                "chart",
            ),
            NavItem(
                "CV builder",
                "resume:builder",
                "resume",
                "Guided, full control, or written for you.",
                "document",
            ),
            NavItem(
                "Templates",
                "resume:templates",
                "resume",
                "Professional layouts that parse cleanly.",
                "layout",
            ),
        ),
    ),
    NavItem(
        label="Jobs",
        feature="jobs",
        children=(
            NavItem(
                "Browse jobs",
                "jobs:list",
                "jobs",
                "Every open role, filtered how you like.",
                "briefcase",
            ),
            NavItem(
                "Matched to my CV",
                "jobs:recommended",
                "job_matching",
                "Roles scored against your actual skills.",
                "target",
            ),
            NavItem(
                "Saved jobs",
                "jobs:saved",
                "jobs",
                "The shortlist you're working through.",
                "bookmark",
            ),
            NavItem(
                "My applications",
                "applications:index",
                "applications",
                "One pipeline, from applied to offer.",
                "clipboard",
            ),
        ),
    ),
    NavItem(
        label="For employers",
        feature="employers",
        children=(
            NavItem(
                "Post a job",
                "employer:index",
                "employers",
                "Reach candidates matched on skills, not keywords.",
                "plus",
            ),
            NavItem(
                "Applicant pipeline",
                "employer:index",
                "employers",
                "Review, shortlist and track every applicant.",
                "users",
            ),
            NavItem(
                "Create a company account",
                "employer:create",
                "employers",
                "Verify your business and start hiring.",
                "building",
            ),
        ),
    ),
    NavItem(label="Pricing", url_name="core:pricing", feature="pricing"),
)


# --- Signed-in rails ----------------------------------------------------
#
# Candidate and employer are not two skins of one dashboard; they are two
# products that share a shell. Keeping the rails separate here is what stops
# employer screens sprouting {% if user.signed_up_to_hire %} branches later.

CANDIDATE_RAIL: tuple[tuple[str, tuple[NavItem, ...]], ...] = (
    ("", (NavItem("Overview", "dashboard:index", "dashboard", icon="home"),)),
    (
        "My CV",
        (
            NavItem("Assisted CV", "resume:ai", "resume", icon="lightbulb"),
            NavItem("Latest analysis", "resume:analysis", "resume", icon="chart"),
            NavItem("CV builder", "resume:builder", "resume", icon="document"),
            # A CV being scanned and read is work the candidate is waiting on,
            # and until this entry existed the only route to it was the
            # confirmation alert shown once after the upload. Anyone who
            # looked at a job in the meantime lost the thread: the file was
            # being read the whole time and the screen saying so was
            # unreachable.
            NavItem("Uploads", "resume:upload", "resume", icon="upload"),
            NavItem("Templates", "resume:templates", "resume", icon="layout"),
        ),
    ),
    (
        "Jobs",
        (
            NavItem("Matched to me", "jobs:recommended", "job_matching", icon="target"),
            NavItem("Browse all", "jobs:list", "jobs", icon="briefcase"),
            NavItem("Saved", "jobs:saved", "jobs", icon="bookmark"),
            NavItem("Applications", "applications:index", "applications", icon="clipboard"),
            NavItem("Cover letters", "letters:list", "applications", icon="document"),
            NavItem("Messages", "messages:inbox", "messaging", icon="message"),
            NavItem("Interviews", "interviews:hub", "interview", icon="video"),
            NavItem("Your numbers", "dashboard:insights", "dashboard", icon="chart"),
        ),
    ),
    (
        "Account",
        (
            NavItem("Profile", "dashboard:profile", "dashboard", icon="user"),
            NavItem("Plan", "dashboard:plan", "billing", icon="card"),
            NavItem("Notifications", "notifications:index", icon="bell"),
            NavItem("Settings", "settings:index", icon="cog"),
        ),
    ),
)

#: The employer rail is built per request, not declared here: every route is
#: company-scoped (`/employer/<slug>/...`), so the items need a slug that only
#: the request has. `rail_nav` fills it in from `request.company`.
EMPLOYER_RAIL_SPEC: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("", (("Overview", "employer:overview", "home"),)),
    (
        "Hiring",
        (
            ("Job postings", "employer:job_list", "briefcase"),
            ("Post a job", "employer:job_create", "plus"),
            ("Applicants", "employer:applicants", "users"),
            ("Messages", "employer:messages", "message"),
            ("Talent pools", "employer:pools", "bookmark"),
            ("Search candidates", "employer:talent", "search"),
            ("Interviews", "employer:interviews", "video"),
            ("Analytics", "employer:analytics", "chart"),
        ),
    ),
    (
        "Company",
        (
            ("Company profile", "employer:profile", "building"),
            ("Updates for followers", "employer:updates", "bell"),
            ("Team", "employer:team", "users"),
            ("Pipeline", "employer:pipeline", "layout"),
            ("Verification", "employer:verification", "verified"),
            ("Plan and billing", "employer:billing", "card"),
        ),
    ),
    (
        "Account",
        (
            ("Notifications", "notifications:index", "bell"),
            # Account-level, shared with the candidate side (decision D-09):
            # one set of preferences per person, reachable from either rail.
            ("Notification settings", "settings:notifications", "sliders"),
            ("Settings", "settings:index", "cog"),
        ),
    ),
)


def employer_rail(
    company_slug: str | None, *, hidden: frozenset[str] = frozenset()
) -> tuple[tuple[str, tuple[NavItem, ...]], ...]:
    """Resolve the employer rail for one company.

    Company-scoped URLs cannot be reversed without a slug, so an employer with
    no company resolved yet gets only the items that need none — which is what
    `NavItem.url` returning None already handles for everything else.

    `hidden` names routes this member's role cannot use ("Post a job" for a
    Viewer), so the rail offers nothing that would be refused on click.
    """
    groups: list[tuple[str, tuple[NavItem, ...]]] = []
    if company_slug is None:
        # No company in the URL (an account page, for somebody in several
        # companies): the way back is the company chooser (D-09).
        groups.append(
            ("", (NavItem("Your companies", "employer:index", "employers", icon="building"),))
        )
    for title, items in EMPLOYER_RAIL_SPEC:
        resolved = []
        for label, url_name, icon in items:
            if url_name in hidden:
                continue
            if url_name.startswith("employer:") and url_name != "employer:index":
                if company_slug is None:
                    continue
                resolved.append(
                    NavItem(label, url_name, "employers", icon=icon, args=(company_slug,))
                )
            else:
                resolved.append(NavItem(label, url_name, icon=icon))
        if resolved:
            groups.append((title, tuple(resolved)))
    return tuple(groups)


#: The footer: a secondary map of the public site, not a second navbar.
#:
#: Every link is a page that exists and that a visitor may open (a signed-in
#: page sends them to sign in first, as any link to it does). Feature-flagged
#: links drop out with their feature. No two links go to the same place — the
#: footer used to offer "Post a job" and "Applicant pipeline", which were one
#: URL under two names.
FOOTER_NAV: tuple[tuple[str, tuple[NavItem, ...]], ...] = (
    (
        "Product",
        (
            NavItem("Find jobs", "jobs:list", "jobs"),
            NavItem("Analyse your CV", "resume:upload", "resume"),
            NavItem("Build or improve a CV", "resume:builder", "resume"),
            NavItem("CV templates", "resume:templates", "resume"),
            NavItem("Practice interviews", "interviews:hub", "interview"),
            NavItem("Pricing", "core:pricing", "pricing"),
        ),
    ),
    (
        "For employers",
        (
            NavItem("Hiring with us", "core:employer_solutions", "employers"),
            NavItem("Employer portal", "employer:index", "employers"),
            # Company-scoped pages, through `employer:go`, which resolves the
            # signed-in member's company (the footer cannot name one).
            NavItem("Post a job", "employer:go", "employers", args=("post-job",)),
            NavItem("Manage jobs", "employer:go", "employers", args=("jobs",)),
            NavItem("Applicants", "employer:go", "employers", args=("applicants",)),
            NavItem("Company profile", "employer:go", "employers", args=("company",)),
            NavItem("Create a company account", "employer:create", "employers"),
        ),
    ),
    (
        "Company",
        (
            NavItem("About", "core:about", "about"),
            NavItem("Contact", "core:contact"),
        ),
    ),
    (
        "Legal",
        (
            NavItem("Privacy", "core:privacy"),
            NavItem("Terms", "core:terms"),
        ),
    ),
)


# --- Resolution ---------------------------------------------------------


def _visible(item: NavItem, features: dict[str, bool]) -> bool:
    if item.feature and not features.get(item.feature, False):
        return False
    if item.children:
        return any(_visible(child, features) for child in item.children)
    return item.url is not None


def _resolve(item: NavItem, features: dict[str, bool]) -> dict:
    return {
        "label": item.label,
        "url": item.url,
        "url_name": item.url_name,
        "description": item.description,
        "icon": item.icon,
        "children": [
            _resolve(child, features) for child in item.children if _visible(child, features)
        ],
    }


def _groups(source, features: dict[str, bool]) -> list[dict]:
    groups = []
    for title, items in source:
        links = [_resolve(item, features) for item in items if _visible(item, features)]
        if links:
            groups.append({"title": title, "links": links})
    return groups


def primary_nav(features: dict[str, bool] | None = None) -> list[dict]:
    features = get_features() if features is None else features
    return [_resolve(item, features) for item in PRIMARY_NAV if _visible(item, features)]


def footer_nav(features: dict[str, bool] | None = None) -> list[dict]:
    features = get_features() if features is None else features
    return _groups(FOOTER_NAV, features)


def rail_nav(
    role: str,
    features: dict[str, bool] | None = None,
    *,
    company_slug: str | None = None,
    hidden: frozenset[str] = frozenset(),
) -> list[dict]:
    """The signed-in left rail for a role: 'employer' or anything else.

    The employer rail needs a company slug because its routes are scoped by one
    (ADR-A18). Without one — an employer who has not chosen a company yet — the
    scoped items are dropped rather than rendered as dead links.
    """
    features = get_features() if features is None else features
    source = employer_rail(company_slug, hidden=hidden) if role == "employer" else CANDIDATE_RAIL
    return _groups(source, features)
