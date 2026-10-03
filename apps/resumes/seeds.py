"""
The template catalogue.

Data rather than migration content: templates are edited by staff, and a
migration that inserts rows makes every later edit look like drift. The seed is
idempotent, so a new environment gets the catalogue and an existing one keeps
its edits to names and notes while structure stays correct.

Ten templates across ten page structures. Every one of them changes the shape
of the page — where the sidebar sits, whether the header is centred, whether
headings carry a rule, how dense the setting is — because a template that
differs only by accent colour is a colour picker wearing a template's name.
The structures are implemented once in `rendering/theme.py` and read by the
PDF renderer, the Word renderer and the browser preview alike.

The `suits` line is the part candidates actually read — a gallery of ten names
with no guidance is ten coin flips.
"""

from __future__ import annotations

#: (slug, name, family, layout, accent, font, density, tier, suits, notes)
TEMPLATES: list[tuple[str, str, str, str, str, str, str, str, str, str]] = [
    (
        "meridian",
        "Meridian",
        "Meridian",
        "classic",
        "ink",
        "Source Serif",
        "normal",
        "free",
        "Most roles, most employers. Start here if you are not sure.",
        "One column, ruled section headings, generous margins. Nothing an "
        "applicant tracking system can trip over, and the layout an employer "
        "expects before they have read a word.",
    ),
    (
        "meridian-compact",
        "Meridian Compact",
        "Meridian",
        "compact",
        "ink",
        "Source Serif",
        "normal",
        "free",
        "Ten years of history that has to fit on two pages.",
        "The same structure as Meridian with tighter leading, smaller section "
        "gaps and narrower margins. Roughly a third more on the page.",
    ),
    (
        "atlas",
        "Atlas",
        "Atlas",
        "sidebar",
        "slate",
        "Inter",
        "normal",
        "free",
        "Technical roles where the skills list is the first thing anyone reads.",
        "Skills, languages and certifications sit in a tinted left column; the "
        "history gets the wide one. The name and contact line still run across "
        "the top, so a parser reads them before anything else.",
    ),
    (
        "atlas-warm",
        "Atlas Warm",
        "Atlas",
        "portfolio",
        "clay",
        "Inter",
        "normal",
        "pro",
        "Design, marketing and anything where the CV is itself a work sample.",
        "The column moves to the right, so a reader meets the roles before the "
        "toolkit. Title-case headings and a wider measure between the lines: "
        "quieter than Atlas, and the one template that looks composed rather "
        "than filed.",
    ),
    (
        "vertex",
        "Vertex",
        "Vertex",
        "modern",
        "indigo",
        "Inter",
        "normal",
        "pro",
        "Senior roles where the summary carries the argument.",
        "A full-width summary band, ruled top and bottom, then the history "
        "below it. Reads as a profile rather than as a list.",
    ),
    (
        "regent",
        "Regent",
        "Regent",
        "executive",
        "graphite",
        "Source Serif",
        "normal",
        "pro",
        "Director and board-level applications, and any formal submission.",
        "Centred name between two rules, wide margins, serif throughout, no "
        "colour on the headings. The shape a senior CV has had for forty "
        "years, which is exactly why it reads as seniority.",
    ),
    (
        "circuit",
        "Circuit",
        "Circuit",
        "technical",
        "teal",
        "Inter",
        "normal",
        "free",
        "Engineering CVs with a long stack and a longer history.",
        "Dense setting with the stack in a right-hand column, so the reading "
        "eye meets the roles first and the tools stay scannable beside them.",
    ),
    (
        "plainspoke",
        "Plainspoke",
        "Plainspoke",
        "minimal",
        "graphite",
        "Inter",
        "normal",
        "free",
        "Government, academic and any employer that asks for a plain CV.",
        "No rules, no colour, no columns, headings in sentence case. "
        "Deliberately unremarkable, and the safest thing to send when the "
        "advert does not say what it wants.",
    ),
    (
        "lyceum",
        "Lyceum",
        "Lyceum",
        "academic",
        "ink",
        "Source Serif",
        "normal",
        "free",
        "Research posts, teaching applications and funding submissions.",
        "Centred header, serif, looser leading and no accent colour, so a "
        "publication list beside it reads as part of the same document.",
    ),
    (
        "prism",
        "Prism",
        "Prism",
        "creative",
        "plum",
        "Inter",
        "normal",
        "pro",
        "Portfolio-led roles where the CV is read next to the work.",
        "The only template that spends its accent on the name, over a tinted "
        "column. Still one flow of real text in reading order — a CV a parser "
        "cannot read is not a creative CV, it is a lost application.",
    ),
]
