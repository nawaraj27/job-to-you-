"""
Page adapter for templates.

`components/pagination.html` renders a flat page record — number, total_pages,
count, start, end, numbers — rather than a Django `Page`. Kept that way because
the template is shared by every paginated list in the product, and a template
that reaches through `page.paginator.num_pages` is a template that has to be
edited when the source of a list changes.

The window of page numbers is bounded: a search with four hundred pages must
not render four hundred links.
"""

from __future__ import annotations

from typing import Any

from django.core.paginator import Page, Paginator

#: How many numbered links to show around the current page.
WINDOW = 2


def page_of(queryset: Any, *, number: Any, per_page: int) -> dict[str, Any]:
    """Paginate and flatten in one step."""
    return as_page(Paginator(queryset, per_page).get_page(number))


def as_page(page: Page) -> dict[str, Any]:
    paginator = page.paginator
    total = paginator.num_pages
    current = page.number

    first = max(1, current - WINDOW)
    last = min(total, current + WINDOW)

    return {
        "items": page.object_list,
        "number": current,
        "total_pages": total,
        "count": paginator.count,
        "per_page": paginator.per_page,
        # 1-based and inclusive, because "showing 1–20 of 340" is what the
        # reader is being told, not a slice.
        "start": page.start_index(),
        "end": page.end_index(),
        "numbers": list(range(first, last + 1)),
        "previous": current - 1 if page.has_previous() else None,
        "next": current + 1 if page.has_next() else None,
    }
