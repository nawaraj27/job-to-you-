"""
axe-core and overflow scan of the screens changed by the CV templates + CV
improvement repair (2026-09-27).

Reuses the runner in `applications_a11y.py` (light and dark at 1280 px,
overflow at 375 and 320 px):

  * the template gallery, a free template's page, a Pro template's page for a
    free candidate (the lock panel), and the same page previewing the
    candidate's own CV;
  * the plan page and the pricing page;
  * the analysis report (with its hand-off to guided and assisted), the guided
    builder, the assisted CV screen, and the editor.

Needs the development database with `seed_demo --personas-only` data: the free
candidate `demo.interview@example.com` with a CV and an analysis.

    python scripts/cv_improvement_a11y.py --out var/benchmark/cv-improvement-a11y.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import applications_a11y as runner  # noqa: E402

CANDIDATE_EMAIL = "demo.interview@example.com"


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
        .filter(profile__user__email=CANDIDATE_EMAIL)
        .order_by("-updated_at")
        .first()
    )
    pages = [
        ("template gallery", "/cv/templates/"),
        ("free template", "/cv/templates/meridian/"),
        ("pro template, locked", "/cv/templates/vertex/"),
        ("plan", "/dashboard/plan/"),
        ("pricing", "/pricing/"),
        ("analysis report", "/cv/analysis/"),
    ]
    if document is not None:
        doc = document.public_id
        pages += [
            ("template with my CV", f"/cv/templates/atlas/?cv={doc}"),
            ("guided builder", f"/cv/builder/{doc}/guided/"),
            ("assisted CV", f"/cv/builder/{doc}/describe/"),
            ("editor", f"/cv/builder/{doc}/"),
        ]
    return {"sessions": {"candidate": session_for(CANDIDATE_EMAIL)}, "pages": {"candidate": pages}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument(
        "--out", default=str(runner.ROOT / "var" / "benchmark" / "cv-improvement-a11y.json")
    )
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
