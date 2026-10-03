"""
ATS checks (FR-4.2). Deterministic, and deliberately so.

An applicant tracking system is a parser. Whether a parser can read a document
is a fact about the document, and asking a language model to guess at it returns
a fluent answer with no relationship to what the parser actually does. So every
check here is arithmetic on the content, and none of them calls anything.

Each check returns `(status, note)` where the note is what a person should do
about it. A `warn` with no note is a check nobody can act on.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from ..parsing import extract
from . import text as reader

#: Length bands, in words. A one-page CV is roughly 400-500 words; the upper
#: bound is where a two-page CV stops being two pages.
SHORT_CV = 180
LONG_CV = 1_100

#: Below this share of bullets carrying a number, the CV reads as duties.
MEASURED_BULLET_TARGET = 0.35

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.IGNORECASE)
PHONE = re.compile(r"(\+?\d[\d\s\-().]{6,}\d)")


@dataclass
class Check:
    key: str
    label: str
    status: str
    note: str = ""
    order: int = 0


PASS, WARN, FAIL = "pass", "warn", "fail"


def ats_checks(
    content: dict[str, Any],
    *,
    parse_warnings: list[str] | None = None,
    filename: str = "",
    today: dt.date | None = None,
) -> list[Check]:
    """Every machine-readability check, in the order the report shows them.

    `parse_warnings` and `filename` come from the upload a document was built
    from, when there was one. A CV written in the builder has neither, and the
    checks that need them report `pass` rather than inventing a problem: the
    builder's own output is structured, which is the thing those checks are
    worried about.
    """
    today = today or dt.date.today()
    warnings = parse_warnings or []

    checks = [
        _extraction(warnings),
        _contact(content),
        _headings(content),
        _dates(content, today=today),
        _length(content),
        _outcomes(content),
        _bullets(content),
        _filename(filename),
    ]
    for order, check in enumerate(checks):
        check.order = order
    return checks


def _extraction(warnings: list[str]) -> Check:
    """Whether text came out of the file at all.

    The check that matters most and is cheapest to describe: a CV whose text
    cannot be extracted scores zero everywhere in a real ATS, however good it
    looks on screen.

    Only readability warnings count. The parser also warns about sections it
    did not find, and those were reaching this check: a perfectly legible CV
    with no skills heading was told "parts of the original file were hard to
    read", lost machine-readability points for it, and was then charged again
    under skills coverage for the same missing heading. The report says the
    honest thing about a missing section elsewhere, once.
    """
    # No text at all is not a degree of "hard to read" — it is the whole
    # finding, and the only one on the report that matters until it is fixed.
    # A warning is the wrong severity for a file no employer's software can
    # open, and burying it in a semicolon-separated list is the wrong shape.
    if extract.NO_TEXT_WARNING in warnings:
        return Check(
            key="extraction",
            label="Text extraction",
            status=FAIL,
            note=(
                "This file is a picture of a CV, not a document. No text can be "
                "read out of it, so an applicant tracking system sees an empty "
                "application. Rebuild it in the CV builder, or export the original "
                "from Word as a PDF rather than scanning it."
            ),
        )

    unreadable = [warning for warning in warnings if warning not in extract.STRUCTURE_WARNINGS]
    if not unreadable:
        return Check(
            key="extraction",
            label="Text extraction",
            status=PASS,
            note="Every part of this CV can be read as text.",
        )
    return Check(
        key="extraction",
        label="Text extraction",
        status=WARN,
        note=(
            "Parts of the original file were hard to read: "
            + "; ".join(warning.rstrip(".") for warning in unreadable[:3])
            + ". Exporting from this builder produces a file that always extracts cleanly."
        ),
    )


def _contact(content: dict[str, Any]) -> Check:
    contact = content.get("contact", {}) or {}
    blob = " ".join(str(value) for value in contact.values())

    missing = []
    if not EMAIL.search(blob):
        missing.append("an email address")
    if not PHONE.search(blob):
        missing.append("a phone number")
    if not str(contact.get("name", "")).strip():
        missing.append("your name")

    if not missing:
        return Check(
            key="contact",
            label="Contact details",
            status=PASS,
            note="Name, email and phone are all where a parser looks for them.",
        )
    return Check(
        key="contact",
        label="Contact details",
        status=FAIL if "an email address" in missing else WARN,
        note=(
            "A recruiter's system could not find " + ", ".join(missing) + ". "
            "Without a way to reach you, everything else on the page is wasted."
        ),
    )


def _headings(content: dict[str, Any]) -> Check:
    """Whether the sections use names a parser recognises.

    Not a style opinion: an ATS maps headings to fields by name, and "What I
    have been up to" maps to nothing.
    """
    known = {
        "summary",
        "experience",
        "education",
        "skills",
        "projects",
        "certifications",
        "languages",
        "awards",
        "publications",
        "volunteering",
        "interests",
        "references",
    }
    visible = [
        section
        for section in content.get("sections", [])
        if section.get("visible", True) and _has_body(section)
    ]
    unknown = [
        str(section.get("title", "")).strip()
        for section in visible
        if str(section.get("key", "")).lower() not in known
        and str(section.get("title", "")).strip().lower() not in known
    ]

    if not unknown:
        return Check(
            key="headings",
            label="Section headings",
            status=PASS,
            note="Every heading is one an applicant tracking system recognises.",
        )
    return Check(
        key="headings",
        label="Section headings",
        status=WARN,
        note=(
            "These headings will not map to a field: "
            + ", ".join(f"“{title}”" for title in unknown[:3])
            + ". A standard name above the same content costs you nothing."
        ),
    )


def _dates(content: dict[str, Any], *, today: dt.date) -> Check:
    """Whether the date ranges on this CV are ones a parser can read.

    Every dated section, not only employment. An applicant tracking system
    reads the dates on a degree and on a project exactly as it reads the ones
    on a job, and looking only at experience meant a student CV — which has
    dated education and no employment at all — was warned that its dates could
    not be checked, and charged machine-readability points for it. They were
    losing marks for never having had a job, in the one part of the report that
    is supposed to be purely about the file.
    """
    dated = [entry for entry in reader.entries(content) if entry.dates]
    if not dated:
        # Nothing dated anywhere. There is no problem with this document's
        # dates, because it has none; inventing a warning here is how a check
        # that does not apply becomes a penalty.
        return Check(
            key="dates",
            label="Date formats",
            status=PASS,
            note="There are no date ranges on this CV to check.",
        )

    unreadable = [entry for entry in dated if reader.parse_period(entry.dates, today=today) is None]
    if not unreadable:
        return Check(
            key="dates",
            label="Date formats",
            status=PASS,
            note="Every date range on this CV is one a parser can read.",
        )
    return Check(
        key="dates",
        label="Date formats",
        status=WARN,
        note=(
            f"{len(unreadable)} of {len(dated)} dated entries have dates that will not "
            "parse. "
            "Use a form like “Mar 2021 – Present”, consistently."
        ),
    )


def _length(content: dict[str, Any]) -> Check:
    words = reader.word_count(content)
    if words < SHORT_CV:
        return Check(
            key="length",
            label="Length",
            status=WARN,
            note=(
                f"About {words} words. That is thin enough that a recruiter will assume "
                "there is more you did not say."
            ),
        )
    if words > LONG_CV:
        return Check(
            key="length",
            label="Length",
            status=WARN,
            note=(
                f"About {words} words, which is over two pages. The last page of a long CV is "
                "read least, so anything that matters should not be on it."
            ),
        )
    return Check(
        key="length", label="Length", status=PASS, note=f"About {words} words. A sensible length."
    )


def _outcomes(content: dict[str, Any]) -> Check:
    bullets = reader.all_bullets(content)
    if not bullets:
        return Check(
            key="outcomes",
            label="Measurable outcomes",
            status=FAIL,
            note="There are no bullet points, so there is nothing showing what you achieved.",
        )

    measured = [bullet for bullet in bullets if reader.has_measure(bullet)]
    share = len(measured) / len(bullets)
    if share >= MEASURED_BULLET_TARGET:
        return Check(
            key="outcomes",
            label="Measurable outcomes",
            status=PASS,
            note=f"{len(measured)} of {len(bullets)} bullets carry a number.",
        )
    return Check(
        key="outcomes",
        label="Measurable outcomes",
        status=WARN,
        note=(
            f"Only {len(measured)} of {len(bullets)} bullets carry a number. "
            "A figure is the difference between what you were given to do and what happened."
        ),
    )


def _bullets(content: dict[str, Any]) -> Check:
    bullets = reader.all_bullets(content)
    if not bullets:
        return Check(
            key="bullet_style",
            label="Bullet style",
            status=WARN,
            note="Experience written as prose is harder to scan than bullets.",
        )

    filler = [bullet for bullet in bullets if reader.opens_with_filler(bullet)]
    overlong = [bullet for bullet in bullets if len(bullet.split()) > 40]

    problems = []
    if filler:
        problems.append(f"{len(filler)} start with a duty word such as “responsible for”")
    if overlong:
        problems.append(f"{len(overlong)} run past 40 words")

    if not problems:
        return Check(
            key="bullet_style",
            label="Bullet style",
            status=PASS,
            note=f"{len(bullets)} bullets, all leading with something that happened.",
        )
    return Check(
        key="bullet_style",
        label="Bullet style",
        status=WARN,
        note="Of " + str(len(bullets)) + " bullets, " + " and ".join(problems) + ".",
    )


def _filename(filename: str) -> Check:
    if not filename:
        return Check(
            key="filename",
            label="File name",
            status=PASS,
            note="Exports from here are named after you and the version.",
        )

    stem = filename.rsplit(".", 1)[0]
    bad = stem.lower() in {"cv", "resume", "document", "untitled", "final", "cv final"}
    if bad or len(stem) < 4:
        return Check(
            key="filename",
            label="File name",
            status=WARN,
            note=(
                f"“{filename}” tells a recruiter with two hundred files nothing. "
                "Your name and the role reads better in a folder."
            ),
        )
    return Check(
        key="filename", label="File name", status=PASS, note=f"“{filename}” is identifiable."
    )


def _has_body(section: dict[str, Any]) -> bool:
    return bool(
        (section.get("text") or "").strip() or section.get("items") or section.get("entries")
    )
