"""
axe-core and overflow scan of the screens changed by the CV-04 / OPS-01 loop.

Reuses the runner in `applications_a11y.py` (light and dark at 1280 px, overflow
at 375 and 320 px): the three CV builder screens, whose query work changed what
they load, and the staff operations board, which gained the dependency, queue
depth, AI-today and platform-tool sections.

    python scripts/cv04_ops01_a11y.py --out var/benchmark/cv04-ops01-a11y.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import applications_a11y as runner  # noqa: E402


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings

    from apps.accounts.models import User
    from apps.resumes.models import ResumeDocument

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    document = (
        ResumeDocument.objects.alive()
        .filter(profile__user__email="priya.sharma@example.com")
        .order_by("-updated_at")
        .first()
    )
    base = f"/cv/builder/{document.public_id}"
    return {
        "sessions": {
            "candidate": session_for("priya.sharma@example.com"),
            "staff": session_for("ops@careerai.example.com"),
        },
        "pages": {
            "candidate": [
                ("CV editor", f"{base}/"),
                ("CV guided builder", f"{base}/guided/"),
                ("CV assistant", f"{base}/describe/"),
            ],
            "staff": [("staff operations board", "/staff/operations/")],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument(
        "--out", default=str(runner.ROOT / "var" / "benchmark" / "cv04-ops01-a11y.json")
    )
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
