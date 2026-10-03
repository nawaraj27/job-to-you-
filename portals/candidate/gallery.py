"""
Template gallery browsing state: favourites, recently viewed, comparison.

Session-backed, deliberately. These are not domain facts — the SRS defines no
table for them, and none is warranted: a favourite is a browsing convenience
that costs nothing if it is lost, and storing it per account would make every
template view a write.

Everything the *product* remembers about templates lives in `ResumeDocument`
(which template it uses) and `ResumeTemplate` (what templates exist). This
module is view state, which is why it is in the portal rather than the domain.
"""

from __future__ import annotations

from typing import Any

from apps.resumes.models import TemplateLayout

FAVOURITES_KEY = "resume:template_favourites"
RECENT_KEY = "resume:template_recent"
COMPARE_KEY = "resume:template_compare"

#: Two side by side is a comparison; four is a gallery with extra steps.
COMPARE_LIMIT = 3
RECENT_LIMIT = 4


def favourites(request) -> list[str]:
    return list(request.session.get(FAVOURITES_KEY, []))


def is_favourite(request, slug: str) -> bool:
    return slug in favourites(request)


def toggle_favourite(request, slug: str) -> bool:
    """Returns whether it is now a favourite."""
    saved = favourites(request)
    if slug in saved:
        saved.remove(slug)
        on = False
    else:
        saved.append(slug)
        on = True

    request.session[FAVOURITES_KEY] = saved
    request.session.modified = True
    return on


def record_view(request, slug: str) -> None:
    recent = [item for item in request.session.get(RECENT_KEY, []) if item != slug]
    request.session[RECENT_KEY] = [slug, *recent][:RECENT_LIMIT]
    request.session.modified = True


def compare_set(request) -> list[str]:
    return list(request.session.get(COMPARE_KEY, []))


def toggle_compare(request, slug: str) -> tuple[bool, str | None]:
    """Returns (is now in the comparison, error)."""
    chosen = compare_set(request)

    if slug in chosen:
        chosen.remove(slug)
        added = False
        error = None
    elif len(chosen) >= COMPARE_LIMIT:
        return False, f"Compare up to {COMPARE_LIMIT} templates at once."
    else:
        chosen.append(slug)
        added, error = True, None

    request.session[COMPARE_KEY] = chosen
    request.session.modified = True
    return added, error


def clear_compare(request) -> None:
    request.session[COMPARE_KEY] = []
    request.session.modified = True


def context(request, templates, *, locked: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The gallery, filtered and searched, with this browser's state joined on.

    Every card draws the same worked example (`presentation.previews`), never
    the candidate's own draft: a template preview that changed whenever the
    candidate edited their CV was not a preview of the template. `locked` is
    the Pro templates this candidate's plan does not include, so the card can
    say so before they open it.
    """
    query = (request.GET.get("q") or "").strip()
    layout = request.GET.get("layout") or ""
    tier = request.GET.get("tier") or ""

    rows = templates
    if layout:
        rows = rows.filter(layout=layout)
    if tier:
        rows = rows.filter(tier=tier)
    if query:
        from django.db.models import Q

        rows = rows.filter(
            Q(name__icontains=query) | Q(suits__icontains=query) | Q(notes__icontains=query)
        )

    saved = favourites(request)
    chosen = compare_set(request)

    from apps.resumes import presentation

    return {
        "templates": rows,
        "results": [
            {
                "template": preview["template"],
                "paper": preview["paper"],
                "paper_classes": preview["paper_classes"],
                "paper_style": preview["paper_style"],
                "is_favourite": preview["template"].slug in saved,
                "in_compare": preview["template"].slug in chosen,
                "is_locked": preview["template"].slug in locked,
            }
            for preview in presentation.previews(rows)
        ],
        # Offered from the model's own choices so a new structure appears in
        # the filter the moment it exists, rather than the day someone
        # remembers to edit the template.
        "layout_options": TemplateLayout.choices,
        "query": query,
        "layout": layout,
        "tier": tier,
        "favourites": saved,
        "compare": chosen,
        "recent": request.session.get(RECENT_KEY, []),
    }


def compare_context(request) -> dict[str, Any]:
    from apps.resumes import selectors

    chosen = compare_set(request)
    rows = [
        template for slug in chosen if (template := selectors.template_by_slug(slug)) is not None
    ]

    from apps.resumes import presentation

    return {
        "templates": rows,
        "results": presentation.previews(rows),
        "compare": chosen,
        "limit": COMPARE_LIMIT,
    }
