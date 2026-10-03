"""
What a template *looks* like, decided once for all three surfaces.

The builder's live preview is HTML, the export is a PDF, and the Word file is a
DOCX. Before this module each of the three decided its own type sizes, its own
rules and its own idea of which sections belong in a sidebar — which is how a
candidate ends up choosing a template on screen and downloading a different
document. The preview is the promise; the export has to keep it.

So a template resolves to exactly one `Theme`, and every surface reads it:

  * `pdf.py` turns it into ReportLab styles and frames.
  * `docx.py` turns it into run formatting.
  * `presentation.py` turns it into CSS custom properties on the preview's
    root element, so the browser draws the same document with the same numbers.

Sizes are in points throughout, including for the web: the preview is a picture
of a sheet of A4, and a sheet of A4 is measured in points. The CSS scales the
whole sheet at once rather than restating any size in pixels.

**Structure, not decoration.** A template that differs from another only by
accent colour is a colour picker wearing a template's name. Each theme here
changes at least one of: where the sidebar is, whether the header is centred,
whether headings carry a rule, how dense the page is, or whether the summary
gets a full-width band ahead of the columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: How the page is divided. Fewer structures than templates on purpose — the
#: structures are what the renderers must actually implement, and every one of
#: them is a fork in the layout code.
Structure = Literal["single", "sidebar-left", "sidebar-right", "banded"]

#: Sections that go in a sidebar when the structure has one. Chosen because
#: they are scanned rather than read — a sidebar full of dated entries is a
#: column nobody can follow.
DEFAULT_SIDEBAR_KEYS = frozenset({"skills", "languages", "certifications", "interests"})

#: A technical CV is read for its stack first, so the sidebar carries tools and
#: certifications but leaves interests in the body where they cost nothing.
TECHNICAL_SIDEBAR_KEYS = frozenset({"skills", "languages", "certifications"})

#: Accent names are staff-editable strings on the template row, so they are
#: mapped through a closed table rather than reaching a colour parser.
ACCENTS: dict[str, str] = {
    "ink": "#1c1a17",
    "slate": "#334155",
    "clay": "#9a3412",
    "indigo": "#3730a3",
    "graphite": "#111827",
    "teal": "#0f766e",
    "plum": "#6b21a8",
    "sand": "#78350f",
}

DEFAULT_ACCENT = "#1c1a17"


@dataclass(frozen=True)
class Theme:
    """One template's layout decisions, in the units the page is drawn in."""

    #: The structural family. Everything else is styling within it.
    structure: Structure = "single"

    #: Which section keys leave the body for the sidebar. Empty for `single`
    #: and `banded`, which have nowhere to put them.
    sidebar_keys: frozenset[str] = field(default_factory=frozenset)

    #: Sidebar measure, in millimetres. Narrow enough that the body column
    #: keeps a readable line length on A4.
    sidebar_width_mm: float = 58.0

    #: Whether the sidebar sits on a tinted panel. A tint is the one piece of
    #: decoration that survives being printed in greyscale and still reads as
    #: structure rather than as a smudge.
    sidebar_tinted: bool = False

    header_align: Literal["left", "center"] = "left"

    #: A rule under the name block. Executive and academic CVs use one; a
    #: minimal template must not.
    rule_under_header: bool = False

    #: A rule under every section heading, full body width.
    rule_under_heading: bool = False

    heading_case: Literal["upper", "title"] = "upper"
    heading_tracking: float = 0.08  # em, applied to upper-case headings only

    #: Whether headings take the accent colour. A template that wants to read
    #: as plain sets this false and the accent shows only in the headline.
    accent_headings: bool = True

    #: Whether the name itself takes the accent. Creative templates do; a CV
    #: for a government form does not.
    accent_name: bool = False

    name_size: float = 19.0
    headline_size: float = 11.0
    contact_size: float = 8.5
    heading_size: float = 10.5
    body_size: float = 9.5

    #: Multiplies every vertical gap. Compact templates fit ten years on two
    #: pages; the airy ones give a short CV room to breathe rather than
    #: stranding it in the top third of page one.
    density: float = 1.0

    #: Line height as a multiple of body size.
    line_height: float = 1.37

    #: Page margin in millimetres.
    margin_mm: float = 18.0

    #: What separates the contact details on one line.
    contact_separator: str = " · "

    #: Bullet glyph preference. Overridden to an en dash when no real font is
    #: embedded, because U+2022 in a base-14 face does not survive extraction.
    bullet: str = "•"

    #: A full-width summary band ahead of the columns. Only meaningful for
    #: `banded`, where it is the whole point of the structure.
    band_key: str = "summary"

    #: Resolved from the template's `accent` name.
    accent: str = DEFAULT_ACCENT

    #: Resolved from the template's `font` name: sans, serif or mono.
    family: str = "sans"

    @property
    def has_sidebar(self) -> bool:
        return self.structure in ("sidebar-left", "sidebar-right")

    @property
    def leading(self) -> float:
        """Body leading in points."""
        return round(self.body_size * self.line_height, 2)

    def gap(self, points: float) -> float:
        """A vertical gap, scaled by the theme's density."""
        return round(points * self.density, 2)


#: The catalogue, keyed by the `layout` value on a template row.
#:
#: `layout` is a small closed vocabulary rather than a pile of per-template
#: columns: staff pick a structure and an accent, and the structure is the part
#: that costs renderer code. Adding a tenth accent is a data edit; adding a
#: tenth structure is a decision.
_THEMES: dict[str, Theme] = {
    # One column, generous margins, nothing an applicant tracking system can
    # trip over. The safe default and the one most candidates should use.
    "classic": Theme(
        structure="single",
        rule_under_heading=True,
        heading_case="upper",
        density=1.0,
    ),
    # The same structure, wound tighter. For a history that has to fit two
    # pages rather than sprawl over three.
    "compact": Theme(
        structure="single",
        rule_under_heading=True,
        heading_case="upper",
        name_size=17.0,
        heading_size=9.8,
        body_size=9.0,
        line_height=1.28,
        density=0.72,
        margin_mm=15.0,
    ),
    # Skills, tools and languages in a tinted left column; history gets the
    # wide one. The layout technical candidates ask for by name.
    "sidebar": Theme(
        structure="sidebar-left",
        sidebar_keys=DEFAULT_SIDEBAR_KEYS,
        sidebar_tinted=True,
        sidebar_width_mm=58.0,
        rule_under_heading=False,
        heading_case="upper",
        density=0.95,
    ),
    # A full-width summary band, then the body. Reads as a profile rather than
    # a list, which is what a senior CV is arguing for.
    "modern": Theme(
        structure="banded",
        header_align="left",
        rule_under_header=True,
        rule_under_heading=False,
        heading_case="upper",
        heading_tracking=0.12,
        name_size=22.0,
        headline_size=11.5,
        density=1.1,
        margin_mm=20.0,
    ),
    # Centred name, rules above and below, wide margins, serif. The shape a
    # board-level CV has had for forty years, and the one that reads as
    # seniority rather than as a template.
    "executive": Theme(
        structure="single",
        header_align="center",
        rule_under_header=True,
        rule_under_heading=True,
        heading_case="upper",
        heading_tracking=0.14,
        accent_headings=False,
        name_size=21.0,
        headline_size=10.5,
        heading_size=10.0,
        density=1.15,
        margin_mm=22.0,
        contact_separator="   |   ",
    ),
    # No rules, no colour on the headings, nothing but type and space. For
    # employers who ask for a plain CV, and for candidates who want one.
    "minimal": Theme(
        structure="single",
        rule_under_header=False,
        rule_under_heading=False,
        heading_case="title",
        heading_tracking=0.0,
        accent_headings=False,
        name_size=18.0,
        heading_size=10.5,
        density=1.2,
        margin_mm=22.0,
    ),
    # Dense, sidebar on the right so the reading eye hits the roles first, and
    # the stack sits where it can be scanned without breaking that read.
    "technical": Theme(
        structure="sidebar-right",
        sidebar_keys=TECHNICAL_SIDEBAR_KEYS,
        sidebar_tinted=False,
        sidebar_width_mm=52.0,
        rule_under_heading=True,
        heading_case="upper",
        name_size=17.5,
        body_size=9.0,
        heading_size=9.8,
        line_height=1.3,
        density=0.8,
        margin_mm=16.0,
    ),
    # Single column, serif, no colour, no tint: a CV that will be read beside a
    # publication list and must not look like it came out of a design tool.
    "academic": Theme(
        structure="single",
        header_align="center",
        rule_under_header=True,
        rule_under_heading=False,
        heading_case="upper",
        heading_tracking=0.1,
        accent_headings=False,
        name_size=18.0,
        headline_size=10.0,
        body_size=9.5,
        line_height=1.45,
        density=1.1,
        margin_mm=20.0,
        contact_separator="  ·  ",
    ),
    # A tinted column on the *right*, title-case headings, and room between
    # the lines. For a CV that is itself a work sample: the history reads first
    # because the eye starts left, and the panel of tools and languages sits
    # where it can be looked at afterwards.
    #
    # This exists because the template it belongs to was a paid one that
    # differed from a free template by its accent colour and nothing else. A
    # colour is not worth money, and a catalogue that charges for one teaches
    # candidates not to trust the rest of it.
    "portfolio": Theme(
        structure="sidebar-right",
        sidebar_keys=DEFAULT_SIDEBAR_KEYS,
        sidebar_tinted=True,
        sidebar_width_mm=64.0,
        rule_under_header=True,
        rule_under_heading=False,
        heading_case="title",
        heading_tracking=0.0,
        name_size=20.0,
        headline_size=11.0,
        heading_size=11.0,
        line_height=1.46,
        density=1.08,
        margin_mm=17.0,
    ),
    # The only template that spends its accent on the name. Still one column of
    # real text in reading order — a CV that a parser cannot read is not a
    # creative CV, it is a lost application.
    "creative": Theme(
        structure="sidebar-left",
        sidebar_keys=DEFAULT_SIDEBAR_KEYS,
        sidebar_tinted=True,
        sidebar_width_mm=62.0,
        rule_under_header=False,
        rule_under_heading=False,
        heading_case="upper",
        heading_tracking=0.16,
        accent_name=True,
        name_size=23.0,
        headline_size=10.5,
        density=1.05,
        margin_mm=16.0,
    ),
}

#: Retired or unknown `layout` values render as the safe default rather than
#: failing: a template row edited to a typo must still produce a CV.
FALLBACK = "classic"


def for_template(template: dict | None) -> Theme:
    """Resolve one template snapshot to its theme.

    Takes the snapshot dict rather than the model, for the same reason the
    renderers do: a theme that could load a row is a theme that will eventually
    be handed a draft's.
    """
    template = template or {}
    base = _THEMES.get((template.get("layout") or "").strip().lower(), _THEMES[FALLBACK])

    accent = ACCENTS.get((template.get("accent") or "").strip().lower(), DEFAULT_ACCENT)
    family = _family(template)

    density = base.density
    if str(template.get("density") or "").strip().lower() == "compact":
        # A density flag on the row rather than a separate structure: "the same
        # template, tighter" is a real request and does not deserve its own
        # layout fork.
        density = round(density * 0.78, 3)

    return _replace(base, accent=accent, family=family, density=density)


def accent_rgb(theme: Theme) -> tuple[int, int, int]:
    """The accent as three 0-255 channels, for renderers that want bytes."""
    value = theme.accent.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def layout_choices() -> tuple[str, ...]:
    """Every structure a template row may name."""
    return tuple(_THEMES)


def _family(template: dict) -> str:
    from .fonts import family_for

    return family_for(template)


def _replace(theme: Theme, **changes) -> Theme:
    from dataclasses import replace

    return replace(theme, **changes)
