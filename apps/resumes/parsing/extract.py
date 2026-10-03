"""
Text to structure, deterministically.

The approach is boring on purpose. Headings split the document; regexes find
the things that genuinely have a shape (email, phone, URL, date range); the
rest is line position. No model, no scoring, no network.

Two rules keep it honest:

  * **It never guesses silently.** Every group carries a confidence, and a
    section the reader could not find is absent rather than empty-but-claimed.
  * **It proposes terms, not skills.** Extraction emits the words it saw. The
    taxonomy decides whether "JS" is JavaScript, and the candidate decides
    whether it belongs on their profile. A parser that resolved skills itself
    would be a second, worse resolver competing with the real one.
"""

from __future__ import annotations

import re
from decimal import Decimal

from apps.files.constants import PDF

from .base import STRUCTURED_SCHEMA_VERSION, Education, Experience, ParsedResume, Project

__all__ = ["STRUCTURED_SCHEMA_VERSION", "parse"]

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
#: A PDF whose text layer has lost its spaces produces one long run, and the
#: pattern above then swallows whatever follows the address:
#: "sabinbom@gmail.comCONTACTBridge". The domain ends at its last label, and a
#: label that continues into a capital letter is the next word, not part of the
#: address — which is exactly the shape a lost space leaves behind.
EMAIL_TAIL = re.compile(r"^(?P<address>[\w.+-]+@(?:[\w-]+\.)+[a-z]{2,24})(?=[A-Z]|$)")
#: Deliberately loose: Nepali, Indian and international formats all appear, and
#: a strict pattern rejects the market this launches into first.
#
#: The class is horizontal whitespace only. With `\s` in it the pattern ran
#: across newlines, so a CV that keeps its dates in a table — two cells reading
#: "2018" and "2021", one under the other — reported "2018\n\n2021" as the
#: candidate's telephone number, and the profile showed it as one.
PHONE = re.compile(r"(?:\+?\d[\d \t().-]{7,16}\d)")
URL = re.compile(r"(?:https?://|www\.)[^\s,;)]+", re.IGNORECASE)
#: CVs write "github.com/name" far more often than they write the scheme. The
#: trailing path is required so that a company name ending in ".com" in a
#: sentence is not read as a link.
BARE_LINK = re.compile(r"(?<![\w@.])[\w-]+(?:\.[\w-]+)+/[^\s,;)]+", re.IGNORECASE)

#: A range of two years reads as a phone number to any loose phone pattern, and
#: every CV has several.
#: The separator is optional: a table puts the two years in adjacent cells
#: with nothing but space between them, and that is still a date range.
YEAR_RANGE = re.compile(r"^\s*(?:19|20)\d{2}\s*[-–—/]?\s*(?:19|20)\d{2}\s*$")

#: Section headings, in the spellings CVs actually use. Matched on a line that
#: is short and heading-shaped, so "Experience with distributed systems" in a
#: summary paragraph does not start the experience section.
HEADINGS: dict[str, tuple[str, ...]] = {
    "summary": (
        "summary",
        "profile",
        "objective",
        "about me",
        "about",
        "professional summary",
        "career objective",
        "personal statement",
        "career summary",
    ),
    # Bare "work", "career" and "experience" are here because CVs use them, and
    # a heading this vocabulary does not know costs a candidate their entire
    # work history: the section is not found, the roles are not extracted, and
    # the report tells them they have no experience. The South Asian spellings
    # are in for the same reason — this launches into that market first.
    # Internships, volunteering and academic appointments are employment for
    # everybody who has only those. A graduate whose entire history sits under
    # "INTERNSHIPS" was told they had no experience at all, and so was a
    # postdoctoral researcher whose CV says "Academic appointments" because
    # that is what academic CVs say.
    "experience": (
        "experience",
        "work",
        "work experience",
        "working experience",
        "employment",
        "employment history",
        "employment record",
        "employment experience",
        "professional experience",
        "professional background",
        "work history",
        "career",
        "career history",
        "job experience",
        "relevant experience",
        "internship",
        "internships",
        "internship experience",
        "volunteer experience",
        "volunteering",
        "volunteer work",
        "academic appointments",
        "appointments",
        "positions held",
        "research experience",
        "teaching experience",
        "clinical experience",
    ),
    "education": (
        "education",
        "academic background",
        "qualifications",
        "qualification",
        "academic qualification",
        "academic qualifications",
        "educational qualification",
        "educational qualifications",
        "academics",
        "education and training",
        "school",
        "schooling",
        "studies",
        "academic history",
    ),
    "skills": (
        "skills",
        "technical skills",
        "core skills",
        "key skills",
        "competencies",
        "core competencies",
        "expertise",
        "areas of expertise",
        "technical proficiency",
        "tools",
    ),
    "languages": ("languages", "language", "language proficiency", "languages known"),
    "projects": (
        "projects",
        "personal projects",
        "selected projects",
        "academic projects",
        "key projects",
        "project work",
    ),
    "certifications": (
        "certifications",
        "certification",
        "certificates",
        "licenses",
        "licences",
        "licence",
        "license",
        "courses",
        "training",
        "trainings",
        "professional development",
        "courses and training",
    ),
    # Grants and fellowships are an academic's awards section by another name,
    # and they are the part of that CV a reader looks at first.
    "awards": (
        "awards",
        "achievements",
        "honours",
        "honors",
        "awards and honours",
        "awards and honors",
        "accomplishments",
        "key achievements",
        "grants",
        "grants and awards",
        "funding",
        "fellowships",
        "scholarships",
        "scholarship",
        "prizes",
    ),
    "publications": (
        "publications",
        "publication",
        "papers",
        "conferences",
        "presentations",
        "selected publications",
        "peer-reviewed publications",
        "refereed publications",
        "journal articles",
        "conference papers",
        "working papers",
        "preprints",
    ),
    "interests": ("interests", "hobbies", "activities", "extracurricular activities"),
    # The singular spelling is the one a first CV uses, and leaving it out cost
    # every such candidate two skill chips reading "REFERENCE" and "Available
    # upon request" — because an unrecognised heading does not end the section
    # above it.
    "references": ("references", "reference", "referees", "referee"),
    "personal": (
        "personal details",
        "personal information",
        "personal profile",
        "declaration",
        "additional information",
    ),
}

_HEADING_LOOKUP = {spelling: key for key, spellings in HEADINGS.items() for spelling in spellings}

#: What is left of "(2022 - Present)" once the date range is removed.
EMPTY_BRACKETS = re.compile(r"[(\[{]\s*[-–—,]?\s*[)\]}]")

MAX_HEADING_WORDS = 4
BULLET = re.compile(r"^[•\-*·‣▪o]\s+")

MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    "january|february|march|april|june|july|august|september|october|november|december"
)
_DATE = rf"(?:(?:{MONTHS})[a-z]*\.?\s*)?(?:19|20)\d{{2}}"
DATE_RANGE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:-|–|—|to|until)\s*(?P<end>{_DATE}|present|current|now|ongoing)",
    re.IGNORECASE,
)

#: Separators inside a skills section. Newline is included because plenty of
#: CVs list one skill per line and nothing else.
#:
#: A slash only separates when it has space around it. "Python / Java" is two
#: skills; "CI/CD" and "A/B testing" are one each, and splitting them produced
#: the skill chips "CI", "CD", "A" and "B testing" — none of which a taxonomy
#: can resolve and none of which the candidate wrote.
SKILL_SPLIT = re.compile(r"(?:[,;|\n·•]|\s+/\s+)+")

#: A skill is a short phrase. Longer than this and it is a sentence about a
#: skill, which the taxonomy would never resolve and a curator should not see.
MAX_SKILL_WORDS = 5
MAX_SKILL_CHARS = 60

#: Lines that are structure rather than content.
NOISE = re.compile(
    r"^(page\s*\d+|curriculum vitae|resume|cv|references available on request)\.?$",
    re.IGNORECASE,
)


#: Warnings about what the document does not contain, as opposed to what could
#: not be read out of it. Named constants because the analysis has to tell the
#: two apart: a CV with no skills section extracted perfectly well, and
#: reporting that as "parts of the original file were hard to read" both
#: misdiagnoses the problem and charges the candidate machine-readability points
#: for a heading they simply did not write.
NO_SKILLS_WARNING = "No skills section found; skills were not extracted."
NO_EXPERIENCE_WARNING = "No experience section found."

#: The file is a picture. Scanned CVs, phone photographs and PDFs exported as
#: images all land here, and it is a common thing for somebody to upload.
#:
#: Named separately because it is not the same problem as a missing heading and
#: must not be reported as one. A candidate whose scan produced no text was
#: told "No experience section found", which reads as a criticism of their CV
#: rather than a fact about their file — and the fact is the most useful thing
#: this product can tell them, because no applicant tracking system will get
#: anything out of that file either.
NO_TEXT_WARNING = (
    "This file has no text in it. It is a picture of a CV rather than a document. "
    "No applicant tracking system can read it."
)

#: How much text a file can hold and still be a picture.
#:
#: Length alone is the wrong test. A scanner leaves behind a stray "Page 1 of
#: 2" or "Scanned by CamScanner"; a genuinely thin CV is short too, and telling
#: somebody with a real document that their file is a picture is worse than
#: saying nothing. So the length only applies when there is also no email, no
#: phone number and no link — none of which survive being photographed, and at
#: least one of which every real CV carries.
#:
#: Deliberately far below anything that could hold a sentence. The failure
#: modes are not symmetric: under-detecting leaves a generic warning on a file
#: that is genuinely hard to read, while over-detecting tells somebody holding
#: a real document that it is a photograph. Twenty-five characters clears
#: "Scanned by CamScanner" and "Page 1 of 2" and nothing a person wrote.
MAX_PICTURE_CHARS = 25

#: What a scanning app leaves in the text layer of a file that is otherwise a
#: photograph. Its presence is positive evidence of a scan, where an empty text
#: layer is only the absence of evidence of a document.
SCANNER_FURNITURE = re.compile(
    r"camscanner|scanned\s+(?:by|with|using)|adobe\s+scan|office\s+lens|"
    r"genius\s+scan|tapscanner|scanner|^page\s*\d+(\s*of\s*\d+)?\.?$",
    re.IGNORECASE,
)

#: The file is a document and it is empty. Not the same problem as a scan, and
#: telling somebody who uploaded a blank file that it is a photograph sends
#: them to find a scanner they never used.
EMPTY_WARNING = (
    "There is almost nothing in this file. If the CV you meant to send is in "
    "another document, upload that one."
)

#: Below this the file holds a title and nothing else.
MIN_DOCUMENT_CHARS = 120

#: Every warning in this set describes structure, not readability.
STRUCTURE_WARNINGS = frozenset({NO_SKILLS_WARNING, NO_EXPERIENCE_WARNING})


def parse(text: str, *, mime: str = "") -> ParsedResume:
    """Read a CV's text into the structure P4 proposes from.

    `mime` is optional and only ever narrows a conclusion. The one thing it
    decides is whether a file with no text in it can be a photograph: a PDF
    can, and a Word file cannot. Without it, somebody who uploaded an empty
    .docx was told their file was "a picture of a CV rather than a document"
    and advised to export it from Word as a PDF instead of scanning it —
    advice about a scanner they never used, for a document they wrote in Word.
    """
    lines = [line.strip() for line in (text or "").split("\n")]
    sections = _split_sections(lines)
    warnings: list[str] = []

    emails = _unique(_clean_email(match) for match in EMAIL.findall(text))
    phones = _phones(text)
    links = _links(text)

    # Said first and said alone. A file with no text has no sections either, so
    # following the real problem with two guesses about missing headings buries
    # the one thing worth reading.
    if _looks_like_a_picture(text, emails, phones, links, mime=mime):
        return ParsedResume(raw_text=text or "", warnings=(NO_TEXT_WARNING,))

    # A document with a text layer, a heading or two and no content. Said
    # alone for the same reason: it is the only thing worth reading.
    if len((text or "").strip()) < MIN_DOCUMENT_CHARS and not sections:
        return ParsedResume(
            raw_text=text or "",
            name=_name_and_headline(lines, emails, phones)[0],
            emails=emails,
            phones=phones,
            links=links,
            warnings=(EMPTY_WARNING,),
        )

    skills = _skill_terms(sections.get("skills", ""))
    if "skills" not in sections:
        warnings.append(NO_SKILLS_WARNING)

    experience = _experience(sections.get("experience", ""))
    if "experience" not in sections:
        warnings.append(NO_EXPERIENCE_WARNING)

    education = _education(sections.get("education", ""))
    languages = _languages(sections.get("languages", ""))
    name, headline = _name_and_headline(lines, emails, phones)

    return ParsedResume(
        location=_location(lines, emails, phones),
        raw_text=text,
        name=name,
        emails=emails,
        phones=phones,
        links=links,
        headline=headline,
        summary=sections.get("summary", "")[:2000],
        skill_terms=skills,
        experience=experience,
        education=education,
        languages=languages,
        certifications=_line_items(sections.get("certifications", "")),
        projects=_projects(sections.get("projects", "")),
        awards=_line_items(sections.get("awards", "")),
        publications=_line_items(sections.get("publications", "")),
        sections=sections,
        confidence=_confidence(emails, phones, skills, experience, education),
        warnings=tuple(warnings),
    )


# --- sections -------------------------------------------------------------


def _heading_text(line: str) -> str:
    """A heading line with the decoration Word leaves on it removed.

    Built-in heading styles and hand-drawn rules produce "EXPERIENCE ———",
    "Work Experience:" and "— Education —". None of that is part of the word.
    """
    return line.strip().strip(":").strip(" \t-–—_=*·•|").strip()


def _heading_key(line: str) -> str | None:
    """The section a line starts, if it is a heading this reader knows."""
    stripped = _heading_text(line)
    if not stripped or len(stripped.split()) > MAX_HEADING_WORDS:
        return None
    return _HEADING_LOOKUP.get(stripped.casefold())


#: A heading with a count or a qualifier after it: "EXPERIENCE (5 YEARS)".
_HEADING_TRAILER = re.compile(r"\s*[(\[].*$")


def _unknown_heading(line: str) -> str | None:
    """A line that is plainly a section heading whose name is not in the list.

    This exists because of what happens without it. An unrecognised heading
    does not end the section above it, so everything underneath is read as more
    of the previous section: "REFERENCE / Available upon request" became two
    skills on a candidate's profile, a publication list became education, and a
    "COURSES" block became a second degree. Every one of those is the worst
    class of error this reader can make — content the candidate never claimed,
    attributed to them by the platform.

    Ending the section on a heading-shaped line is the conservative move. The
    text is kept under its own key rather than discarded, so a section this
    vocabulary grows to understand later is already there.

    The test is deliberately strict: short, no sentence punctuation, no digits
    doing the work, and either set in capitals or written in title case the way
    a heading is. "Cut latency from 2.1s to 380ms" fails it; "PUBLICATIONS",
    "Volunteer Work" and "Extra-curricular" pass.

    The caller adds the last condition, and it is the one that makes this safe:
    **a blank line above.** Without it, a CV that lists one skill per line has
    "SQL" read as a heading and loses the three skills under it, and a degree
    written over three lines has "Tribhuvan University" read as one. Requiring
    the gap costs nothing on a document that does not have it — the vocabulary
    above still does the work — and removes the whole class of false break.
    """
    stripped = _HEADING_TRAILER.sub("", _heading_text(line)).strip()
    if not stripped or not any(character.isalpha() for character in stripped):
        return None
    words = stripped.split()
    if len(words) > MAX_HEADING_WORDS:
        return None
    if stripped[-1] in ".,;!?":
        return None
    if EMAIL.search(stripped) or URL.search(stripped) or BARE_LINK.search(stripped):
        return None
    if DATE_RANGE.search(stripped) or any(character.isdigit() for character in stripped):
        return None

    letters = [character for character in stripped if character.isalpha()]
    if letters and all(character.isupper() for character in letters):
        return _slug(stripped)

    significant = [word for word in words if len(word) > 3]
    if significant and all(word[:1].isupper() for word in significant):
        return _slug(stripped)
    return None


_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return "other:" + _SLUG.sub("-", value.casefold()).strip("-")


def _split_sections(lines: list[str]) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    after_gap = True

    for line in lines:
        if not line:
            after_gap = True
            continue

        key = _heading_key(line)
        # A bulleted line is an item, never a heading. Word puts a blank line
        # between list paragraphs, so "• Python" and "• Docker" arrived after a
        # gap, passed the heading test once the bullet was stripped, and each
        # became an empty section of its own — a bulleted skills list, the
        # commonest there is, left the CV with no skills at all.
        if key is None and after_gap and not BULLET.match(line):
            key = _unknown_heading(line)
        after_gap = False

        if key is not None:
            current = key
            sections.setdefault(current, [])
            continue
        if current is not None and not NOISE.match(line):
            sections[current].append(line)

    return {key: "\n".join(body).strip() for key, body in sections.items() if body}


# --- fields ---------------------------------------------------------------


def _phones(text: str) -> tuple[str, ...]:
    """Telephone numbers, and nothing that merely contains digits.

    Links and addresses are blanked out first. An ORCID — "orcid.org/
    0000-0002-1825-0097" — is sixteen digits with separators and matches any
    phone pattern loose enough to accept the formats this launches into; an
    academic's CV therefore listed their ORCID as their telephone number, on
    their profile and in front of employers. A DOI, an ISBN and a tracking
    reference all fail the same way, and masking the links removes all of them
    at once.
    """
    masked = URL.sub(" ", text or "")
    masked = BARE_LINK.sub(" ", masked)
    masked = EMAIL.sub(" ", masked)

    found = []
    for candidate in PHONE.findall(masked):
        if YEAR_RANGE.match(candidate):
            # "2018 - 2022" is eight digits and matches any loose phone
            # pattern. Every CV contains several.
            continue
        digits = [character for character in candidate if character.isdigit()]
        if 7 <= len(digits) <= 15:
            found.append(candidate.strip())
    return _unique(found)


def _looks_like_a_picture(
    text: str,
    emails: tuple[str, ...],
    phones: tuple[str, ...],
    links: tuple[str, ...],
    *,
    mime: str = "",
) -> bool:
    """Whether this file is a scan or a photograph rather than a document.

    Every real CV carries an email address, a phone number or a link, and none
    of the three survives being photographed — a raster PDF's text layer holds
    producer metadata and nothing else. So the absence of all three, together
    with almost no text, is the signal; either half alone would misfire on a
    thin CV or on a file that happens to be short.
    """
    if emails or phones or links:
        return False
    if mime and mime != PDF:
        # Only a PDF can be a photograph. A Word file with nothing in it is an
        # empty Word file, and telling its author to stop scanning is advice
        # about a machine they never touched.
        return False

    body = (text or "").strip()
    if len(body) >= MAX_PICTURE_CHARS:
        return False

    # Short, and with none of the three contact shapes. That is either a scan
    # or a document somebody left empty, and the two need different advice: one
    # person has to export their CV differently, the other has to upload a
    # different file. A raster PDF's text layer holds no letters at all, or
    # holds the scanner's own name.
    if not any(character.isalpha() for character in body):
        return True
    return bool(SCANNER_FURNITURE.search(body))


def _links(text: str) -> tuple[str, ...]:
    """URLs, with or without a scheme, and never an email address."""
    found = [*URL.findall(text), *BARE_LINK.findall(text)]
    return _unique(link.rstrip(".,;") for link in found if "@" not in link)


def _headline(lines: list[str], emails: tuple[str, ...], phones: tuple[str, ...]) -> str:
    """The first line that reads like a title rather than a name or contact.

    Position is the only signal available without a model, so this is offered
    as a proposal and never written anywhere on its own.

    Contact lines are skipped, and that includes the ones written without a
    scheme. A CV whose header reads "linkedin.com/in/priya | github.com/priyash"
    is five words, is not a name, and passed every test here — so that string
    became the candidate's job title, on their profile and on the dashboard
    above their own name. `URL` only matches a scheme or a leading "www.";
    `BARE_LINK` is the pattern that catches the rest, and both belong in this
    guard.

    The docstring above was the intent from the start; the name test was the
    part that was missing. Every CV opens with the candidate's name, that line
    passed every filter here, and the builder therefore showed "Sita Rai" as
    Sita Rai's current job title. One name line is skipped — there is only ever
    one — and a heading is never a headline.

    **Positive evidence, not merely the absence of a reason to refuse.** Taking
    the first line nothing objected to gave a designer's CV the headline
    "slightly annoyed. Six years, mostly in logistics and" — a fragment of a
    sentence from a two-column layout that extracted out of order — and gave a
    form-style CV the headline "Name - sunita gurung". Both passed every guard
    above and neither is a job title. A line now has to look like one: it names
    a role, or it is short and set in title case the way a strapline is.

    When nothing qualifies the answer is nothing, which the candidate fills in
    themselves — and which the builder asks them for directly.
    """
    _name, headline = _name_and_headline(lines, emails, phones)
    return headline


def name_from(lines: list[str], emails: tuple[str, ...], phones: tuple[str, ...]) -> str:
    """The candidate's own name, off the top of their CV.

    Found by the same pass that looks for the headline, because it is the same
    question asked once: which of these header lines is the person, and which
    is the job. The pass already identified the name line in order to skip it,
    and threw the answer away — so an imported CV took its email from the file
    and its name from the account, and a candidate whose account name differs
    from the one on their CV got a document with both.
    """
    name, _headline = _name_and_headline(lines, emails, phones)
    return name


#: What a contact line separates its parts with.
_CONTACT_SEPARATOR = re.compile(r"\s*(?:\||•|·|\u2022|;|\s{3,}|\t)\s*")
#: One part of a place name: a capitalised word, possibly hyphenated.
_PLACE_WORD = re.compile(r"^[A-Z][A-Za-z'.-]+$")
#: Labels a contact line uses that are not places.
_NOT_A_PLACE = frozenset(
    [
        "linkedin",
        "github",
        "portfolio",
        "website",
        "web",
        "email",
        "e-mail",
        "phone",
        "mobile",
        "tel",
        "telephone",
        "cell",
        "address",
        "contact",
        "remote",
        "hybrid",
        "available",
        "relocation",
        "cv",
        "resume",
    ]
)


def _location(lines: list[str], emails: tuple[str, ...], phones: tuple[str, ...]) -> str:
    """The place on the contact line, if the CV writes one there.

    "maya@example.com | +977 98… | Pokhara, Nepal" — two CVs in the 2026-09
    audits wrote their city here and it was dropped on import, so the assistant
    then asked the candidate where they were based. Only the header is read, and
    only a line that also carries the email or phone: a comma phrase further down
    is a job ("Engineer, Fonepay"), not an address. Nothing is inferred: a part
    must look like a place name — capitalised words, no digits, no link.
    """
    for line in [line for line in lines[:8] if line][:8]:
        if not (any(email in line for email in emails) or any(phone in line for phone in phones)):
            continue
        for part in _CONTACT_SEPARATOR.split(line):
            part = part.strip(" ,")
            if not part or any(value in part for value in (*emails, *phones)):
                continue
            if EMAIL.search(part) or URL.search(part) or BARE_LINK.search(part):
                continue
            if any(character.isdigit() for character in part):
                continue
            pieces = [piece.strip() for piece in part.split(",")]
            words = [word for piece in pieces for word in piece.split()]
            if not (1 <= len(pieces) <= 3 and 1 <= len(words) <= 6):
                continue
            if not all(_PLACE_WORD.match(word) for word in words):
                continue
            if any(word.lower().strip(".") in _NOT_A_PLACE for word in words):
                continue
            return ", ".join(pieces)[:120]
    return ""


def _name_and_headline(
    lines: list[str], emails: tuple[str, ...], phones: tuple[str, ...]
) -> tuple[str, str]:
    name = ""
    skipped_name = False
    for line in lines[:12]:
        if not line or NOISE.match(line):
            continue

        # "Name - Sunita Karki". A form-style CV writes every header field this
        # way, and the guard below correctly refuses to read the line as a job
        # title — but nothing read it as the name either, so a whole class of
        # first CV imported with no name on it at all.
        labelled = _labelled_name(line)
        if labelled:
            if not skipped_name:
                name, skipped_name = labelled, True
            continue

        if any(email in line for email in emails) or any(phone in line for phone in phones):
            continue
        if EMAIL.search(line) or URL.search(line) or BARE_LINK.search(line):
            continue
        if _heading_key(line) is not None:
            # The header block is over. Anything past the first heading belongs
            # to a section, and reading it as a job title is how a CV whose
            # education came first was given the headline "BSc Computer
            # Science". No headline is better than the wrong one.
            break

        words = line.split()
        # One word is normally a heading, not a name — except in a script that
        # writes a whole name as one token. 张伟 and 김민준 are complete names
        # and were skipped before anything looked at them.
        single_token_name = len(words) == 1 and not skipped_name and _looks_like_a_name(words)
        if (not (2 <= len(words) <= 10) and not single_token_name) or line.endswith("."):
            continue

        if not skipped_name and _looks_like_a_name(words):
            skipped_name = True
            name = line[:120]
            continue

        if ROLE_WORDS.search(line) and not LABELLED.match(line):
            return name, line[:160]

        # Not a role by vocabulary. Before falling back to "short and title
        # cased", which is also the shape of every section heading ever
        # written, stop at one: a postdoctoral researcher whose CV opens with
        # "ACADEMIC APPOINTMENTS" was given that as their job title.
        if skipped_name and _unknown_heading(line) is not None:
            break

        if not _reads_like_a_role(line, words):
            continue

        return name, line[:160]
    return name, ""


#: A form label and its value: "Name - sunita gurung", "Position: Officer".
#: The left side is short and the separator is doing the work of a colon.
LABELLED = re.compile(r"^[A-Za-z ]{2,20}\s*[-–—:]\s+\S")

#: The same shape, where the label is specifically the person's name.
NAME_FIELD = re.compile(
    r"^(?:full\s+)?name(?:\s+of\s+(?:the\s+)?(?:candidate|applicant))?\s*[-–—:]\s*(?P<value>.+)$",
    re.IGNORECASE,
)


def _labelled_name(line: str) -> str:
    """The value of a "Name:" field, if this line is one."""
    match = NAME_FIELD.match(line.strip())
    if match is None:
        return ""
    value = match.group("value").strip(" .,:;|")
    words = value.split()
    if not (2 <= len(words) <= 5) or any(character.isdigit() for character in value):
        return ""
    return value[:120]


def _reads_like_a_role(line: str, words: list[str]) -> bool:
    """Whether this line is plausibly somebody's job title.

    Two ways to qualify, and a line that does neither is passed over:

      * it contains a word from the role vocabulary — "Senior Backend
        Engineer", "Marketing Manager", "Staff Nurse";
      * or it is short and title-cased, which is how a strapline is written
        and is not how a sentence continues.
    """
    if LABELLED.match(line):
        return False

    # A fragment of running prose: an extractor reading a two-column layout
    # produces these constantly, and they always start mid-sentence.
    first = words[0]
    if first[:1].islower():
        return False

    if ROLE_WORDS.search(line):
        return True

    # A strapline: four words or fewer, every significant word capitalised —
    # or, in a script with no case, simply written in it.
    if len(words) > 4:
        return False
    significant = [word for word in words if len(word) > 3]
    return bool(significant) and all(_opens_like_a_proper_noun(word) for word in significant)


#: Words that make a line a job title rather than a name. Short list on
#: purpose: it only has to beat "two capitalised words".
ROLE_WORDS = re.compile(
    r"\b(engineer|developer|manager|analyst|designer|consultant|accountant|nurse|"
    r"teacher|scientist|architect|administrator|officer|specialist|director|lead|"
    r"head|intern|assistant|executive|coordinator|technician|researcher|advisor|"
    r"student|graduate|freelance|founder|partner|associate)\b",
    re.IGNORECASE,
)


def _opens_like_a_proper_noun(word: str) -> bool:
    """Whether a word starts the way a name does, in any writing system.

    "Capitalised" is a property of cased scripts only. Devanagari, Arabic,
    Thai, Chinese, Japanese and Korean have no case at all, so `isupper()` is
    False for every character in them — and a test written as "each word is
    capitalised" therefore rejects every name written in any of them.

    That is not a niche: this product's own vocabulary is tuned for Nepal and
    launches into that market first, where नवराज गौतम is an ordinary way to
    write a name. Every such candidate imported a CV with no name on it, and
    the builder then offered them the name on their account instead.

    An uncased letter is one whose upper and lower case are the same character.
    Digits and punctuation satisfy that too, so the letter test comes first.
    """
    first = word[:1]
    if not first.isalpha():
        return False
    return first.isupper() or first.upper() == first.lower()


def _is_uncased(word: str) -> bool:
    letters = [character for character in word if character.isalpha()]
    return bool(letters) and all(character.upper() == character.lower() for character in letters)


#: How long a single-token name can be. Chinese, Japanese and Korean names are
#: written as one word of two to four characters; beyond that a lone uncased
#: token is a heading or a sentence, not somebody's name.
MAX_SINGLE_TOKEN_NAME = 6


def _looks_like_a_name(words: list[str]) -> bool:
    """Whether a short line is a person's name rather than a job title.

    Two to four words, each starting like a proper noun, no digits and no word
    from the role vocabulary. "Sita Rai" and "प्रिया शर्मा" pass; "Marketing
    Manager" and "Senior Backend Engineer" do not, which is the only
    distinction that has to hold.

    One word is allowed only when it is written in an uncased script, because
    that is how a Chinese, Japanese or Korean name is written and there is no
    second token to look for. A single Latin word is not a name here — every
    section heading is one.
    """
    if len(words) == 1:
        return _is_uncased(words[0]) and len(words[0]) <= MAX_SINGLE_TOKEN_NAME
    if not 2 <= len(words) <= 4:
        return False
    if ROLE_WORDS.search(" ".join(words)):
        return False
    return all(
        _opens_like_a_proper_noun(word) and not any(c.isdigit() for c in word) for word in words
    )


def _skill_terms(section: str) -> tuple[str, ...]:
    terms = []
    for raw in _split_outside_brackets(section):
        term = BULLET.sub("", raw).strip(" .:-–—")
        term = _balance_brackets(term)
        if not term or len(term) > MAX_SKILL_CHARS:
            continue
        if len(term.split()) > MAX_SKILL_WORDS:
            continue
        if term.casefold() in {"and", "or", "etc"}:
            continue
        terms.append(term)
    return _unique(terms)


def _clean_email(match: str) -> str:
    """Trim an address that ran into the text after it."""
    trimmed = EMAIL_TAIL.match(match)
    return trimmed.group("address") if trimmed else match


def _split_outside_brackets(section: str) -> list[str]:
    """Split a skills list, ignoring separators inside brackets.

    "observability (Prometheus, Grafana, OpenTelemetry)" is one skill written
    with its examples. Splitting on the commas inside the bracket produced
    three terms, two of which carried half a bracket: "observability
    (Prometheus" and "OpenTelemetry)".
    """
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    index = 0
    while index < len(section):
        character = section[index]
        if character in "([{":
            depth += 1
        elif character in ")]}" and depth:
            depth -= 1

        if depth == 0:
            separator = SKILL_SPLIT.match(section, index)
            if separator is not None and separator.end() > index:
                parts.append("".join(buffer))
                buffer = []
                index = separator.end()
                continue

        buffer.append(character)
        index += 1

    parts.append("".join(buffer))
    return parts


def _balance_brackets(term: str) -> str:
    """Drop a trailing fragment that has lost its opening bracket, and close
    one that has lost its closing bracket."""
    opened = term.count("(") - term.count(")")
    if opened > 0:
        term = term + ")" * opened
    elif opened < 0:
        term = term.replace(")", "", -opened)
    return term.strip()


def _experience(section: str) -> tuple[Experience, ...]:
    """Group the experience section into roles, split on dated lines.

    A dated line starts a role and the lines under it belong to it. That is the
    structure of essentially every CV, and where it is wrong the candidate sees
    the proposal and rejects it — which is exactly why proposals exist.
    """
    rows: list[Experience] = []
    header: str | None = None
    match: re.Match[str] | None = None
    bullets: list[str] = []

    def flush() -> None:
        nonlocal bullets
        if header is None or match is None:
            return
        title, org = _title_and_org(header, match)
        if not org:
            org, bullets = _org_from_first_line(bullets)
        end = match.group("end")
        rows.append(
            Experience(
                title=title,
                org=org,
                start=match.group("start"),
                end="" if _is_present(end) else end,
                is_current=_is_present(end),
                bullets=tuple(bullets),
                evidence=header[:300],
            )
        )

    # A PDF wraps a long achievement across two lines, and reading each line as
    # its own bullet turned "…went to zero over 18 months." into an achievement
    # called "18 months." on the candidate's CV. When the section marks its
    # bullets, an unmarked line is a continuation of the one above; when it
    # marks none — a Word file, where the marker is a paragraph style rather
    # than a character — a line opening in lower case or a digit is.
    lines = _join_date_lines(section.split("\n"))
    marked = any(BULLET.match(line) for line in lines)

    for line in lines:
        found = DATE_RANGE.search(line)
        if found is not None:
            flush()
            header, match, bullets = line, found, []
        elif header is not None and line:
            if bullets and _continues(line, bullets[-1], marked=marked):
                bullets[-1] = f"{bullets[-1]} {line.strip()}"
            else:
                bullets.append(BULLET.sub("", line).strip())

    flush()
    return tuple(rows)


#: A line that is nothing but a date range, with whatever punctuation the
#: layout left around it. Word's two-line role block writes one of these under
#: every job, and a table cell holds one on its own.
DATE_ONLY = re.compile(
    rf"^[\s(\[|·—–-]*{DATE_RANGE.pattern}[\s)\]|·—–.,-]*$",
    re.IGNORECASE,
)


def _join_date_lines(lines: list[str]) -> list[str]:
    """Put a role and the dates written under it back on one line.

    A very large share of Word CVs — and every CV whose history is a table —
    write the role on one line and its dates on the next:

        Staff Nurse — Tribhuvan University Teaching Hospital
        2020 — Present

    The grouper below starts a role at a dated line, so on those documents the
    dated line became a role with no title and the title became a bullet under
    it. The candidate was then told they had no work experience, on a CV that
    is two thirds work experience. Joining the pair first is the whole fix, and
    it changes nothing for the documents that already wrote them together.

    Dates above the role are joined the same way: "2021 - Present" followed by
    "Product Designer, Foodmandu" is the same document written upside down, and
    two-column layouts produce it constantly.
    """
    joined: list[str] = []
    pending_dates: str = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if DATE_ONLY.match(stripped):
            previous = joined[-1] if joined else ""
            if previous and not DATE_RANGE.search(previous) and not BULLET.match(previous):
                joined[-1] = f"{previous} — {stripped}"
            else:
                pending_dates = stripped
            continue

        if pending_dates and not DATE_RANGE.search(stripped) and not BULLET.match(stripped):
            joined.append(f"{stripped} — {pending_dates}")
            pending_dates = ""
            continue

        pending_dates = ""
        joined.append(stripped)

    return joined


#: A bullet that ends on one of these has finished its sentence.
_SENTENCE_END = ".!?;:"


def _continues(line: str, previous: str, *, marked: bool) -> bool:
    """Whether this line is the rest of the bullet above it."""
    if BULLET.match(line):
        return False
    if not previous or previous[-1] in _SENTENCE_END:
        return False
    if marked:
        # The section marks its bullets and this line carries no marker, so it
        # is the tail of the last one however it happens to begin.
        return True
    first = line.lstrip()[:1]
    return bool(first) and (first.islower() or first.isdigit())


def _org_from_first_line(bullets: list[str]) -> tuple[str, list[str]]:
    """The employer, when it is written on the line under the job title.

    Designers and anyone using a modern Word template write a role as three
    stacked lines — title, employer, then the achievements — and the dates sit
    on the title line. Everything under the title was read as an achievement,
    so the role reached the candidate's profile with no employer at all and
    their first bullet point was the company's name.

    Only a line that plainly is not an achievement is taken: short, opening in
    capitals, no verb-like sentence ending. Anything else stays a bullet, which
    is the safe direction — a missing employer is a blank the candidate fills
    in, an achievement eaten as an employer is content they lose.
    """
    if not bullets:
        return "", bullets

    first = bullets[0].strip()
    words = first.split()
    if not (1 <= len(words) <= 5):
        return "", bullets
    if first[-1:] in ".!?;:" or first[:1].islower():
        return "", bullets
    if DATE_RANGE.search(first) or EMAIL.search(first) or URL.search(first):
        return "", bullets
    return first[:160], bullets[1:]


def _title_and_org(header: str, dates: re.Match[str]) -> tuple[str, str]:
    """Split "Backend Engineer, Nexa Payments — 2023 to present" into its parts."""
    without_dates = (header[: dates.start()] + " " + header[dates.end() :]).strip(" ,–—-|·")
    # "Data Scientist, NIC Asia Bank (2022-Present)" leaves "( )" once the dates
    # are cut out, and that reached the profile as part of the employer's name.
    without_dates = EMPTY_BRACKETS.sub(" ", without_dates).strip(" ,–—-|·")
    parts = [part.strip(" ,–—-|·") for part in re.split(r"[,|·—–]|\sat\s", without_dates)]
    parts = [part for part in parts if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0][:160], ""
    return parts[0][:160], parts[1][:160]


#: A word that makes a line the place somebody studied rather than what they
#: studied. Deliberately a word list and not a heuristic: "Amrit Science
#: Campus" and "Bachelor of Science" differ by exactly this.
AWARDING_BODY = re.compile(
    r"\b(university|universit[a-z]*|college|campus|school|institute|institution|academy|"
    r"polytechnic|faculty|seminary|board|vidyalaya|vidhyalaya|niketan|gurukul)\b",
    re.IGNORECASE,
)


def _education(section: str) -> tuple[Education, ...]:
    """Group the education section into qualifications.

    One line is not one degree. Word CVs write a qualification over two or
    three lines — the degree, the institution under it, the years under that —
    and reading each line as its own entry gave a teacher three degrees, one of
    which was called "Tribhuvan University" and one of which was called
    "2012 - 2014". A candidate then saw all three offered for import, and the
    analysis counted them.

    So a line is a continuation when it supplies something the entry above is
    missing: the awarding body, or the dates. Anything else starts a new one.
    """
    rows: list[Education] = []
    pending: dict[str, str] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None and (pending["qualification"] or pending["institution"]):
            rows.append(
                Education(
                    qualification=pending["qualification"][:160],
                    institution=pending["institution"][:160],
                    start=pending["start"],
                    end=pending["end"],
                    evidence=pending["evidence"][:300],
                )
            )
        pending = None

    for raw in _join_date_lines(section.split("\n")):
        line = BULLET.sub("", raw).strip()
        if not line:
            continue

        dates = DATE_RANGE.search(line)
        cleaned = line if dates is None else (line[: dates.start()] + line[dates.end() :])
        cleaned = EMPTY_BRACKETS.sub(" ", cleaned)
        parts = [part.strip(" ,–—-|·") for part in re.split(r"[,|·—–]", cleaned) if part.strip()]
        start = dates.group("start") if dates else ""
        end = "" if dates is None or _is_present(dates.group("end")) else dates.group("end")

        if not parts:
            # A bare date line under the entry above it.
            if pending is not None and not pending["start"]:
                pending["start"], pending["end"] = start, end
            continue

        is_body = bool(AWARDING_BODY.search(parts[0])) and len(parts) == 1
        if pending is not None and is_body and not pending["institution"]:
            pending["institution"] = parts[0]
            pending["evidence"] = f"{pending['evidence']} {line}".strip()
            if start and not pending["start"]:
                pending["start"], pending["end"] = start, end
            continue

        flush()
        pending = {
            "qualification": parts[0],
            "institution": parts[1] if len(parts) > 1 else "",
            "start": start,
            "end": end,
            "evidence": line,
        }

    flush()
    return tuple(rows)


def _languages(section: str) -> tuple[str, ...]:
    return _skill_terms(section)


#: A line this long is prose, not the name of a certificate or an award.
MAX_ITEM_CHARS = 300

#: "1.", "2)", "[3]" — how a publication list is numbered. Numbering is a list
#: marker exactly as a bullet is, and reading it as one is what keeps a wrapped
#: citation attached to its own entry.
ENUMERATOR = re.compile(r"^(?:\[\d{1,3}\]|\(?\d{1,3}[.)])\s+")


def _line_items(section: str) -> tuple[str, ...]:
    """One item per line, with wrapped lines put back together.

    Certifications, awards and publications are all written the same way: a
    list, one per line, each of which a PDF may have wrapped across two. A
    reader that took every line would give an academic two publications where
    they wrote one, and the second would be half a title.

    A numbered list is the case that made this its own function. Academic CVs
    number their publications, every citation wraps, and the continuation of a
    citation starts with a capital — "…in the Koshi basin. Journal of" /
    "Himalayan Hydrology, 11(2), 107-119". The bullet-continuation rule reads
    an opening capital as a new item, so each citation split in two and the
    next one's number glued itself onto the stray half. Where the section
    numbers itself, the number is the only thing that starts an item.
    """
    lines = [line for line in section.split("\n") if line.strip()]
    numbered = sum(1 for line in lines if ENUMERATOR.match(line.strip())) >= 2
    marked = any(BULLET.match(line) for line in lines)

    items: list[str] = []
    for raw in lines:
        line = raw.strip()
        starts_item = (
            ENUMERATOR.match(line)
            if numbered
            else not _continues(line, items[-1] if items else "", marked=marked)
        )
        if items and not starts_item:
            items[-1] = f"{items[-1]} {line}"[:MAX_ITEM_CHARS]
            continue
        cleaned = ENUMERATOR.sub("", BULLET.sub("", line)).strip(" .;,")
        if cleaned and not NOISE.match(cleaned):
            items.append(cleaned[:MAX_ITEM_CHARS])
    return _unique(items)


#: What separates a project's name from its one-line description. A spaced
#: dash or a colon; not a hyphen inside a word, which is how "Bus-Tracker"
#: would otherwise lose half its name.
PROJECT_SPLIT = re.compile(r"\s+[—–]\s+|\s+-\s+|\s*:\s+|\s+\|\s+")


def _projects(section: str) -> tuple[Project, ...]:
    """Projects, with whatever the candidate said about each one.

    Shaped like roles rather than like a list, because that is what they are
    to the people who lead with them — a student, a designer, a career changer.
    An unmarked line starts a project and a bullet under it belongs to it, so
    "Bus Tracker — a live map of valley routes" is one project with one line
    about it rather than two projects.
    """
    rows: list[Project] = []
    lines = [line for line in section.split("\n") if line.strip()]
    marked = any(BULLET.match(line) for line in lines)
    # A section written as a plain bulleted list has no name lines at all, and
    # a bullet was only ever attached to the project above it — so every one
    # of them was dropped and the candidate's projects silently vanished. With
    # no name above it, a bullet is a project of its own.
    named = any(not BULLET.match(line.strip()) for line in lines)

    for raw in lines:
        line = raw.strip()

        if BULLET.match(line):
            if rows and named:
                rows[-1] = _with_bullet(rows[-1], BULLET.sub("", line).strip())
                continue
            line = BULLET.sub("", line).strip()

        if rows and rows[-1].bullets and _continues(line, rows[-1].bullets[-1], marked=marked):
            joined = f"{rows[-1].bullets[-1]} {line}"[:MAX_ITEM_CHARS]
            rows[-1] = Project(
                name=rows[-1].name,
                bullets=(*rows[-1].bullets[:-1], joined),
                evidence=rows[-1].evidence,
            )
            continue

        parts = PROJECT_SPLIT.split(line, maxsplit=1)
        name = parts[0].strip()
        description = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            continue
        rows.append(
            Project(
                name=name[:160],
                bullets=((description[:MAX_ITEM_CHARS],) if description else ()),
                evidence=line[:300],
            )
        )
    return tuple(rows)


def _with_bullet(project: Project, text: str) -> Project:
    if not text:
        return project
    return Project(
        name=project.name,
        bullets=(*project.bullets, text[:MAX_ITEM_CHARS]),
        evidence=project.evidence,
    )


def _is_present(value: str) -> bool:
    return value.strip().casefold() in {"present", "current", "now", "ongoing"}


def _unique(values) -> tuple[str, ...]:
    """Order-preserving deduplication, case-insensitive."""
    seen: dict[str, str] = {}
    for value in values:
        key = value.strip().casefold()
        if key and key not in seen:
            seen[key] = value.strip()
    return tuple(seen.values())


def _confidence(emails, phones, skills, experience, education) -> dict[str, Decimal]:
    """How much to trust each group.

    Matched patterns score high; anything derived from line position scores
    low, because that is honestly how reliable it is.
    """
    return {
        "contact": Decimal("0.95") if emails else (Decimal("0.60") if phones else Decimal("0")),
        "skills": Decimal("0.70") if skills else Decimal("0"),
        "experience": Decimal("0.55") if experience else Decimal("0"),
        "education": Decimal("0.50") if education else Decimal("0"),
    }
