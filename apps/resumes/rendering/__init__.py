"""
Rendering a frozen version to a file (SRS §5.5.2, FR-3.10).

Three rules shape everything in this package:

  * **Only a `ResumeVersion` is rendered.** A draft changes between the moment
    a file is produced and the moment anyone opens it, so a PDF of a draft is a
    document nobody can reproduce. The renderers take content and a template
    snapshot; the service layer is what refuses to hand them a draft.
  * **Rendering is deterministic.** The same version, template and format
    produce byte-identical output — which is what makes the cache correct
    rather than merely convenient, and what lets ADR-A8 prove that an
    employer's copy is the file the candidate sent.
  * **Layout is decided once.** Both renderers consume the same block list from
    `layout.py`, so a PDF and a DOCX of one CV say the same things in the same
    order. Two independent layout passes drift within a phase.

    result = render(content, template=snapshot, fmt="pdf")
"""

from .base import (
    ENGINE_VERSION,
    RenderFailed,
    RenderResult,
    UnsupportedRenderFormat,
)
from .engine import available_formats, render

__all__ = [
    "ENGINE_VERSION",
    "RenderFailed",
    "RenderResult",
    "UnsupportedRenderFormat",
    "available_formats",
    "render",
]
