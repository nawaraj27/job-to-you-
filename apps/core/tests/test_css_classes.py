"""
Every class a template names is one the built stylesheet defines.

Found while checking the owner-decision screens (2026-09-18): the caution box
on four of them used `alert-caution`, which the design system never defined —
only `alert-warning` exists — so the "you cannot review this company" and
"this invoice is overdue" notices rendered as plain text. The same check found
error messages styled with an undefined `text-content-critical` across nine
templates, and a stylesheet built from stale sources that lacked `break-words`.

Static `class="..."` attributes only: Alpine bindings (`:class`) and template
expressions are skipped, and a class that exists purely as a structural hook
is listed below with its reason.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

#: Classes with no style on purpose.
HOOKS = {
    # Structural wrapper in the CV paper layout; `.sheet-entry` styles the block.
    "sheet-entry-head",
}

_CLASS_ATTR = re.compile(r'(?<![:\w-])class="([^"{}]*)"')
#: A class selector. Its first character may itself be escaped: container
#: query variants are `.\@container` and `.\@3xl\:grid-cols-…`, and a class
#: starting with a digit is written with a hex escape (`2xl:` is `.\32 xl\:`).
#: The pattern used to skip both, reporting those classes as undefined.
_DEFINED = re.compile(r"\.((?:[a-zA-Z_-]|\\[0-9a-fA-F]{1,6} ?|\\.)(?:[\w-]|\\.)*)")
_HEX_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6}) ?")


class TemplateClassTests(SimpleTestCase):
    def test_every_static_template_class_is_defined_in_the_stylesheet(self):
        base = Path(settings.BASE_DIR)
        css = (base / "static" / "css" / "dist" / "site.css").read_text(encoding="utf-8")
        defined = {
            _HEX_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), name).replace("\\", "")
            for name in _DEFINED.findall(css)
        }

        missing: dict[str, list[str]] = {}
        for template in sorted((base / "templates").rglob("*.html")):
            for attribute in _CLASS_ATTR.findall(template.read_text(encoding="utf-8")):
                for name in attribute.split():
                    if name in HOOKS or name.startswith("["):
                        continue
                    if name not in defined:
                        missing.setdefault(name, []).append(template.name)

        self.assertEqual(
            missing, {}, "classes the stylesheet does not define (run `npm run css:build`?)"
        )


class UnwantedDecorationTests(SimpleTestCase):
    """No text in the product carries a decoration nobody asked for.

    Found in manual testing (2026-09-21): a line drawn *above* "Salary",
    "Location", every metadata `dt` and status eyebrow. `.overline` is the
    design system's eyebrow label, and it is also the name of Tailwind's
    `text-decoration-line: overline` utility; because the word appears in
    templates, Tailwind generated the utility as well and both applied. The
    stylesheet now excludes that one utility (`@source not inline`).
    """

    def setUp(self):
        base = Path(settings.BASE_DIR)
        self.css = (base / "static" / "css" / "dist" / "site.css").read_text(encoding="utf-8")
        self.templates = sorted((base / "templates").rglob("*.html"))

    def test_the_overline_utility_is_not_generated(self):
        self.assertNotIn("text-decoration-line:overline", self.css.replace(" ", ""))

    def test_the_eyebrow_label_still_exists(self):
        self.assertRegex(self.css, r"\.overline\{[^}]*text-transform:uppercase")

    def test_no_template_decorates_text_with_dashes_or_dots(self):
        offenders = [
            str(template)
            for template in self.templates
            if re.search(
                r"\b(decoration-(dashed|dotted)|underline decoration-)",
                template.read_text(encoding="utf-8"),
            )
        ]
        self.assertEqual(offenders, [])

    def test_focus_indicators_are_untouched(self):
        # The fix is one excluded utility, not a reset: focus rings stay.
        self.assertIn("focus-visible", self.css)


class StatusContrastTests(SimpleTestCase):
    """Every status badge clears 4.5:1 on its own tint, in both themes (A11Y-01).

    `.badge-caution` set `warning-fg` on `warning-surface` at 12 px and
    measured 4.44:1 in light mode: `caution-700` cleared 4.5:1 on white but
    not on the amber tint the badge sits on, which is the pairing that counts.
    The design system promises ≥ 4.5:1 for all text, so the 700 step was
    darkened. Computed from the source tokens rather than from a screenshot,
    so the next token edit that breaks it fails here.
    """

    BADGES = ("success", "warning", "danger", "info")

    def setUp(self):
        base = Path(settings.BASE_DIR)
        self.source = (base / "static" / "css" / "src" / "input.css").read_text(encoding="utf-8")

    def test_every_status_foreground_clears_aa_on_its_surface_and_the_page(self):
        for theme, block_start, page in (
            ("light", "\n:root {", "--ui-surface"),
            ("dark", "\n.dark {", "--ui-surface"),
        ):
            tokens = self._tokens(block_start)
            background = self._rgb(tokens[page], tokens, over=None)
            for name in self.BADGES:
                with self.subTest(theme=theme, badge=name):
                    fg = self._rgb(tokens[f"--ui-{name}-fg"], tokens, over=None)
                    tint = self._rgb(tokens[f"--ui-{name}-surface"], tokens, over=background)
                    self.assertGreaterEqual(round(_ratio(fg, tint), 2), 4.5)
                    self.assertGreaterEqual(round(_ratio(fg, background), 2), 4.5)

    # --- token resolution ---------------------------------------------------

    def _tokens(self, block_start: str) -> dict[str, str]:
        tokens: dict[str, str] = {}
        for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", self.source):
            tokens.setdefault(name, value.strip())
        start = self.source.index(block_start)
        block = self.source[start : self.source.index("\n}", start)]
        for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", block):
            tokens[name] = value.strip()
        return tokens

    def _rgb(self, value: str, tokens: dict[str, str], *, over):
        """A token as linear sRGB, composited over `over` when translucent."""
        value = value.strip()
        reference = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if reference:
            if reference.group(1) == "--color-white":  # Tailwind's own, not declared here
                return (1.0, 1.0, 1.0)
            return self._rgb(tokens[reference.group(1)], tokens, over=over)
        mix = re.fullmatch(r"color-mix\(in oklab, (.+) (\d+)%, transparent\)", value)
        if mix:
            colour = self._rgb(mix.group(1), tokens, over=None)
            return _over(colour, float(mix.group(2)) / 100, over)
        oklch = re.fullmatch(r"oklch\(([\d.]+) ([\d.]+) ([\d.]+)(?: / ([\d.]+))?\)", value)
        if oklch:
            colour = _oklch(*(float(part) for part in oklch.groups()[:3]))
            alpha = float(oklch.group(4)) if oklch.group(4) else 1.0
            return _over(colour, alpha, over) if alpha < 1 else colour
        if value in ("#fff", "#ffffff", "white"):
            return (1.0, 1.0, 1.0)
        self.fail(f"cannot resolve {value!r}")


def _oklch(lightness: float, chroma: float, hue: float):
    import math

    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    return tuple(min(1.0, max(0.0, channel)) for channel in rgb)


def _encode(linear):
    return tuple(12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055 for c in linear)


def _decode(encoded):
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in encoded)


def _over(colour, alpha: float, background):
    """Alpha-composite in gamma-encoded sRGB, as the browser paints it."""
    top, bottom = _encode(colour), _encode(background or (1.0, 1.0, 1.0))
    return _decode(tuple(t * alpha + b * (1 - alpha) for t, b in zip(top, bottom, strict=True)))


def _ratio(first, second) -> float:
    def luminance(colour):
        return 0.2126 * colour[0] + 0.7152 * colour[1] + 0.0722 * colour[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)
