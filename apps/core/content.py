"""
Marketing content for the public pages.

Kept in Python (not in templates) so every page stays data-driven: when a CMS
or database model lands, only these functions change — the templates already
iterate over the same shapes.
"""

from django.urls import NoReverseMatch, reverse

from .features import is_enabled


def _link(url_name: str, feature: str | None = None) -> str | None:
    """Resolve a URL only if its module is live; otherwise return None so the
    caller can fall back (usually to sign-up) instead of rendering a dead link."""
    if feature and not is_enabled(feature):
        return None
    try:
        return reverse(url_name)
    except NoReverseMatch:
        return None


# ---------------------------------------------------------------------------
# Photography
#
# The marketing pages are carried by pictures of people, not by illustration or
# UI screenshots, because trust in a careers product transfers from faces.
#
# **Self-hosted since P19.** They were served from the Unsplash CDN, with a note
# here saying to self-host them before launch. The launch audit found the real
# reason it could not wait: the Content Security Policy is `img-src 'self'
# data:`, so every one of these was blocked in production and the marketing
# pages rendered with no photography at all. The files now live in
# `static/img/photos/`, which also pins the crop so a photo cannot change under
# us, and keeps the credit accurate to the version actually shown.
#
# The flag stays so a fork can go back to a CDN deliberately, with the CSP
# change that would require.
# ---------------------------------------------------------------------------

SELF_HOSTED_PHOTOS = True

_CDN = "https://images.unsplash.com/"
_WIDTHS = (640, 960, 1280, 1600, 2000)


def _photo(
    photo_id: str,
    *,
    local: str,
    alt: str,
    credit: str,
    credit_url: str,
    position: str = "50% 50%",
) -> dict:
    """Build a responsive image record.

    `position` is the object-position of the subject, set per photo so a face is
    never cropped out at narrow widths.
    """
    if SELF_HOSTED_PHOTOS:
        return {
            "remote": False,
            "local": f"img/photos/{local}",
            "alt": alt,
            "credit": credit,
            "credit_url": credit_url,
            "position": position,
        }

    base = f"{_CDN}{photo_id}?auto=format&fit=crop&q=68"
    return {
        "remote": True,
        "src": f"{base}&w=1600",
        "srcset": ", ".join(f"{base}&w={w} {w}w" for w in _WIDTHS),
        "alt": alt,
        "credit": credit,
        "credit_url": credit_url,
        "position": position,
    }


def photos() -> dict:
    """Named photographs, so a swap is one edit and never a template change."""
    return {
        "hero": _photo(
            "photo-1758270705290-62b6294dd044",
            local="hero-students.jpg",
            alt="Three students working together at a laptop in a lecture hall, "
            "one pointing at the screen.",
            credit="Vitaly Gariev",
            credit_url="https://unsplash.com/photos/kp7qkHTgSKc",
            position="60% 40%",
        ),
        "story": _photo(
            "photo-1713946598544-c3d106930f72",
            local="story-applicant.jpg",
            alt="A young woman at a desk, looking up from her laptop.",
            credit="Vitaly Gariev",
            credit_url="https://unsplash.com/photos/J8JT0DnTP3k",
            position="50% 30%",
        ),
        "audiences": _photo(
            "photo-1740065592671-9cb593ee9b78",
            local="audiences-graduates.jpg",
            alt="Graduates in caps and gowns at a commencement ceremony.",
            credit="Zacqueline Baldwin",
            credit_url="https://unsplash.com/photos/q6yC7rYyPuU",
            position="50% 40%",
        ),
        "closing": _photo(
            "photo-1758518730384-be3d205838e8",
            local="closing-offer.jpg",
            alt="Two people shaking hands across a table after an interview.",
            credit="Vitaly Gariev",
            credit_url="https://unsplash.com/photos/jEpZNyFSQwQ",
            position="50% 45%",
        ),
    }


def hero() -> dict:
    return {
        "eyebrow": "For students and job seekers",
        "title": "Everything between your CV and your next offer.",
        "subtitle": (
            "Start with the CV you already have. See it the way an applicant "
            "tracking system does, fix what is costing you interviews, and find "
            "the roles your skills actually fit, with the reasoning shown, not "
            "just a score."
        ),
        "primary_cta": {
            "label": "Upload your CV",
            "url": _link("resume:upload", "resume") or _link("accounts:signup"),
            "icon": "upload",
        },
        "secondary_cta": {
            "label": "Browse jobs",
            "url": _link("jobs:list", "jobs") or _link("accounts:signup"),
            "icon": "search",
        },
        "note": "Free to start. No card required.",
    }


def hero_preview() -> dict:
    """The sample report shown beside the hero.

    A real artefact from the product rather than a decorative graphic — it is
    rendered with the same components the analysis page uses, so it can never
    drift from what users actually get.
    """
    return {
        "filename": "priya-sharma-cv.pdf",
        "target_role": "Backend Engineer",
        "score": 78,
        "score_label": "Good: 4 fixes to make",
        "checks": [
            {"label": "Parsed cleanly by ATS", "status": "pass"},
            {"label": "Contact details complete", "status": "pass"},
            {
                "label": "Achievements are measurable",
                "status": "warn",
                "note": "6 of 11 bullets have no numbers",
            },
            {
                "label": "Keywords for target role",
                "status": "warn",
                "note": "Missing: Docker, CI/CD, PostgreSQL",
            },
            {
                "label": "Consistent date formatting",
                "status": "fail",
                "note": "3 formats used across roles",
            },
        ],
    }


def story() -> dict:
    """The argument the page has to win before any feature matters.

    Placed above the feature sections on purpose: a reader who has not accepted
    the problem reads a capability list as noise.
    """
    return {
        "eyebrow": "Why this exists",
        "title": "Most applications are rejected before a person reads them.",
        "body": [
            "A recruiter opens your CV after software has already decided it is "
            "worth opening. That software does not read for potential. It reads "
            "for structure, dates, job titles and the exact words in the job "
            "description. Anything it cannot parse is discarded.",
            "Nobody teaches this. So capable people rewrite the same CV a dozen "
            "times, get nothing back, and conclude the problem is them.",
        ],
        "quote": "You were never told what the filter was looking for. That is the whole problem.",
        "link_label": "See what your CV scores",
    }


def journey() -> list[dict]:
    """The five-step arc the product is built around, in order.

    Every step links to the page that carries its detail — which is the point
    of the homepage: it routes, it does not explain. Anything that needs a
    paragraph to justify belongs on the page it links to.
    """
    return [
        {
            "step": "01",
            "icon": "upload",
            "title": "Upload the CV you already have",
            "description": "PDF or Word. We read it the way an applicant tracking system does.",
            "url": _link("resume:upload", "resume"),
        },
        {
            "step": "02",
            "icon": "chart",
            "title": "See what's holding it back",
            "description": "Parse result, skills found and weak lines, each scored and explained.",
            "url": _link("resume:analysis", "resume"),
        },
        {
            "step": "03",
            "icon": "document",
            "title": "Fix it, or rebuild it",
            "description": "Guided, full control, or describe it in your own words. Your wording, tightened.",
            "url": _link("resume:builder", "resume"),
        },
        {
            "step": "04",
            "icon": "target",
            "title": "See the roles you actually match",
            "description": "Openings scored against the skills we found, with the gaps named.",
            "url": _link("jobs:recommended", "job_matching"),
        },
        {
            "step": "05",
            "icon": "clipboard",
            "title": "Apply and track it",
            "description": "One pipeline for applications, replies and interviews.",
            "url": _link("applications:index", "applications"),
        },
    ]


def capabilities() -> list[dict]:
    return [
        {
            "icon": "chart",
            "title": "CV analysis",
            "description": (
                "An honest read of your CV: parse result, skills found and missing, "
                "link check, and the specific lines costing you interviews."
            ),
            "url": _link("resume:upload", "resume"),
            "link_label": "Analyse a CV",
        },
        {
            "icon": "document",
            "title": "CV builder",
            "description": (
                "Three ways in: describe your work and approve each drafted line, "
                "answer questions step by step, or take full control of the layout."
            ),
            "url": _link("resume:builder", "resume"),
            "link_label": "Open the builder",
        },
        {
            "icon": "layout",
            "title": "Templates",
            "description": (
                "Twelve layouts built around what a particular field actually puts on a "
                "CV, all structured to parse cleanly."
            ),
            "url": _link("resume:templates", "resume"),
            "link_label": "Browse templates",
        },
        {
            "icon": "target",
            "title": "Job matching",
            "description": (
                "Roles scored against your CV, with the gap shown up front so you know "
                "what to fix before applying rather than after the rejection."
            ),
            "url": _link("jobs:list", "jobs"),
            "link_label": "See matched jobs",
        },
    ]


def proof_points() -> list[dict]:
    """Product facts, not vanity metrics.

    No user counts, no "trusted by 10,000 candidates", no logo wall of companies
    that have never heard of us. Everything here is checkable inside the product
    on day one, which is the only kind of proof that survives contact with a
    sceptical reader.
    """
    return [
        {
            "value": "PDF · DOCX",
            "label": "Read the way ATS read",
            "note": "The same parse, not a preview of your file.",
        },
        {
            "value": "9 checks",
            "label": "Scored on every upload",
            "note": "Structure, keywords, achievements, formatting.",
        },
        {
            "value": "Line by line",
            "label": "Rewrites you approve",
            "note": "Your wording tightened, never replaced silently.",
        },
        {
            "value": "Yours to delete",
            "label": "Your CV, your data",
            "note": "Delete it and everything derived from it, any time.",
        },
    ]


def pricing_plans(user=None) -> list[dict]:
    """The plans, read from the `Plan` rows that actually govern the product.

    Until P18 this was a hand-written list with a docstring promising to swap
    it for a Plan model before launch. It was also wrong — it advertised twelve
    templates where six exist and eight free where four are, and it named
    limits nothing enforced. Both halves of that are the same defect as
    linking to an unbuilt feature (INV-6), so the page now describes the limit
    document itself: change a plan and the page changes with it, or it does not
    change at all.

    The call to action stays here, because a URL is a property of the site
    rather than of a commercial plan.
    """
    from apps.billing.selectors import plan_cards

    calls_to_action = {
        "free": {"label": "Create an account", "url": _link("accounts:signup")},
        "pro": {"label": "Start with Pro", "url": _link("accounts:signup")},
        "employer": {"label": "Contact sales", "url": _link("core:contact")},
    }
    fallback = {"label": "Create an account", "url": _link("accounts:signup")}

    # Signed in, the page is about *their* plan. "Create an account" sent a
    # candidate who already had one to the sign-up page, which bounced them to
    # their dashboard — the Pro button went nowhere near a plan. A candidate's
    # plan changes on their plan page, which says what moving costs and when
    # (decision D-08); an account with no candidate side is not offered one.
    current = ""
    signed_in = bool(user is not None and getattr(user, "is_authenticated", False))
    if signed_in:
        profile = getattr(user, "candidate_profile", None)
        if profile is not None:
            from apps.billing.services import entitlements

            current = entitlements.plan_for(profile).code
            plan_url = _link("dashboard:plan")
            calls_to_action = {
                "free": {"label": "Move to Free", "url": plan_url},
                "pro": {"label": "Upgrade to Pro", "url": plan_url},
                "employer": calls_to_action["employer"],
            }
            fallback = {"label": "See your plan", "url": plan_url}
        else:
            calls_to_action = {"employer": calls_to_action["employer"]}
            fallback = {"label": "", "url": ""}

    rows = []
    for card in plan_cards():
        is_current = bool(current) and card["code"] == current
        cta = calls_to_action.get(card["code"], fallback)
        if is_current:
            cta = {"label": "Your current plan", "url": _link("dashboard:plan")}
        rows.append(
            {
                **card,
                "cadence": "forever" if card["is_free"] else card["cadence"],
                "cta": cta,
                "is_current": is_current,
                "featured": card["code"] == "pro" and not current,
            }
        )
    return rows


def pricing_faq() -> list[dict]:
    return [
        {
            "question": "Do I need to pay to see my CV score?",
            "answer": "No. Your first analysis each month is free: the parse result, "
            "the score and the full list of problems. Pro unlocks unlimited "
            "analyses, every template, and downloading the finished CV.",
        },
        {
            "question": "What happens to my CV data?",
            "answer": "It stays in your account and is used only to power your own "
            "analysis, matching, and interviews. You can delete it, and "
            "everything derived from it, at any time.",
        },
        {
            "question": "How do I pay, and when does Pro start?",
            "answer": _payment_answer(),
        },
        {
            "question": "Can I cancel?",
            "answer": "Yes, from your plan page. Pro keeps working until the end of the "
            "period you paid for, and your CVs and history stay on the free plan.",
        },
        {
            "question": "Do employers pay per job post?",
            "answer": "Employer pricing is per company rather than per post, so hiring "
            "volume doesn't change the price. Get in touch for the current terms.",
        },
    ]


def audiences() -> list[dict]:
    return [
        {
            "icon": "user",
            "title": "For candidates",
            "description": "Fix the CV, find the role, rehearse the interview, track the pipeline.",
            "url": _link("accounts:signup"),
            "link_label": "Create an account",
        },
        {
            "icon": "building",
            "title": "For employers",
            "description": "Post a role and read applicants who arrive already scored against "
            "the skills you listed.",
            "url": _link("core:employer_solutions") or _link("core:contact"),
            "link_label": "See how hiring works",
        },
    ]


def _payment_answer() -> str:
    """Price and payment terms, read from the plan (ADR-A59)."""
    from apps.billing.models import Plan
    from apps.billing.money import format_money

    pro = Plan.objects.filter(code="pro", is_active=True).first()
    price = f"Pro is {format_money(pro.price, pro.currency)} a {pro.interval}. " if pro else ""
    return (
        f"{price}Choose Pro on your plan page and pay its invoice with eSewa or Khalti, "
        "or by bank transfer. Pro starts as soon as the payment is confirmed, and you stay "
        "on Free until then."
    )
