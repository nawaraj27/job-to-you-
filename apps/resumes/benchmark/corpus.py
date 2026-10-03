"""
The benchmark corpus: documents, and the truth they were written from.

Every case is a CV somebody could plausibly have written, declared once as
lines of text and rendered to real PDF or DOCX bytes by `render`. The reader
under test never sees the lines — it sees the file, exactly as it would see an
upload — so a regression in the PDF text layer counts against the score the
same way a regression in the section splitter does.

`Truth` is deliberately written *beside* the document rather than derived from
it. Deriving it would test the parser against itself.

Three rules the corpus obeys, because breaking them turns the benchmark into
decoration:

  * **Not every CV has every field.** A student CV has no employment and its
    truth says so. Scoring an empty field as a miss would teach the parser to
    invent one.
  * **Hard cases stay in.** Two-column layouts, tables, scans and unusual
    headings are the documents candidates actually upload. Removing the ones
    that fail is how a benchmark reaches 100% and the product does not.
  * **The document is realistic, not convenient.** Dates are written the many
    ways people write them; headings are spelled the many ways people spell
    them.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from apps.files.constants import DOCX, PDF

__all__ = ["CASES", "Case", "Truth", "render"]


@dataclass(frozen=True)
class Role:
    title: str
    org: str
    start: str = ""
    end: str = ""
    is_current: bool = False


@dataclass(frozen=True)
class Study:
    qualification: str
    institution: str = ""


@dataclass(frozen=True)
class Truth:
    """What is genuinely on the document.

    An empty tuple means "this CV does not contain that", which is a fact to be
    scored, not an omission to be forgiven: a parser that reports experience on
    a school-leaver's CV is wrong in the direction that matters most.
    """

    name: str = ""
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    headline: str = ""
    has_summary: bool = False
    skills: tuple[str, ...] = ()
    experience: tuple[Role, ...] = ()
    education: tuple[Study, ...] = ()
    languages: tuple[str, ...] = ()
    certifications: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    awards: tuple[str, ...] = ()
    #: Text that must never appear as a skill. Section furniture, soft-skill
    #: filler and stray headings all reach the skill list through the same
    #: door, and each one is a chip the candidate has to read and dismiss.
    never_skills: tuple[str, ...] = ()
    #: The document is a picture; nothing can be read out of it and the right
    #: answer is to say so.
    is_picture: bool = False


@dataclass(frozen=True)
class Case:
    slug: str
    sector: str
    profile: str
    body: tuple[str, ...]
    truth: Truth
    #: "linear" — one column, the common case.
    #: "two_column" — a designer's CV; the split marker is `COLUMN_BREAK`.
    #: "table" — the dated rows live in a table, as Word users write them.
    layout: str = "linear"
    formats: tuple[str, ...] = ("pdf",)
    notes: str = ""
    #: Rendered with no text layer at all, the way a scan arrives.
    picture: bool = False


COLUMN_BREAK = "<<<COLUMN>>>"


# --- rendering ------------------------------------------------------------


def render(case: Case, fmt: str) -> bytes:
    """The case as the bytes an upload would carry."""
    if fmt == "pdf":
        return _to_pdf(case)
    if fmt == "docx":
        return _to_docx(case)
    raise ValueError(f"No renderer for {fmt!r}.")


MIME = {"pdf": PDF, "docx": DOCX}


def _to_pdf(case: Case) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    width, height = A4
    pdf = canvas.Canvas(buffer, pagesize=A4)

    if case.picture:
        # A scan: shapes, no text layer. This is what a phone photograph of a
        # CV exported to PDF actually contains.
        pdf.setFillGray(0.85)
        for row in range(24):
            pdf.rect(60, height - 90 - row * 28, 4 + (row * 37) % 430, 12, fill=1, stroke=0)
        pdf.showPage()
        pdf.save()
        return buffer.getvalue()

    if case.layout == "sidebar":
        _draw_sidebar(pdf, case.body, top=height - 60)
        pdf.showPage()
        pdf.save()
        return buffer.getvalue()

    if case.layout == "two_column":
        left, right = _split_columns(case.body)
        _draw_column(pdf, left, x=54, top=height - 60, width=230)
        _draw_column(pdf, right, x=310, top=height - 60, width=250)
        pdf.showPage()
        pdf.save()
        return buffer.getvalue()

    y = height - 60
    for line in case.body:
        # Wrapped, not truncated. A line cut off mid-word invents a skill
        # called "Sk" and then scores the reader down for finding it, which
        # measures the renderer rather than the product.
        for piece in _wrap(line, 108):
            if y < 60:
                pdf.showPage()
                y = height - 60
            if piece:
                pdf.setFont("Helvetica", 9.5)
                pdf.drawString(54, y, piece)
            y -= 14
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _wrap(line: str, limit: int) -> list[str]:
    if len(line) <= limit:
        return [line]
    pieces, current = [], ""
    for word in line.split(" "):
        if current and len(current) + 1 + len(word) > limit:
            pieces.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        pieces.append(current)
    return pieces


def _split_columns(body: tuple[str, ...]) -> tuple[list[str], list[str]]:
    if COLUMN_BREAK not in body:
        return list(body), []
    index = body.index(COLUMN_BREAK)
    return list(body[:index]), list(body[index + 1 :])


#: In a sidebar case, the label and the content that shares its baseline.
SIDEBAR_MARK = "::"
#: A line the renderer sets letter-spaced, the way a designer sets their name.
SPACED_MARK = "~~"
#: A value pushed to the right edge of the content column, as dates are.
RIGHT_MARK = ">>"


def _draw_sidebar(pdf, lines: tuple[str, ...], *, top: float) -> None:
    """A label column and a content column sharing one baseline.

    The layout that broke extraction completely: pdfminer writes the two
    columns as one line with the gutter left in as whitespace, so the section
    label ends up in front of the first line of its own section and no heading
    is ever found. A CV like this extracted to no skills, no roles and no
    education at all.
    """
    y = top
    for raw in lines:
        if not raw:
            y -= 13
            continue

        label, _, rest = raw.partition(SIDEBAR_MARK)
        if not rest:
            label, rest = "", raw

        if label:
            pdf.setFont("Helvetica-Bold", 9)
            pdf.drawString(54, y, _spaced(label))

        pdf.setFont("Helvetica", 9)
        content, _, right = rest.partition(RIGHT_MARK)
        pdf.drawString(180, y, _spaced(content.strip()))
        if right:
            pdf.drawRightString(540, y, right.strip())
        y -= 13


def _spaced(value: str) -> str:
    """Letter-spacing, as a designer's title block is actually set."""
    if not value.startswith(SPACED_MARK):
        return value
    return "   ".join(" ".join(word) for word in value[len(SPACED_MARK) :].split())


def _draw_column(pdf, lines: list[str], *, x: float, top: float, width: float) -> None:
    y = top
    for line in lines:
        if line:
            pdf.setFont("Helvetica", 9)
            pdf.drawString(x, y, line[:60])
        y -= 13


def _to_docx(case: Case) -> bytes:
    import docx

    document = docx.Document()
    if case.layout == "table":
        _docx_with_table(document, case.body)
    else:
        for line in case.body:
            if line == COLUMN_BREAK:
                continue
            if line.startswith("• "):
                document.add_paragraph(line[2:], style="List Bullet")
            else:
                document.add_paragraph(line)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


TABLE_MARK = "|"


def _docx_with_table(document, body: tuple[str, ...]) -> None:
    """Rows written `left|right` become a two-cell table row.

    Word users lay their employment history out this way constantly — dates in
    one cell, the role in the other — and it is the layout that breaks readers
    that assume a role and its dates share a line.
    """
    pending: list[tuple[str, str]] = []

    def flush() -> None:
        if not pending:
            return
        table = document.add_table(rows=0, cols=2)
        for left, right in pending:
            cells = table.add_row().cells
            cells[0].text = left
            cells[1].text = right
        pending.clear()

    for line in body:
        if TABLE_MARK in line and not line.startswith("•"):
            left, _, right = line.partition(TABLE_MARK)
            pending.append((left.strip(), right.strip()))
            continue
        flush()
        if line.startswith("• "):
            document.add_paragraph(line[2:], style="List Bullet")
        else:
            document.add_paragraph(line)
    flush()


# --- the corpus -----------------------------------------------------------


def _case(**kwargs) -> Case:
    return Case(**kwargs)


CASES: tuple[Case, ...] = (
    # 1 — the ordinary professional CV everything else is measured against.
    _case(
        slug="standard-professional",
        sector="software",
        profile="experienced",
        formats=("pdf", "docx"),
        body=(
            "Priya Sharma",
            "Senior Backend Engineer",
            "priya.sharma@example.com | +977 9841 220 118 | linkedin.com/in/priyasharma",
            "Kathmandu, Nepal",
            "",
            "SUMMARY",
            "Backend engineer with seven years building payment systems at scale.",
            "",
            "EXPERIENCE",
            "Senior Backend Engineer, Khalti — 2021 to Present",
            "• Led the migration of the settlement ledger to event sourcing.",
            "• Cut median checkout latency from 2.1s to 380ms.",
            "Backend Engineer, eSewa — 2018 - 2021",
            "• Built the merchant payout API used by 4,000 shops.",
            "",
            "EDUCATION",
            "BE Computer Engineering, Kathmandu University, 2014 - 2018",
            "",
            "SKILLS",
            "Python, Django, PostgreSQL, Redis, Celery, Docker, Kubernetes, AWS",
            "",
            "LANGUAGES",
            "Nepali, English, Hindi",
        ),
        truth=Truth(
            name="Priya Sharma",
            emails=("priya.sharma@example.com",),
            phones=("+977 9841 220 118",),
            links=("linkedin.com/in/priyasharma",),
            headline="Senior Backend Engineer",
            has_summary=True,
            skills=(
                "Python",
                "Django",
                "PostgreSQL",
                "Redis",
                "Celery",
                "Docker",
                "Kubernetes",
                "AWS",
            ),
            experience=(
                Role("Senior Backend Engineer", "Khalti", "2021", "", True),
                Role("Backend Engineer", "eSewa", "2018", "2021"),
            ),
            education=(Study("BE Computer Engineering", "Kathmandu University"),),
            languages=("Nepali", "English", "Hindi"),
        ),
    ),
    # 2 — no employment at all. The parser must not invent any.
    _case(
        slug="undergraduate-no-experience",
        sector="student",
        profile="undergraduate",
        body=(
            "Sabin Bhandari",
            "sabin.bhandari@example.com",
            "9812345678",
            "",
            "OBJECTIVE",
            "Final-year computing student looking for a first role in software.",
            "",
            "EDUCATION",
            "BSc (Hons) Computing, Islington College, 2022 - 2026",
            "",
            "PROJECTS",
            "Bus Tracker — a live map of Kathmandu valley routes, built with Flutter.",
            "Notes API — a REST API with token authentication, written in Django.",
            "",
            "SKILLS",
            "Python, Java, Git, SQL",
        ),
        truth=Truth(
            name="Sabin Bhandari",
            emails=("sabin.bhandari@example.com",),
            phones=("9812345678",),
            has_summary=True,
            skills=("Python", "Java", "Git", "SQL"),
            experience=(),
            education=(Study("BSc (Hons) Computing", "Islington College"),),
            projects=("Bus Tracker", "Notes API"),
        ),
        notes="No employment. Any extracted role is a fabrication.",
    ),
    # 3 — a graduate whose only work is internships, under a heading the
    #     vocabulary has to know.
    _case(
        slug="graduate-internships",
        sector="finance",
        profile="graduate",
        body=(
            "Anisha Thapa",
            "Finance Graduate",
            "anisha.thapa@example.com | 01-4412233",
            "",
            "PROFILE",
            "BBA graduate seeking an analyst role in corporate finance.",
            "",
            "INTERNSHIPS",
            "Finance Intern, NIC Asia Bank — Jun 2025 to Sep 2025",
            "• Reconciled daily branch cash positions across 12 branches.",
            "Audit Intern, PKF TR Upadhya — Jan 2025 - Mar 2025",
            "• Assisted on three statutory audits of manufacturing clients.",
            "",
            "ACADEMIC QUALIFICATIONS",
            "BBA Finance, Kathmandu University School of Management, 2021 - 2025",
            "",
            "CERTIFICATIONS",
            "CFA Level I — passed February 2026",
            "Bloomberg Market Concepts",
            "",
            "SKILLS",
            "Financial modelling, Excel, Power BI, IFRS, Variance analysis",
        ),
        truth=Truth(
            name="Anisha Thapa",
            emails=("anisha.thapa@example.com",),
            phones=("01-4412233",),
            headline="Finance Graduate",
            has_summary=True,
            skills=(
                "Financial modelling",
                "Excel",
                "Power BI",
                "IFRS",
                "Variance analysis",
            ),
            experience=(
                Role("Finance Intern", "NIC Asia Bank", "Jun 2025", "Sep 2025"),
                Role("Audit Intern", "PKF TR Upadhya", "Jan 2025", "Mar 2025"),
            ),
            education=(Study("BBA Finance", "Kathmandu University School of Management"),),
            certifications=("CFA Level I", "Bloomberg Market Concepts"),
        ),
        notes="'Internships' is the experience heading. Missing it costs the whole history.",
    ),
    # 4 — a career changer: the transferable history is the point.
    _case(
        slug="career-changer",
        sector="hospitality-to-software",
        profile="career-changer",
        body=(
            "Rabin Karki",
            "rabin.karki@example.com | +977 9803 114 552 | github.com/rabink",
            "",
            "PERSONAL STATEMENT",
            "Eight years running restaurant floors, retrained in software in 2025.",
            "",
            "WORK HISTORY",
            "Restaurant Manager, Hotel Yak & Yeti — 2019 to 2025",
            "• Managed a floor team of 24 across two outlets.",
            "• Cut food waste 31% by rebuilding the prep forecast.",
            "Assistant Manager, Fire and Ice — 2017-2019",
            "",
            "EDUCATION",
            "Diploma in Hotel Management, NATHM, 2014 - 2016",
            "",
            "COURSES",
            "Full-stack Web Development, Broadway Infosys, 2025",
            "",
            "KEY SKILLS",
            "Team leadership, Rostering, P&L ownership, JavaScript, React, Node.js",
        ),
        truth=Truth(
            name="Rabin Karki",
            emails=("rabin.karki@example.com",),
            phones=("+977 9803 114 552",),
            links=("github.com/rabink",),
            has_summary=True,
            skills=(
                "Team leadership",
                "Rostering",
                "P&L ownership",
                "JavaScript",
                "React",
                "Node.js",
            ),
            experience=(
                Role("Restaurant Manager", "Hotel Yak & Yeti", "2019", "2025"),
                Role("Assistant Manager", "Fire and Ice", "2017", "2019"),
            ),
            education=(Study("Diploma in Hotel Management", "NATHM"),),
            # The retraining course is the whole point of a career-changer's
            # CV, and "Courses" is a certifications heading. The truth omitted
            # it until the scorer started reading the structured field and
            # asked what this was.
            certifications=("Full-stack Web Development",),
        ),
    ),
    # 5 — healthcare, and a CV that writes its dates on their own line.
    _case(
        slug="healthcare-nurse",
        sector="healthcare",
        profile="experienced",
        body=(
            "Sunita Gurung",
            "Staff Nurse",
            "sunita.gurung@example.com",
            "+977 9856 442 190",
            "",
            "PROFESSIONAL EXPERIENCE",
            "Staff Nurse — Tribhuvan University Teaching Hospital",
            "2020 — Present",
            "• Charge nurse on a 32-bed surgical ward.",
            "• Precepted 14 student nurses through their final placement.",
            "Staff Nurse — Patan Hospital",
            "2017 — 2020",
            "",
            "EDUCATION",
            "BSc Nursing, Institute of Medicine, 2013 - 2017",
            "",
            "LICENCES",
            "Nepal Nursing Council registration 24119",
            "",
            "SKILLS",
            "Post-operative care, IV cannulation, Triage, Wound management",
        ),
        truth=Truth(
            name="Sunita Gurung",
            emails=("sunita.gurung@example.com",),
            phones=("+977 9856 442 190",),
            headline="Staff Nurse",
            skills=("Post-operative care", "IV cannulation", "Triage", "Wound management"),
            experience=(
                Role("Staff Nurse", "Tribhuvan University Teaching Hospital", "2020", "", True),
                Role("Staff Nurse", "Patan Hospital", "2017", "2020"),
            ),
            education=(Study("BSc Nursing", "Institute of Medicine"),),
            certifications=("Nepal Nursing Council registration 24119",),
        ),
        notes="Dates on their own line, under the role. A very common Word layout.",
    ),
    # 6 — a two-column designer's CV: text extracts out of reading order.
    _case(
        slug="designer-two-column",
        sector="design",
        profile="experienced",
        layout="two_column",
        body=(
            "Manisha Tuladhar",
            "Product Designer",
            "manisha@example.com",
            "+977 9808 771 234",
            "manishatuladhar.com/work",
            "",
            "SKILLS",
            "Figma",
            "Design systems",
            "Prototyping",
            "User research",
            "Accessibility",
            COLUMN_BREAK,
            "EXPERIENCE",
            "Product Designer, Foodmandu",
            "2022 - Present",
            "Owned the ordering flow end to end.",
            "UI Designer, Yomari",
            "2019 - 2022",
            "",
            "EDUCATION",
            "BA Design, Kathmandu University, 2015 - 2019",
        ),
        truth=Truth(
            name="Manisha Tuladhar",
            emails=("manisha@example.com",),
            phones=("+977 9808 771 234",),
            links=("manishatuladhar.com/work",),
            headline="Product Designer",
            skills=(
                "Figma",
                "Design systems",
                "Prototyping",
                "User research",
                "Accessibility",
            ),
            experience=(
                Role("Product Designer", "Foodmandu", "2022", "", True),
                Role("UI Designer", "Yomari", "2019", "2022"),
            ),
            education=(Study("BA Design", "Kathmandu University"),),
        ),
        notes="The hardest common layout. Honest partial credit is the goal, not 100%.",
    ),
    # 7 — a Word CV whose employment history is a table.
    _case(
        slug="marketing-table-docx",
        sector="marketing",
        profile="experienced",
        layout="table",
        formats=("docx",),
        body=(
            "Sita Rai",
            "Marketing Manager",
            "sita.rai@example.com | 9841 000 221",
            "",
            "EXPERIENCE",
            "Marketing Manager, Daraz Nepal|2021 - Present",
            "• Ran the festive campaign that added 180,000 first orders.",
            "Marketing Executive, Sastodeal|2018 - 2021",
            "",
            "EDUCATION",
            "MBA Marketing, Ace Institute of Management|2016 - 2018",
            "",
            "SKILLS",
            "SEO, Google Ads, Meta Ads, HubSpot, Copywriting, Analytics",
        ),
        truth=Truth(
            name="Sita Rai",
            emails=("sita.rai@example.com",),
            phones=("9841 000 221",),
            headline="Marketing Manager",
            skills=("SEO", "Google Ads", "Meta Ads", "HubSpot", "Copywriting", "Analytics"),
            experience=(
                Role("Marketing Manager", "Daraz Nepal", "2021", "", True),
                Role("Marketing Executive", "Sastodeal", "2018", "2021"),
            ),
            education=(Study("MBA Marketing", "Ace Institute of Management"),),
        ),
    ),
    # 8 — an academic CV: publications and appointments, not employment.
    _case(
        slug="academic-researcher",
        sector="research",
        profile="academic",
        body=(
            "Dr Bimala Adhikari",
            "bimala.adhikari@example.edu.np",
            "orcid.org/0000-0002-1825-0097",
            "",
            "ACADEMIC APPOINTMENTS",
            "Assistant Professor, Tribhuvan University — 2021 to present",
            "Postdoctoral Researcher, University of Bergen — 2018 - 2021",
            "",
            "EDUCATION",
            "PhD Glaciology, University of Bergen, 2014 - 2018",
            "MSc Environmental Science, Tribhuvan University, 2011 - 2013",
            "",
            "PUBLICATIONS",
            "Adhikari B. et al. (2024) Debris-covered glacier retreat in the Langtang.",
            "Adhikari B., Shrestha R. (2022) Seasonal mass balance of Yala Glacier.",
            "",
            "AWARDS",
            "Nepal Academy of Science Young Scientist Award, 2023",
            "",
            "SKILLS",
            "Remote sensing, GIS, Python, R, Field survey",
        ),
        truth=Truth(
            name="Dr Bimala Adhikari",
            emails=("bimala.adhikari@example.edu.np",),
            links=("orcid.org/0000-0002-1825-0097",),
            skills=("Remote sensing", "GIS", "Python", "R", "Field survey"),
            experience=(
                Role("Assistant Professor", "Tribhuvan University", "2021", "", True),
                Role("Postdoctoral Researcher", "University of Bergen", "2018", "2021"),
            ),
            education=(
                Study("PhD Glaciology", "University of Bergen"),
                Study("MSc Environmental Science", "Tribhuvan University"),
            ),
            awards=("Nepal Academy of Science Young Scientist Award",),
        ),
        notes="'Academic appointments' is employment. 'Publications' is not a skill list.",
    ),
    # 9 — the soft-skill CV. Everything here is genuinely in a skills section;
    #     the failure to avoid is the furniture around it.
    _case(
        slug="weak-soft-skills",
        sector="administration",
        profile="early-career",
        body=(
            "Kiran Shrestha",
            "kiran.shrestha@example.com",
            "9841122334",
            "",
            "CAREER OBJECTIVE",
            "To obtain a challenging position in a reputed organisation.",
            "",
            "EXPERIENCE",
            "Office Assistant, Himalayan Traders — 2023 - 2025",
            "",
            "EDUCATION",
            "Bachelor of Business Studies, Shanker Dev Campus, 2019 - 2023",
            "",
            "SKILLS",
            "MS Word, MS Excel, Email, Internet, Typing",
            "",
            "REFERENCE",
            "Available upon request",
        ),
        truth=Truth(
            name="Kiran Shrestha",
            emails=("kiran.shrestha@example.com",),
            phones=("9841122334",),
            has_summary=True,
            skills=("MS Word", "MS Excel", "Email", "Internet", "Typing"),
            experience=(Role("Office Assistant", "Himalayan Traders", "2023", "2025"),),
            education=(Study("Bachelor of Business Studies", "Shanker Dev Campus"),),
            never_skills=("REFERENCE", "Available upon request", "Reference"),
        ),
        notes="A singular 'REFERENCE' heading. Unknown headings leak into the section above.",
    ),
    # 10 — a scan. The only correct answer is to say it is a picture.
    _case(
        slug="scanned-picture",
        sector="any",
        profile="any",
        picture=True,
        body=(),
        truth=Truth(is_picture=True),
    ),
    # 11 — education across several lines, the way most Word CVs write it.
    _case(
        slug="education-multiline",
        sector="education",
        profile="experienced",
        body=(
            "Deepak Poudel",
            "Secondary School Teacher",
            "deepak.poudel@example.com | 9845 667 788",
            "",
            "EXPERIENCE",
            "Mathematics Teacher, Budhanilkantha School — 2016 to Present",
            "• Raised SEE mathematics pass rates from 78% to 94% over four years.",
            "",
            "EDUCATION",
            "Master of Education (Mathematics)",
            "Tribhuvan University",
            "2012 - 2014",
            "Bachelor of Science (Mathematics)",
            "Amrit Science Campus",
            "2008 - 2012",
            "",
            "SKILLS",
            "Curriculum design, Classroom management, Assessment, Moodle",
        ),
        truth=Truth(
            name="Deepak Poudel",
            emails=("deepak.poudel@example.com",),
            phones=("9845 667 788",),
            headline="Secondary School Teacher",
            skills=("Curriculum design", "Classroom management", "Assessment", "Moodle"),
            experience=(Role("Mathematics Teacher", "Budhanilkantha School", "2016", "", True),),
            education=(
                Study("Master of Education (Mathematics)", "Tribhuvan University"),
                Study("Bachelor of Science (Mathematics)", "Amrit Science Campus"),
            ),
        ),
        notes="Three lines are one qualification. Reading each line as a degree triples them.",
    ),
    # 12 — Unicode throughout, including the name.
    _case(
        slug="unicode-content",
        sector="engineering",
        profile="experienced",
        formats=("pdf", "docx"),
        body=(
            "Zoë Björk-Nørgaard",
            "Structural Engineer",
            "zoe.bjork@example.com | +45 20 11 44 55",
            "",
            "EXPERIENCE",
            "Structural Engineer, Rambøll — 2019 to Present",
            "• Designed façade connections for the Nørreport canopy.",
            "Graduate Engineer, COWI — 2017 – 2019",
            "",
            "EDUCATION",
            "MSc Civil Engineering, Danmarks Tekniske Universitet, 2012 - 2017",
            "",
            "SKILLS",
            "Eurocode 3, Robot Structural Analysis, Revit, Tekla, Autodesk",
            "",
            "LANGUAGES",
            "Dansk, English, Deutsch",
        ),
        truth=Truth(
            name="Zoë Björk-Nørgaard",
            emails=("zoe.bjork@example.com",),
            phones=("+45 20 11 44 55",),
            headline="Structural Engineer",
            skills=(
                "Eurocode 3",
                "Robot Structural Analysis",
                "Revit",
                "Tekla",
                "Autodesk",
            ),
            experience=(
                Role("Structural Engineer", "Rambøll", "2019", "", True),
                Role("Graduate Engineer", "COWI", "2017", "2019"),
            ),
            education=(Study("MSc Civil Engineering", "Danmarks Tekniske Universitet"),),
            languages=("Dansk", "English", "Deutsch"),
        ),
    ),
    # 13 — no contact details at all. Extraction must report nothing, not guess.
    _case(
        slug="no-contact-details",
        sector="sales",
        profile="experienced",
        body=(
            "Bikash Maharjan",
            "Regional Sales Manager",
            "",
            "EXPERIENCE",
            "Regional Sales Manager, Chaudhary Group — 2020 to Present",
            "• Grew the mid-hills distributor network from 40 to 96 outlets.",
            "Sales Officer, Unilever Nepal — 2016 - 2020",
            "",
            "EDUCATION",
            "MBA, Pokhara University, 2014 - 2016",
            "",
            "SKILLS",
            "Distributor management, Territory planning, Negotiation, Forecasting",
        ),
        truth=Truth(
            name="Bikash Maharjan",
            headline="Regional Sales Manager",
            skills=(
                "Distributor management",
                "Territory planning",
                "Negotiation",
                "Forecasting",
            ),
            experience=(
                Role("Regional Sales Manager", "Chaudhary Group", "2020", "", True),
                Role("Sales Officer", "Unilever Nepal", "2016", "2020"),
            ),
            education=(Study("MBA", "Pokhara University"),),
        ),
        notes="No email, no phone, no link — and it is still a document, not a picture.",
    ),
    # 14 — one long bullet wrapped across two lines by the renderer.
    _case(
        slug="wrapped-bullets",
        sector="software",
        profile="experienced",
        body=(
            "Saugat Khadka",
            "Site Reliability Engineer",
            "saugat.khadka@example.com | 9803 556 001",
            "",
            "EXPERIENCE",
            "Site Reliability Engineer, Leapfrog — 2020 to Present",
            "• Rebuilt the alerting pipeline so that paging volume fell from roughly",
            "two hundred a week to under twenty, and the on-call rota stopped",
            "burning people out.",
            "• Ran the migration from EC2 to EKS with no customer-visible downtime.",
            "",
            "EDUCATION",
            "BE Computer Engineering, Pulchowk Campus, 2013 - 2017",
            "",
            "SKILLS",
            "Kubernetes, Terraform, Prometheus, Grafana, Go, CI/CD, A/B testing",
        ),
        truth=Truth(
            name="Saugat Khadka",
            emails=("saugat.khadka@example.com",),
            phones=("9803 556 001",),
            headline="Site Reliability Engineer",
            skills=(
                "Kubernetes",
                "Terraform",
                "Prometheus",
                "Grafana",
                "Go",
                "CI/CD",
                "A/B testing",
            ),
            experience=(Role("Site Reliability Engineer", "Leapfrog", "2020", "", True),),
            education=(Study("BE Computer Engineering", "Pulchowk Campus"),),
            never_skills=("CI", "CD", "A", "B testing"),
        ),
        notes="Two bullets, not four. And 'CI/CD' is one skill.",
    ),
    # 15 — a volunteer-only history under an unusual heading.
    _case(
        slug="volunteer-only",
        sector="social",
        profile="early-career",
        body=(
            "Nabin Chaudhary",
            "nabin.chaudhary@example.com",
            "9807 223 119",
            "",
            "ABOUT ME",
            "Public health graduate, two years of field volunteering in Province 2.",
            "",
            "VOLUNTEER EXPERIENCE",
            "Field Volunteer, Nepal Red Cross Society — 2024 to Present",
            "• Ran maternal health outreach in 9 village wards.",
            "Camp Volunteer, Rotaract Birgunj — 2023 - 2024",
            "",
            "EDUCATION",
            "BPH Public Health, Purbanchal University, 2020 - 2024",
            "",
            "SKILLS",
            "Community outreach, Data collection, KoboToolbox, Report writing",
            "",
            "LANGUAGES",
            "Nepali, Bhojpuri, English",
        ),
        truth=Truth(
            name="Nabin Chaudhary",
            emails=("nabin.chaudhary@example.com",),
            phones=("9807 223 119",),
            has_summary=True,
            skills=("Community outreach", "Data collection", "KoboToolbox", "Report writing"),
            experience=(
                Role("Field Volunteer", "Nepal Red Cross Society", "2024", "", True),
                Role("Camp Volunteer", "Rotaract Birgunj", "2023", "2024"),
            ),
            education=(Study("BPH Public Health", "Purbanchal University"),),
            languages=("Nepali", "Bhojpuri", "English"),
        ),
    ),
    # 16 — headings written in the mixed case and with the trailing rules that
    #      Word's built-in styles produce.
    _case(
        slug="decorated-headings",
        sector="engineering",
        profile="experienced",
        body=(
            "Ramesh Basnet",
            "Mechanical Engineer",
            "ramesh.basnet@example.com | +977 9841 776 002",
            "",
            "Work Experience:",
            "Mechanical Engineer, Hulas Steel — 2019 to Present",
            "• Redesigned the roll-forming line, lifting throughput 22%.",
            "",
            "Educational Qualification:",
            "BE Mechanical, IOE Thapathali Campus, 2014 - 2018",
            "",
            "Technical Skills:",
            "AutoCAD, SolidWorks, ANSYS, GD&T, Lean manufacturing",
        ),
        truth=Truth(
            name="Ramesh Basnet",
            emails=("ramesh.basnet@example.com",),
            phones=("+977 9841 776 002",),
            headline="Mechanical Engineer",
            skills=("AutoCAD", "SolidWorks", "ANSYS", "GD&T", "Lean manufacturing"),
            experience=(Role("Mechanical Engineer", "Hulas Steel", "2019", "", True),),
            education=(Study("BE Mechanical", "IOE Thapathali Campus"),),
        ),
    ),
    # 17 — a long CV that runs to three pages.
    _case(
        slug="long-multipage",
        sector="management",
        profile="experienced",
        body=(
            "Gopal Shrestha",
            "Chief Operating Officer",
            "gopal.shrestha@example.com | +977 9851 004 220",
            "",
            "SUMMARY",
            "Twenty-two years in manufacturing operations across three countries.",
            "",
            "EXPERIENCE",
            "Chief Operating Officer, Surya Nepal — 2018 to Present",
            *[f"• Operational initiative number {n} delivered on schedule." for n in range(1, 13)],
            "Head of Operations, Dabur Nepal — 2012 - 2018",
            *[f"• Plant programme number {n} completed under budget." for n in range(1, 13)],
            "Plant Manager, Bottlers Nepal — 2006 - 2012",
            *[f"• Line improvement number {n} shipped." for n in range(1, 13)],
            "",
            "EDUCATION",
            "MBA, Kathmandu University School of Management, 2004 - 2006",
            "BE Industrial Engineering, Pulchowk Campus, 1999 - 2003",
            "",
            "SKILLS",
            "Lean, Six Sigma, S&OP, Capex planning, ERP, Supply chain",
        ),
        truth=Truth(
            name="Gopal Shrestha",
            emails=("gopal.shrestha@example.com",),
            phones=("+977 9851 004 220",),
            headline="Chief Operating Officer",
            has_summary=True,
            skills=("Lean", "Six Sigma", "S&OP", "Capex planning", "ERP", "Supply chain"),
            experience=(
                Role("Chief Operating Officer", "Surya Nepal", "2018", "", True),
                Role("Head of Operations", "Dabur Nepal", "2012", "2018"),
                Role("Plant Manager", "Bottlers Nepal", "2006", "2012"),
            ),
            education=(
                Study("MBA", "Kathmandu University School of Management"),
                Study("BE Industrial Engineering", "Pulchowk Campus"),
            ),
        ),
        notes="Three pages. Page furniture must not become content.",
    ),
    # 18 — a skills line that writes its examples in brackets.
    _case(
        slug="bracketed-skills",
        sector="software",
        profile="experienced",
        body=(
            "Aayush Pandey",
            "Platform Engineer",
            "aayush.pandey@example.com | 9861 220 004",
            "",
            "EXPERIENCE",
            "Platform Engineer, Fusemachines — 2021 to Present",
            "• Owned the internal developer platform used by 60 engineers.",
            "",
            "EDUCATION",
            "BSc CSIT, Tribhuvan University, 2016 - 2020",
            "",
            "CORE COMPETENCIES",
            "observability (Prometheus, Grafana, OpenTelemetry), Go / Rust, CI/CD",
        ),
        truth=Truth(
            name="Aayush Pandey",
            emails=("aayush.pandey@example.com",),
            phones=("9861 220 004",),
            headline="Platform Engineer",
            skills=(
                "observability (Prometheus, Grafana, OpenTelemetry)",
                "Go",
                "Rust",
                "CI/CD",
            ),
            experience=(Role("Platform Engineer", "Fusemachines", "2021", "", True),),
            education=(Study("BSc CSIT", "Tribhuvan University"),),
            never_skills=("observability (Prometheus", "OpenTelemetry)"),
        ),
    ),
    # 19 — an empty document. Not a picture; simply blank.
    _case(
        slug="near-empty",
        sector="any",
        profile="any",
        body=("Untitled", "", "", ""),
        truth=Truth(),
        notes="Nothing to find. Reporting anything is a fabrication.",
    ),
    # 20 — hospitality, with a role line that puts the org first.
    _case(
        slug="hospitality-org-first",
        sector="hospitality",
        profile="experienced",
        body=(
            "Pemba Sherpa",
            "pemba.sherpa@example.com | 9842 331 776",
            "",
            "EMPLOYMENT HISTORY",
            "Hyatt Regency Kathmandu — Front Office Supervisor — 2021 to Present",
            "• Led a front desk team of 11 across three shifts.",
            "Hotel Shanker — Guest Relations Officer — 2018 - 2021",
            "",
            "EDUCATION",
            "Bachelor in Hotel Management, NATHM, 2014 - 2018",
            "",
            "SKILLS",
            "Opera PMS, Guest relations, Upselling, Shift rostering",
            "",
            "LANGUAGES",
            "Nepali, English, Japanese",
        ),
        truth=Truth(
            name="Pemba Sherpa",
            emails=("pemba.sherpa@example.com",),
            phones=("9842 331 776",),
            skills=("Opera PMS", "Guest relations", "Upselling", "Shift rostering"),
            experience=(
                Role("Hyatt Regency Kathmandu", "Front Office Supervisor", "2021", "", True),
                Role("Hotel Shanker", "Guest Relations Officer", "2018", "2021"),
            ),
            education=(Study("Bachelor in Hotel Management", "NATHM"),),
            languages=("Nepali", "English", "Japanese"),
        ),
        notes="Org before title. Order is genuinely ambiguous; scored on the pair, not the slot.",
    ),
    # 21 — one skill per line, no separators at all.
    _case(
        slug="skills-one-per-line",
        sector="software",
        profile="early-career",
        formats=("pdf", "docx"),
        body=(
            "Sneha Maharjan",
            "Junior Data Analyst",
            "sneha.maharjan@example.com | 9808 442 116",
            "",
            "EXPERIENCE",
            "Junior Data Analyst, CloudFactory — 2024 to Present",
            "• Built the weekly throughput dashboard the operations team runs on.",
            "",
            "EDUCATION",
            "BSc Statistics, Tribhuvan University, 2020 - 2024",
            "",
            "TOOLS",
            "SQL",
            "Power BI",
            "Python",
            "Excel",
        ),
        truth=Truth(
            name="Sneha Maharjan",
            emails=("sneha.maharjan@example.com",),
            phones=("9808 442 116",),
            headline="Junior Data Analyst",
            skills=("SQL", "Power BI", "Python", "Excel"),
            experience=(Role("Junior Data Analyst", "CloudFactory", "2024", "", True),),
            education=(Study("BSc Statistics", "Tribhuvan University"),),
        ),
    ),
    # 22 — a CV written as a form, the way many first CVs are.
    _case(
        slug="form-style",
        sector="administration",
        profile="early-career",
        body=(
            "CURRICULUM VITAE",
            "Name - Sunita Karki",
            "Address - Lalitpur, Nepal",
            "Phone - 9841 553 220",
            "Email - sunita.karki@example.com",
            "",
            "EDUCATION",
            "+2 Management, Trinity International College, 2018 - 2020",
            "Bachelor of Business Administration, Apex College, 2020 - 2024",
            "",
            "WORK EXPERIENCE",
            "Admin Assistant, Vianet Communications — 2024 - Present",
            "",
            "SKILLS",
            "Data entry, Filing, MS Office, Reception",
        ),
        truth=Truth(
            name="Sunita Karki",
            emails=("sunita.karki@example.com",),
            phones=("9841 553 220",),
            skills=("Data entry", "Filing", "MS Office", "Reception"),
            experience=(Role("Admin Assistant", "Vianet Communications", "2024", "", True),),
            education=(
                Study("+2 Management", "Trinity International College"),
                Study("Bachelor of Business Administration", "Apex College"),
            ),
            never_skills=("Name - Sunita Karki", "CURRICULUM VITAE"),
        ),
        notes="'Name - Sunita Karki' is the name, written as a form field.",
    ),
)


HARD_CASES: tuple[Case, ...] = (
    # 23 — the layout that extracted to nothing at all: a label column beside a
    #      content column, and a letter-spaced title block above both.
    _case(
        slug="creative-sidebar",
        sector="design",
        profile="experienced",
        layout="sidebar",
        body=(
            "~~Anjali Karki",
            "~~Product Designer",
            "hello@anjalikarki.design · anjalikarki.design/work · Pokhara",
            "",
            "ABOUT::I design things people use when they are in a hurry and",
            "slightly annoyed. Six years, mostly in logistics.",
            "",
            "WORK::Lead Product Designer>>2022 - now",
            "Pathao Nepal",
            "Owned the rider app end to end. Cut order-acceptance",
            "time from 22s to 9s.",
            "",
            "Product Designer>>2019 - 2022",
            "Foodmandu",
            "Ran the first usability sessions the company had done.",
            "",
            "TOOLS::Figma · Framer · Principle · Maze · Dovetail",
            "",
            "SCHOOL::BA Fine Arts, Kathmandu University>>2015 - 2019",
        ),
        truth=Truth(
            name="Anjali Karki",
            emails=("hello@anjalikarki.design",),
            links=("anjalikarki.design/work",),
            headline="Product Designer",
            has_summary=True,
            skills=("Figma", "Framer", "Principle", "Maze", "Dovetail"),
            experience=(
                Role("Lead Product Designer", "Pathao Nepal", "2022", "", True),
                Role("Product Designer", "Foodmandu", "2019", "2022"),
            ),
            education=(Study("BA Fine Arts", "Kathmandu University"),),
            never_skills=("WORK", "TOOLS", "SCHOOL", "Pathao Nepal", "Foodmandu"),
        ),
        notes="Sidebar labels, right-aligned dates, employer under the title, spaced name.",
    ),
    # 24 — month-and-year dates in every spelling a CV uses for them.
    _case(
        slug="mixed-date-formats",
        sector="software",
        profile="experienced",
        body=(
            "Nishan Gurung",
            "Data Engineer",
            "nishan.gurung@example.com | 9841 776 220",
            "",
            "EXPERIENCE",
            "Data Engineer, Cedar Gate — March 2022 – Present",
            "• Owned the claims pipeline.",
            "Analytics Engineer, Verisk — Jan. 2020 to Feb 2022",
            "• Built the dbt layer the analysts use.",
            "Junior Analyst, LIS Nepal — 2018-2020",
            "",
            "EDUCATION",
            "BSc CSIT, Tribhuvan University, 2014 – 2018",
            "",
            "SKILLS",
            "Python, dbt, Snowflake, Airflow, SQL",
        ),
        truth=Truth(
            name="Nishan Gurung",
            emails=("nishan.gurung@example.com",),
            phones=("9841 776 220",),
            headline="Data Engineer",
            skills=("Python", "dbt", "Snowflake", "Airflow", "SQL"),
            experience=(
                Role("Data Engineer", "Cedar Gate", "March 2022", "", True),
                Role("Analytics Engineer", "Verisk", "Jan. 2020", "Feb 2022"),
                Role("Junior Analyst", "LIS Nepal", "2018", "2020"),
            ),
            education=(Study("BSc CSIT", "Tribhuvan University"),),
        ),
        notes="Three date spellings, including one with no spaces around the dash.",
    ),
    # 25 — an address full of digits, next to a real phone number.
    _case(
        slug="address-digits",
        sector="administration",
        profile="experienced",
        body=(
            "Renu Shakya",
            "Office Manager",
            "Flat 4B, Block 27, Ward No. 11, Lalitpur 44700",
            "renu.shakya@example.com | 977-1-5521490",
            "",
            "EXPERIENCE",
            "Office Manager, Himalaya Airlines — 2019 to Present",
            "• Ran facilities and procurement for a 90-desk office.",
            "",
            "EDUCATION",
            "BBS, Patan Multiple Campus, 2012 - 2016",
            "",
            "SKILLS",
            "Procurement, Vendor management, Scheduling, MS Office",
        ),
        truth=Truth(
            name="Renu Shakya",
            emails=("renu.shakya@example.com",),
            phones=("977-1-5521490",),
            headline="Office Manager",
            skills=("Procurement", "Vendor management", "Scheduling", "MS Office"),
            experience=(Role("Office Manager", "Himalaya Airlines", "2019", "", True),),
            education=(Study("BBS", "Patan Multiple Campus"),),
        ),
        notes="The postcode and the ward number are not telephone numbers.",
    ),
    # 26 — a Word CV whose whole body is one three-column table.
    _case(
        slug="three-column-table-docx",
        sector="education",
        profile="experienced",
        layout="table",
        formats=("docx",),
        body=(
            "Laxmi Bhattarai",
            "Primary School Teacher",
            "laxmi.bhattarai@example.com | 9807 221 554",
            "",
            "WORK EXPERIENCE",
            "Primary School Teacher|Rato Bangala School|2018 - Present",
            "Teaching Assistant|Ullens School|2015 - 2018",
            "",
            "EDUCATION",
            "MEd Primary Education|Kathmandu University|2013 - 2015",
            "",
            "SKILLS",
            "Lesson planning, Differentiated instruction, Phonics, Assessment",
        ),
        truth=Truth(
            name="Laxmi Bhattarai",
            emails=("laxmi.bhattarai@example.com",),
            phones=("9807 221 554",),
            headline="Primary School Teacher",
            skills=(
                "Lesson planning",
                "Differentiated instruction",
                "Phonics",
                "Assessment",
            ),
            experience=(
                Role("Primary School Teacher", "Rato Bangala School", "2018", "", True),
                Role("Teaching Assistant", "Ullens School", "2015", "2018"),
            ),
            education=(Study("MEd Primary Education", "Kathmandu University"),),
        ),
    ),
    # 27 — a CV that never says "experience": the roles sit under the employer.
    _case(
        slug="employer-led-history",
        sector="finance",
        profile="experienced",
        body=(
            "Suraj Tamang",
            "Credit Analyst",
            "suraj.tamang@example.com | +977 9841 009 771",
            "",
            "CAREER",
            "Nabil Bank",
            "Credit Analyst — 2021 to Present",
            "• Underwrote SME facilities up to NPR 50 million.",
            "Standard Chartered Nepal",
            "Credit Officer — 2018 - 2021",
            "",
            "QUALIFICATIONS",
            "MBA Finance, Kathmandu University, 2016 - 2018",
            "",
            "KEY SKILLS",
            "Credit risk, Financial statement analysis, Basel III, Excel",
        ),
        truth=Truth(
            name="Suraj Tamang",
            emails=("suraj.tamang@example.com",),
            phones=("+977 9841 009 771",),
            headline="Credit Analyst",
            skills=(
                "Credit risk",
                "Financial statement analysis",
                "Basel III",
                "Excel",
            ),
            experience=(
                Role("Credit Analyst", "", "2021", "", True),
                Role("Credit Officer", "", "2018", "2021"),
            ),
            education=(Study("MBA Finance", "Kathmandu University"),),
        ),
        notes="Employer above the dated role line. Scored on the role, not the pairing.",
    ),
    # 28 — a school leaver. No degree, no employment, and that is the CV.
    _case(
        slug="school-leaver",
        sector="retail",
        profile="school-leaver",
        body=(
            "Aashish Thapa",
            "aashish.thapa@example.com",
            "9861 442 003",
            "",
            "ABOUT ME",
            "School leaver looking for a first part-time role in retail.",
            "",
            "EDUCATION",
            "SEE, Shree Bhagwati Secondary School, 2024",
            "",
            "ACTIVITIES",
            "School football team captain, 2023",
            "",
            "SKILLS",
            "Customer service, Cash handling, Nepali, English",
        ),
        truth=Truth(
            name="Aashish Thapa",
            emails=("aashish.thapa@example.com",),
            phones=("9861 442 003",),
            has_summary=True,
            skills=("Customer service", "Cash handling", "Nepali", "English"),
            experience=(),
            education=(Study("SEE", "Shree Bhagwati Secondary School"),),
            never_skills=("School football team captain", "ACTIVITIES"),
        ),
        notes="No employment and no degree. Inventing either is the failure.",
    ),
    # 29 — a keyword-stuffed CV: eighty terms, and the reader must not drown
    #      the candidate in them.
    _case(
        slug="keyword-stuffed",
        sector="software",
        profile="experienced",
        body=(
            "Prabin Rai",
            "Full Stack Developer",
            "prabin.rai@example.com | 9803 117 662",
            "",
            "EXPERIENCE",
            "Full Stack Developer, Leapfrog — 2020 to Present",
            "• Shipped the customer portal.",
            "",
            "EDUCATION",
            "BE Software, Pulchowk Campus, 2015 - 2019",
            "",
            "TECHNICAL SKILLS",
            ", ".join(f"Skill{n}" for n in range(1, 41)),
        ),
        truth=Truth(
            name="Prabin Rai",
            emails=("prabin.rai@example.com",),
            phones=("9803 117 662",),
            headline="Full Stack Developer",
            skills=tuple(f"Skill{n}" for n in range(1, 41)),
            experience=(Role("Full Stack Developer", "Leapfrog", "2020", "", True),),
            education=(Study("BE Software", "Pulchowk Campus"),),
        ),
        notes="Extraction reads all forty. Capping them is the proposal layer's job.",
    ),
)

EVIDENCE_CASES: tuple[Case, ...] = (
    # 30 — the sections a CV built on anything but employment depends on, and
    #      the numbered, wrapping list an academic writes them in.
    _case(
        slug="academic-numbered-publications",
        sector="research",
        profile="academic",
        body=(
            "Dr Prakash Adhikari",
            "prakash.adhikari@example.edu.np",
            "",
            "ACADEMIC APPOINTMENTS",
            "Associate Professor, Kathmandu University — 2019 to present",
            "",
            "EDUCATION",
            "PhD Hydrology, University of Tokyo, 2011 - 2015",
            "",
            "SELECTED PUBLICATIONS",
            "1. Adhikari, P. and Tanaka, K. (2011). Snowmelt contribution to dry-season",
            "discharge in the Koshi basin. Journal of Himalayan Hydrology, 11(2), 107-119.",
            "2. Adhikari, P. and Shrestha, R. (2014). Debris cover and glacier retreat in the",
            "Langtang valley. Cryosphere Letters, 6(1), 22-31.",
            "3. Adhikari, P. (2018). Monsoon variability and reservoir operation.",
            "",
            "GRANTS",
            "Nepal Academy of Science and Technology, principal investigator, 2021-2025",
            "",
            "SKILLS",
            "Remote sensing, GIS, Python, R",
        ),
        truth=Truth(
            name="Dr Prakash Adhikari",
            emails=("prakash.adhikari@example.edu.np",),
            skills=("Remote sensing", "GIS", "Python", "R"),
            experience=(Role("Associate Professor", "Kathmandu University", "2019", "", True),),
            education=(Study("PhD Hydrology", "University of Tokyo"),),
            awards=("Nepal Academy of Science and Technology",),
            never_skills=("GRANTS", "SELECTED PUBLICATIONS"),
        ),
        notes="Numbered publications, every one wrapped. 'Grants' is an awards heading.",
    ),
    # 31 — a student whose CV is projects and certificates, which is the whole
    #      point of the sections this corpus used to ignore.
    _case(
        slug="projects-and-certificates",
        sector="student",
        profile="undergraduate",
        formats=("pdf", "docx"),
        body=(
            "Sabin Bhandari",
            "sabin.bhandari@example.com | 9812345678",
            "",
            "EDUCATION",
            "BSc Computing, Islington College, 2022 - 2026",
            "",
            "PROJECTS",
            "Bus Tracker - a live map of Kathmandu valley routes, built with Flutter.",
            "Notes API",
            "• A REST API with token authentication, written in Django.",
            "• Deployed on a free tier and used by forty classmates.",
            "",
            "CERTIFICATIONS",
            "AWS Certified Cloud Practitioner",
            "Google Data Analytics Professional Certificate",
            "",
            "SKILLS",
            "Python, Java, Git, SQL",
        ),
        truth=Truth(
            name="Sabin Bhandari",
            emails=("sabin.bhandari@example.com",),
            phones=("9812345678",),
            skills=("Python", "Java", "Git", "SQL"),
            experience=(),
            education=(Study("BSc Computing", "Islington College"),),
            projects=("Bus Tracker", "Notes API"),
            certifications=(
                "AWS Certified Cloud Practitioner",
                "Google Data Analytics Professional Certificate",
            ),
            never_skills=("PROJECTS", "CERTIFICATIONS", "Bus Tracker"),
        ),
        notes="No employment, and the sections that carry this CV instead.",
    ),
)

SCRIPT_CASES: tuple[Case, ...] = (
    # 32 — a name in the script the launch market actually writes in. Every
    #      such candidate imported a CV with no name on it, because "each word
    #      is capitalised" is a test only a cased script can pass.
    _case(
        slug="devanagari-name",
        sector="software",
        profile="experienced",
        # DOCX only, and the reason is the corpus rather than the product:
        # reportlab's built-in fonts carry no Devanagari glyphs, so rendering
        # this case to PDF here draws a row of notdef boxes and would measure
        # the benchmark's own typesetting instead of the reader. A real
        # Devanagari PDF comes out of Word or Google Docs with the font
        # embedded, and by the time the text layer reaches `parse` the two
        # formats are the same code path.
        formats=("docx",),
        body=(
            "नवराज गौतम",
            "Senior Backend Engineer",
            "nawaraj.gautam@example.com | +977 9841 220 118",
            "",
            "EXPERIENCE",
            "Senior Backend Engineer, Khalti — 2021 to Present",
            "• Rebuilt the settlement ledger on event sourcing.",
            "",
            "EDUCATION",
            "BE Computer Engineering, Tribhuvan University, 2014 - 2018",
            "",
            "SKILLS",
            "Python, Django, PostgreSQL, Redis",
            "",
            "LANGUAGES",
            "नेपाली, English",
        ),
        truth=Truth(
            name="नवराज गौतम",
            emails=("nawaraj.gautam@example.com",),
            phones=("+977 9841 220 118",),
            headline="Senior Backend Engineer",
            skills=("Python", "Django", "PostgreSQL", "Redis"),
            experience=(Role("Senior Backend Engineer", "Khalti", "2021", "", True),),
            education=(Study("BE Computer Engineering", "Tribhuvan University"),),
            languages=("नेपाली", "English"),
        ),
        notes="Devanagari name and language. The launch market writes both this way.",
    ),
)

CASES = CASES + HARD_CASES + EVIDENCE_CASES + SCRIPT_CASES


def case_by_slug(slug: str) -> Case:
    for case in CASES:
        if case.slug == slug:
            return case
    raise KeyError(slug)


#: Every (case, format) pair the benchmark runs.
def pairs() -> list[tuple[Case, str]]:
    return [(case, fmt) for case in CASES for fmt in case.formats]
