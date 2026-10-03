#!/usr/bin/env python
"""
Fail the build if a service takes a `request`.

The rule (SRS §7.2, DEVELOPER.md §3.3): services take an explicit `actor` and,
where tenant-scoped, an explicit `company`. Never a request.

It is not a style preference. A scheduled job, a Celery task and a management
command all have an actor and no request. A service that needs HTTP to know who
is calling it cannot be called from any of them — so the background path either
grows its own unscoped queries, or the tenant check quietly disappears. That is
how employer data leaks between companies in a system whose web tier is
perfectly scoped.

The rule held through P1 by discipline. From P2 it holds by build failure.

    python scripts/check_service_signatures.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Modules where a request is legitimate: the portal edge, middleware, views,
#: and anything whose whole job is to speak HTTP.
ALLOWED_PARTS = {"views.py", "middleware.py", "forms.py", "urls.py", "csp.py", "ratelimit.py"}
ALLOWED_DIRS = {"portals", "tests", "migrations"}

TARGET_GLOBS = ("apps/*/services.py", "apps/*/services/*.py", "apps/*/policies.py")

BANNED = {"request", "req"}


def offending_functions(path: Path) -> list[tuple[int, str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue

        args = node.args
        names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
        if args.vararg:
            names.append(args.vararg.arg)
        if args.kwarg:
            names.append(args.kwarg.arg)

        for name in names:
            if name in BANNED:
                found.append((node.lineno, node.name, name))

    return found


def main() -> int:
    failures: list[str] = []

    for pattern in TARGET_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            parts = set(path.parts)
            if parts & ALLOWED_DIRS or path.name in ALLOWED_PARTS:
                continue

            for line, function, parameter in offending_functions(path):
                relative = path.relative_to(ROOT).as_posix()
                failures.append(
                    f"{relative}:{line}: {function}() takes {parameter!r}. "
                    f"Services take an explicit actor and company, never a request "
                    f"(SRS §7.2)."
                )

    if failures:
        print("Service signature check failed:\n", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "\nBackground jobs have an actor and no request. A service that needs one "
            "cannot be called from them, and the tenant check goes missing.",
            file=sys.stderr,
        )
        return 1

    print("Service signature check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
