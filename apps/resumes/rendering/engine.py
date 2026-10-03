"""
Which renderer handles which format.

A registry rather than an if-chain, for the same reason the taxonomy importer
has one: adding an output format should be one module and one line, and the
service layer should not learn a new name when it happens.
"""

from __future__ import annotations

from collections.abc import Callable

from .base import RenderResult, UnsupportedRenderFormat

Renderer = Callable[..., RenderResult]

_RENDERERS: dict[str, Renderer] = {}


def register(fmt: str, renderer: Renderer) -> None:
    _RENDERERS[fmt] = renderer


def available_formats() -> list[str]:
    return sorted(_RENDERERS)


def render(content: dict, *, template: dict | None = None, fmt: str) -> RenderResult:
    """Render content in one format.

    Takes content and a template *snapshot*, never a model instance: a renderer
    that could load a draft is a renderer that will eventually be handed one
    (SRS §5.5.1).
    """
    try:
        renderer = _RENDERERS[fmt]
    except KeyError:
        raise UnsupportedRenderFormat(
            f"No renderer for {fmt!r}. Available: {', '.join(available_formats()) or 'none'}."
        ) from None

    return renderer(content, template=template)


def _install() -> None:
    from . import docx as docx_renderer
    from . import pdf as pdf_renderer

    register("pdf", pdf_renderer.render)
    register("docx", docx_renderer.render)


_install()
