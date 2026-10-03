"""
Build the CV files a manual tester uploads, and say what each must produce.

Writes synthetic CVs to `var/manual-testing/cvs/` (git-ignored) and
`var/manual-testing/CV-TEST-PACK.local.md`: for every file, what it is for,
the expected extraction (the benchmark corpus's recorded truth where the file
comes from it), and what the current parser actually returns — run through the
same `extract_text` and `parse` the upload pipeline uses. Every person and
employer in these files is fictional.

    python scripts/build_manual_cv_pack.py
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from docx import Document  # noqa: E402

from apps.resumes.benchmark import corpus  # noqa: E402
from apps.resumes.parsing import ENGINE_VERSION, extract_text, parse  # noqa: E402

OUT = ROOT / "var" / "manual-testing"
CVS = OUT / "cvs"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"


def _docx(lines: list[tuple[str, str]]) -> bytes:
    document = Document()
    for kind, text in lines:
        if kind == "h0":
            document.add_heading(text, 0)
        elif kind == "h1":
            document.add_heading(text, 1)
        elif kind == "b":
            document.add_paragraph(text, style="List Bullet")
        else:
            document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


SKILLS_BULLETS = [
    ("h0", "Nima Sherpa"),
    ("p", "nima.sherpa@example.com | +977 9811 000 222 | Pokhara, Nepal"),
    ("h1", "Summary"),
    ("p", "Web developer building booking sites for trekking agencies."),
    ("h1", "Experience"),
    ("p", "Web Developer, Annapurna Web Studio (fictional) - Jan 2023 - Present"),
    ("b", "Built booking pages for six trekking agencies in Django."),
    ("b", "Moved two agency sites from shared hosting into Docker containers."),
    ("h1", "Skills"),
    ("b", "Python"),
    ("b", "Django"),
    ("b", "Docker"),
    ("b", "Git"),
    ("b", "GitHub Actions"),
    ("h1", "Education"),
    ("p", "BSc CSIT, Tribhuvan University (2018 - 2022)"),
]

PROJECTS_BULLETS = [
    ("h0", "Pema Lama"),
    ("p", "pema.lama@example.com | +977 9811 000 333 | Kathmandu, Nepal"),
    ("h1", "Summary"),
    ("p", "Computer science graduate who learns by building things."),
    ("h1", "Projects"),
    ("b", "Bus arrival tracker for Kathmandu valley routes, built with Flutter."),
    ("b", "Room availability cache with Redis; reduced database reads by 70% in a class project."),
    ("h1", "Skills"),
    ("p", "Python, Flutter, Redis, SQL"),
    ("h1", "Education"),
    ("p", "BSc Computer Science, Kathmandu University (2020 - 2024)"),
    ("h1", "Certifications"),
    ("p", "AWS Cloud Practitioner"),
]

DEVANAGARI = [
    ("h0", "अनिता राई"),
    ("p", "anita.rai@example.com | +977 9811 000 444 | Dharan, Nepal"),
    ("h1", "Summary"),
    ("p", "Accountant who closes the month for small trading companies."),
    ("h1", "Experience"),
    ("p", "Accountant, Koshi Traders (fictional) - Mar 2022 - Present"),
    ("b", "Closed the monthly books for 3 trading companies in Tally."),
    ("h1", "Skills"),
    ("p", "Accounting, Excel, Tally"),
    ("h1", "Education"),
    ("p", "BBS, Tribhuvan University (2017 - 2021)"),
]

MALFORMED = (
    b"%PDF-1.7\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pa" + b"\x00\xff" * 40
)

PACK = [
    # file name, source, format, purpose, known limitation
    (
        "clean_text_cv.pdf",
        "standard-professional",
        "pdf",
        "Ordinary one-column text PDF: the everyday case.",
        "",
    ),
    (
        "clean_docx_cv.docx",
        "standard-professional",
        "docx",
        "The same CV as a Word file: DOCX reading.",
        "",
    ),
    (
        "scanned_cv.pdf",
        "scanned-picture",
        "pdf",
        "An image-only PDF (no text layer), like a phone scan.",
        "No OCR by decision (ledger §15, §18): expect the 'picture of a CV' message and nothing extracted.",
    ),
    (
        "multi_column_cv.pdf",
        "designer-two-column",
        "pdf",
        "A designer's two-column layout.",
        "Two-column reading is heuristic; check nothing from the side column lands in the wrong section.",
    ),
    (
        "table_history_cv.docx",
        "three-column-table-docx",
        "docx",
        "Work history laid out in a Word table.",
        "Row-major reading of tables is a recorded decision.",
    ),
    (
        "nepali_name_cv.docx",
        DEVANAGARI,
        "docx",
        "A name in Devanagari script: Unicode handling.",
        "",
    ),
    (
        "skills_bullets_cv.docx",
        SKILLS_BULLETS,
        "docx",
        "Skills as a bulleted Word list (the PARSE-02 fix): every skill must be kept.",
        "",
    ),
    (
        "projects_bullets_cv.docx",
        PROJECTS_BULLETS,
        "docx",
        "Projects written only as bullets (the PARSE-01 fix), plus a certification.",
        "",
    ),
    (
        "malformed_cv.pdf",
        MALFORMED,
        "pdf",
        "A truncated, corrupt PDF: must be refused politely, never crash.",
        "Expect a 'could not be read' message.",
    ),
]

EXPECTED_CUSTOM = {
    "nepali_name_cv.docx": {
        "name": "अनिता राई (Anita Rai)",
        "email": "anita.rai@example.com",
        "location": "Dharan, Nepal",
        "skills": ["Accounting", "Excel", "Tally"],
        "roles": ["Accountant at Koshi Traders (fictional)"],
        "projects": [],
        "certifications": [],
    },
    "skills_bullets_cv.docx": {
        "name": "Nima Sherpa",
        "email": "nima.sherpa@example.com",
        "location": "Pokhara, Nepal",
        "skills": ["Python", "Django", "Docker", "Git", "GitHub Actions"],
        "roles": ["Web Developer at Annapurna Web Studio (fictional)"],
        "projects": [],
        "certifications": [],
    },
    "projects_bullets_cv.docx": {
        "name": "Pema Lama",
        "email": "pema.lama@example.com",
        "location": "Kathmandu, Nepal",
        "skills": ["Python", "Flutter", "Redis", "SQL"],
        "roles": [],
        "projects": ["Bus arrival tracker…", "Room availability cache with Redis…"],
        "certifications": ["AWS Cloud Practitioner"],
    },
}


def main() -> None:
    CVS.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, source, fmt, purpose, limitation in PACK:
        truth = None
        if isinstance(source, str):
            case = corpus.case_by_slug(source)
            data = corpus.render(case, fmt)
            truth = case.truth
        elif isinstance(source, bytes):
            data = source
        else:
            data = _docx(source)
        (CVS / name).write_bytes(data)

        mime = PDF if fmt == "pdf" else DOCX
        try:
            text = extract_text(data, mime=mime)
            parsed = parse(text, mime=mime)
            error = ""
        except Exception as exc:  # noqa: BLE001 - reported as the result
            parsed, error = None, f"{type(exc).__name__}: {getattr(exc, 'message', str(exc))}"
        rows.append((name, source, len(data), purpose, limitation, truth, parsed, error))
        print(f"{name:26} {len(data):7d} bytes  {'error: ' + error if error else 'parsed'}")

    lines = [
        "# CV TEST PACK — LOCAL TEST ONLY",
        "",
        f"Generated by `python scripts/build_manual_cv_pack.py`. Parser engine {ENGINE_VERSION}.",
        "Every person and employer in these files is fictional. Files: `var/manual-testing/cvs/`.",
        "",
        "Upload each as the candidate named in the test plan (Account J, Ramesh, has no CV",
        "yet and is the cleanest place to start; delete uploads afterwards from the Uploads page).",
        "'Expected' is the benchmark truth (or, for the two custom files, the content",
        "written into them); 'Parser today' is what the current reader actually returns.",
        "",
    ]
    for name, source, size, purpose, limitation, truth, parsed, error in rows:
        lines += [f"## {name}", "", f"- **Purpose:** {purpose}", f"- **Size:** {size} bytes"]
        if isinstance(source, str):
            lines.append(f"- **Source:** benchmark corpus case `{source}`")
        if limitation:
            lines.append(f"- **Known limitation / expected message:** {limitation}")
        if truth is not None:
            lines += [
                "- **Expected:**",
                f"  - name: {truth.name or '—'}; email: {', '.join(truth.emails) or '—'}",
                f"  - skills: {', '.join(truth.skills) or '—'}",
                f"  - experience: {'; '.join(f'{r.title} at {r.org}' for r in truth.experience) or '—'}",
                f"  - education: {'; '.join(s.qualification for s in truth.education) or '—'}",
                f"  - projects: {', '.join(truth.projects) or '—'}; certifications: {', '.join(truth.certifications) or '—'}",
                f"  - picture (no text): {'yes' if truth.is_picture else 'no'}",
            ]
        elif name in EXPECTED_CUSTOM:
            e = EXPECTED_CUSTOM[name]
            lines += [
                "- **Expected:**",
                f"  - name: {e['name']}; email: {e['email']}; location: {e['location']}",
                f"  - skills: {', '.join(e['skills'])}",
                f"  - experience: {'; '.join(e['roles']) or '—'}",
                f"  - projects: {', '.join(e['projects']) or '—'}; certifications: {', '.join(e['certifications']) or '—'}",
            ]
        if error:
            lines.append(f"- **Parser today:** refused — {error}")
        elif parsed is not None:
            lines += [
                "- **Parser today:**",
                f"  - name: {parsed.name or '—'}; email: {', '.join(parsed.emails) or '—'}; "
                f"location: {getattr(parsed, 'location', '') or '—'}",
                f"  - skills: {', '.join(parsed.skill_terms) or '—'}",
                f"  - experience: {'; '.join(f'{r.title} at {r.org}' for r in parsed.experience) or '—'}",
                f"  - education: {'; '.join(getattr(s, 'qualification', str(s)) for s in parsed.education) or '—'}",
                f"  - projects: {', '.join(p.name for p in parsed.projects) or '—'}; "
                f"certifications: {', '.join(parsed.certifications) or '—'}",
                f"  - warnings: {' | '.join(parsed.warnings) or 'none'}",
            ]
        lines.append("")
    (OUT / "CV-TEST-PACK.local.md").write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT / "CV-TEST-PACK.local.md")


if __name__ == "__main__":
    main()
