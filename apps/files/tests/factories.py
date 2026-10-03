"""
File fixtures.

Real bytes, not mocks. Type detection reads magic numbers and, for DOCX, opens
the ZIP container and inspects its parts — a stub would test nothing.
"""

from __future__ import annotations

import io
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile

MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
    b"%%EOF\n"
)

_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b"</Types>"
)

_DOCUMENT_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b"<w:body><w:p><w:r><w:t>CareerAI</w:t></w:r></w:p></w:body>"
    b"</w:document>"
)


def docx_bytes() -> bytes:
    """A ZIP container with the parts that make it a Word document."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", _DOCUMENT_XML)
    return buffer.getvalue()


def zip_but_not_docx_bytes() -> bytes:
    """A plain ZIP. Shares DOCX's magic number and must still be rejected."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", b"not a word document")
    return buffer.getvalue()


def upload(
    content: bytes = MINIMAL_PDF,
    name: str = "cv.pdf",
    content_type: str = "application/pdf",
) -> SimpleUploadedFile:
    """An uploaded file whose declared content type is deliberately untrusted."""
    return SimpleUploadedFile(name, content, content_type=content_type)


def pdf_upload(name: str = "cv.pdf") -> SimpleUploadedFile:
    """A PDF CV with a text layer.

    `MINIMAL_PDF` is a valid PDF with an empty page, which the CV upload now
    refuses as a scan (it is one, in every way that matters). Anything that
    uploads "a CV" gets a CV; tests of the empty-page case use `MINIMAL_PDF`.
    """
    from apps.resumes.tests.factories import SAMPLE_CV, pdf_bytes

    return upload(pdf_bytes(SAMPLE_CV), name, "application/pdf")


def docx_upload(name: str = "cv.docx") -> SimpleUploadedFile:
    """A real Word CV (python-docx). `docx_bytes` is the bare container."""
    from apps.resumes.tests.factories import docx_bytes as cv_docx_bytes

    return upload(
        cv_docx_bytes(),
        name,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
