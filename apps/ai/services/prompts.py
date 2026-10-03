"""
Prompt loading (ADR-A20).

Prompts are **files in version control**, not rows somebody can edit at 2 a.m.
They determine the output, which makes them code with a text syntax, and they
deserve what code gets: review, a diff, a test run and a rollback.

A deploy step loads each file, hashes it, and records a `PromptTemplate`. The
version increments only when the body actually changes, so `AIRun.prompt_version`
identifies exactly what ran six months later — and re-running the loader on an
unchanged file is a no-op rather than a new version nobody asked for.

File format: JSON front matter, then the prompt. JSON rather than YAML because
this project has no YAML dependency and a prompt file is not the place to
acquire one.

    ---
    {"system": "You are a careful reviewer of CVs.",
     "schema": {"type": "object", "required": ["findings"]}}
    ---
    Review the CV below and list what would stop a recruiter reading on.

    {cv_text}
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from django.conf import settings

from config import ai as ai_config

from ..models import PromptTemplate

logger = logging.getLogger(__name__)


class PromptMissing(LookupError):
    """No prompt has been loaded for this task.

    Loud rather than silent: a task with no prompt cannot run, and a runner
    that quietly substituted a default would produce output nobody wrote.
    """


def prompt_dir() -> Path:
    return Path(settings.BASE_DIR) / ai_config.PROMPT_DIR


def current(task_key: str) -> PromptTemplate:
    """The newest loaded version for one task."""
    template = PromptTemplate.objects.filter(task_key=task_key).order_by("-version").first()
    if template is None:
        raise PromptMissing(f"No prompt loaded for {task_key!r}. Run `manage.py load_prompts`.")
    return template


def render(template: PromptTemplate, inputs: dict[str, Any]) -> tuple[str, str]:
    """Fill the prompt from the caller's inputs.

    A missing placeholder renders blank rather than raising: half a prompt
    produces a poor answer, and an exception produces none — and the schema
    check downstream catches the cases where the difference mattered.
    """
    system, body = _split_system(template.body)
    return system, _fill(body, inputs)


def _fill(text: str, inputs: dict[str, Any]) -> str:
    class _Blank(dict):
        def __missing__(self, key: str) -> str:
            logger.warning("prompt placeholder %r had no input", key)
            return ""

    try:
        return text.format_map(_Blank(inputs))
    except (ValueError, IndexError):
        # A literal brace in the prompt body. Send it as written rather than
        # failing the run over punctuation.
        return text


def _split_system(body: str) -> tuple[str, str]:
    """The stored body keeps its system line so a run is reproducible from the
    row alone, without reading the file it came from."""
    marker = "\n---\n"
    if body.startswith("SYSTEM:"):
        head, _, rest = body.partition(marker)
        return head.removeprefix("SYSTEM:").strip(), rest.strip()
    return "", body.strip()


# --- loading --------------------------------------------------------------


def load_all(*, directory: Path | None = None) -> dict[str, int]:
    """Load every prompt file. Idempotent.

    Returns counts of what happened, so a deploy log says whether anything
    actually changed.
    """
    root = directory or prompt_dir()
    if not root.exists():
        return {"files": 0, "created": 0, "unchanged": 0}

    files = created = unchanged = 0
    for path in sorted(root.glob("*.md")):
        files += 1
        if load_one(path) is None:
            unchanged += 1
        else:
            created += 1

    return {"files": files, "created": created, "unchanged": unchanged}


def load_one(path: Path) -> PromptTemplate | None:
    """Load one file. Returns None when the body is unchanged.

    The task key is the filename, so a prompt for `resume.analysis` lives in
    `resume.analysis.md` and nothing has to map one to the other.
    """
    task_key = path.stem
    if task_key not in ai_config.TASKS:
        logger.warning("prompt file %s has no configured task", path.name)
        return None

    stored, digest, schema = _read(path)

    latest = PromptTemplate.objects.filter(task_key=task_key).order_by("-version").first()
    if latest is not None and latest.body_sha256 == digest and latest.output_schema == schema:
        # Unchanged. A new version for an identical body would make
        # `AIRun.prompt_version` meaningless and invalidate the cache for
        # nothing. The schema is compared too: an edit to the schema alone —
        # a field made required, an enum narrowed — was read as "unchanged",
        # and the old schema went on validating the model's output.
        return None

    return PromptTemplate.objects.create(
        task_key=task_key,
        version=(latest.version + 1) if latest else 1,
        body=stored,
        body_sha256=digest,
        output_schema=schema,
        source_path=str(path.name),
    )


def _read(path: Path) -> tuple[str, str, dict[str, Any]]:
    """The stored body, its hash and the output schema of one prompt file."""
    system, schema, body = _parse_file(path.read_text(encoding="utf-8"))
    stored = f"SYSTEM:{system}\n---\n{body}" if system else body
    return stored, hashlib.sha256(stored.encode()).hexdigest(), schema


def stale(*, directory: Path | None = None) -> list[tuple[str, str]]:
    """Prompt files the database does not match: `(task_key, why)` for each.

    What `load_prompts --check` reports. A deploy that skipped `load_prompts`
    does not fail — the previous versions keep running — so this is how a
    deploy, or anybody wondering, finds out.
    """
    root = directory or prompt_dir()
    if not root.exists():
        return []
    found = []
    for path in sorted(root.glob("*.md")):
        task_key = path.stem
        if task_key not in ai_config.TASKS:
            continue
        _stored, digest, schema = _read(path)
        latest = PromptTemplate.objects.filter(task_key=task_key).order_by("-version").first()
        if latest is None:
            found.append((task_key, "not loaded"))
        elif latest.body_sha256 != digest:
            found.append((task_key, f"text differs from loaded version {latest.version}"))
        elif latest.output_schema != schema:
            found.append((task_key, f"schema differs from loaded version {latest.version}"))
    return found


def _parse_file(raw: str) -> tuple[str, dict[str, Any], str]:
    """Front matter, then the prompt.

    The front matter is JSON rather than YAML: this project has no YAML
    dependency, and a prompt file is not the place to acquire one.
    """
    text = raw.strip()
    if not text.startswith("---"):
        return "", {}, text

    _, _, rest = text.partition("---")
    front, _, body = rest.partition("---")

    try:
        meta = json.loads(front.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Front matter is not valid JSON: {exc.msg}") from exc

    return str(meta.get("system") or ""), meta.get("schema") or {}, body.strip()
