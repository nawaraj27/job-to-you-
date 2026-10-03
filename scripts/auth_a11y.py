"""
axe-core scan of the sign-in and sign-up pages, repeated to catch timing.

The Employer report recorded a `color-contrast` result on these pages that
appeared in one run and not the next. Each page is scanned three times in
light and dark, immediately after load, which is when the auth layout's
entrance animation is still running.

    python scripts/auth_a11y.py --out var/benchmark/auth-a11y.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import applications_a11y as runner  # noqa: E402

REPEATS = 3


def prepare() -> dict:
    import django

    django.setup()
    pages = []
    for attempt in range(1, REPEATS + 1):
        pages.append((f"sign in #{attempt}", "/accounts/login/"))
        pages.append((f"sign up #{attempt}", "/accounts/signup/"))
        pages.append((f"check your email #{attempt}", "/accounts/signup/sent/"))
    return {"sessions": {"anonymous": "not-a-session"}, "pages": {"anonymous": pages}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument("--out", default=str(runner.ROOT / "var" / "benchmark" / "auth-a11y.json"))
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
