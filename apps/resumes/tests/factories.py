"""
Fixtures for ingestion tests.

The PDF is generated rather than checked in: a binary fixture is a file nobody
reviews, and a minimal PDF that pdfminer genuinely reads is thirty lines of
`reportlab`-free bytes. The DOCX comes from `python-docx`, which is already a
dependency.
"""

from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile

SAMPLE_CV = """Priya Sharma
Backend Engineer
priya.sharma@example.com
+977 9812345678
github.com/priyasharma

Summary
Backend engineer with three years building payment systems.

Experience
Backend Engineer, Nexa Payments  Aug 2023 - Present
- Built the settlement service handling 40,000 transactions a day.
- Cut p95 latency from 800ms to 120ms.
Junior Developer, Himal Software  Jun 2022 - Jul 2023
- Maintained the reporting API.

Education
BSc Computer Science, Tribhuvan University  2018 - 2022

Skills
Python, Django, PostgreSQL, Redis, Docker, Git

Languages
Nepali, English
"""


def docx_bytes(text: str = SAMPLE_CV) -> bytes:
    import docx

    document = docx.Document()
    for line in text.split("\n"):
        document.add_paragraph(line)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def pdf_bytes(text: str = "Priya Sharma\nSkills\nPython, Django") -> bytes:
    """A minimal single-page PDF with a real text layer.

    Written by hand because every PDF library that could do it is a dependency
    this project does not otherwise need. The offsets are computed rather than
    hardcoded so editing the text cannot silently produce a corrupt file.
    """
    lines = text.split("\n")
    content = "BT /F1 12 Tf 72 720 Td 14 TL\n"
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"({escaped}) Tj T*\n"
    content += "ET"
    stream = content.encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def uploaded_pdf(name: str = "cv.pdf", text: str | None = None) -> SimpleUploadedFile:
    data = pdf_bytes(text) if text is not None else pdf_bytes()
    return SimpleUploadedFile(name, data, content_type="application/pdf")


def uploaded_docx(name: str = "cv.docx", text: str = SAMPLE_CV) -> SimpleUploadedFile:
    return SimpleUploadedFile(
        name,
        docx_bytes(text),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
