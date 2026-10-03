"""Vocabulary for the files subsystem."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Visibility(models.TextChoices):
    """Who may reach a file, and how it is served (SRS §14.1)."""

    PUBLIC = "public", _("Public")  # CDN, long cache, hashed name
    COMPANY = "company", _("Company")  # signed URL, membership checked
    PRIVATE = "private", _("Private")  # signed URL, single use, access logged


class ScanStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    CLEAN = "clean", _("Clean")
    INFECTED = "infected", _("Infected")
    ERROR = "error", _("Scan error")


#: Formats the platform accepts. Detection is by content, never by extension
#: (DEVELOPER.md §6.4); the extension is only used to reject an obvious
#: mismatch between what a file claims to be and what it is.
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
#: Word 97-2003. Accepted for CVs only (`CV_TYPES`): a great many CVs in this
#: market are still .doc files, and refusing them sends people to a converter.
DOC = "application/msword"
PNG = "image/png"
JPEG = "image/jpeg"
SVG = "image/svg+xml"

DOCUMENT_TYPES = frozenset({PDF, DOCX})
CV_TYPES = frozenset({PDF, DOC, DOCX})
IMAGE_TYPES = frozenset({PNG, JPEG})

EXTENSIONS = {
    PDF: ".pdf",
    DOCX: ".docx",
    DOC: ".doc",
    PNG: ".png",
    JPEG: ".jpg",
}

#: SVG is deliberately absent from every accepted set. It is an XML document
#: that can carry script, so accepting one as an image is accepting stored XSS.
REJECTED_TYPES = frozenset({SVG})
