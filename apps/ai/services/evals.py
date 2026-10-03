"""
The evaluation harness (ADR-A20, SRS §5.11).

The exit criterion this exists for: **a prompt change that degrades quality
fails CI.** Without it, prompt quality is folklore — somebody remembers that
the analysis prompt used to be better, and nobody can show it.

A case is a JSON file beside the prompts: an input, and assertions about the
output that are strong enough to catch a regression and weak enough to survive
a model that phrases things differently. Assertions are structural — required
fields, item counts, terms that must or must not appear — never exact text,
because asserting exact model output produces a suite that fails on every
harmless rewording and is therefore switched off within a month.

CI runs this against the fake adapter, so it costs nothing and is deterministic.
Run it against a real provider by hand before shipping a prompt change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings

from ..models import RunStatus

logger = logging.getLogger(__name__)

EVAL_DIR = "prompts/evals"


@dataclass
class CaseResult:
    name: str
    task_key: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    cost: str = "0"


def eval_dir() -> Path:
    return Path(settings.BASE_DIR) / EVAL_DIR


def load_cases(*, task_key: str = "") -> list[dict[str, Any]]:
    root = eval_dir()
    if not root.exists():
        return []

    cases = []
    for path in sorted(root.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case.setdefault("name", path.stem)
        if not task_key or case.get("task_key") == task_key:
            cases.append(case)
    return cases


def run_all(*, task_key: str = "", user: Any = None) -> dict[str, Any]:
    """Run every case. Returns a summary a CI step can fail on."""
    from . import runner

    results = []
    for case in load_cases(task_key=task_key):
        results.append(run_case(case, runner=runner, user=user))

    failed = [result for result in results if not result.passed]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
    }


def run_case(case: dict[str, Any], *, runner: Any, user: Any = None) -> CaseResult:
    """One case: run the task, then check the output against its assertions."""
    name = case.get("name", "unnamed")
    task_key = case["task_key"]

    run_row = runner.run(
        task_key=task_key,
        inputs=case.get("inputs", {}),
        user=user,
        # Never a cache hit: an evaluation that passes because it read an old
        # answer has evaluated nothing.
        force=True,
    )

    if run_row.status != RunStatus.OK:
        return CaseResult(
            name=name,
            task_key=task_key,
            passed=False,
            failures=[f"run failed: {run_row.status} {run_row.error}".strip()],
            cost=str(run_row.cost),
        )

    failures = check(run_row.output, case.get("expect", {}))
    return CaseResult(
        name=name,
        task_key=task_key,
        passed=not failures,
        failures=failures,
        cost=str(run_row.cost),
    )


def check(output: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """Structural assertions about one output.

    Five kinds, and no sixth until a real case needs one:

      `has`            — these paths exist and are not empty
      `min_items`      — this array has at least this many entries
      `contains`       — these terms appear somewhere in the output
      `absent`         — these terms appear nowhere (the regression guard that
                         catches a prompt starting to invent things)
      `each_has`       — every item of this array has these fields
    """
    failures: list[str] = []

    for path in expect.get("has", []):
        value = _at(output, path)
        if value in (None, "", [], {}):
            failures.append(f"{path} is missing or empty")

    for path, minimum in (expect.get("min_items") or {}).items():
        value = _at(output, path)
        if not isinstance(value, list) or len(value) < minimum:
            got = len(value) if isinstance(value, list) else "not an array"
            failures.append(f"{path} should have at least {minimum} item(s), got {got}")

    haystack = json.dumps(output).lower()
    for term in expect.get("contains", []):
        if term.lower() not in haystack:
            failures.append(f"expected {term!r} somewhere in the output")
    for term in expect.get("absent", []):
        if term.lower() in haystack:
            failures.append(f"{term!r} should not appear in the output")

    for path, fields in (expect.get("each_has") or {}).items():
        value = _at(output, path)
        if not isinstance(value, list):
            failures.append(f"{path} should be an array")
            continue
        for index, item in enumerate(value):
            for name in fields:
                if not isinstance(item, dict) or not item.get(name):
                    failures.append(f"{path}[{index}] is missing {name!r}")

    return failures


def _at(document: Any, path: str) -> Any:
    """Dotted lookup. Returns None rather than raising for a missing path,
    because a missing path is exactly what the caller is testing for."""
    current = document
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current
