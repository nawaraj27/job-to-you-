"""
axe-core and overflow scan for the 2026-10-01 refinement pages.

Reuses the runner in `applications_a11y.py` (light and dark at 1280 px,
overflow at 375 and 320 px):

  * visitor: home (the expanded footer), a payment return that cannot be
    verified (the refusal page);
  * candidate (`e2e.improve@example.com`, or `--email`): CV upload, the
    processing page, the plan page with its invoice and pay buttons, and the
    most recent payment result page, if there is one.

    python scripts/refinement_a11y.py --out var/benchmark/refinement-a11y.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import applications_a11y as runner  # noqa: E402

EMAIL = "e2e.improve@example.com"


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings

    from apps.accounts.models import User
    from apps.billing.models import PaymentAttempt

    user = User.objects.get(email=EMAIL)
    store = import_module(settings.SESSION_ENGINE).SessionStore()
    store["_auth_user_id"] = str(user.pk)
    store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
    store["_auth_user_hash"] = user.get_session_auth_hash()
    store.create()

    candidate = [
        ("upload", "/cv/upload/"),
        ("processing", "/cv/upload/processing/"),
        ("plan", "/dashboard/plan/"),
    ]
    attempt = (
        PaymentAttempt.objects.filter(invoice__subscription__candidate__user=user)
        .order_by("-created_at")
        .first()
    )
    if attempt is not None:
        candidate.append(("payment result", f"/billing/payments/{attempt.public_id}/"))
    return {
        "sessions": {"visitor": "anonymous", "candidate": store.session_key},
        "pages": {
            "visitor": [
                ("home", "/"),
                ("payment not confirmed", "/billing/return/esewa/ca-unknown/?data=x"),
            ],
            "candidate": candidate,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument(
        "--out", default=str(runner.ROOT / "var" / "benchmark" / "refinement-a11y.json")
    )
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
