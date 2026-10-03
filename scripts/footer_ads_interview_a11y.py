"""
axe-core and overflow scan for the footer, the demo ad slots and the interview
question page (2026-09-28).

Reuses the runner in `applications_a11y.py` (light and dark at 1280 px,
overflow at 375 and 320 px):

  * visitor: home, pricing, jobs (footer, jobs ad slot), a demo-ad page;
  * candidate: dashboard (side-column ad), jobs, analysis (ad under the report);
  * interviewing candidate: the current interview question (no ad, no-store).

Needs `seed_demo --personas-only` data. The interviewing candidate is any
candidate with an interview in progress; the scan skips that side otherwise.

    python scripts/footer_ads_interview_a11y.py --out var/benchmark/footer-ads-interview-a11y.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import applications_a11y as runner  # noqa: E402

CANDIDATE_EMAIL = "demo.candidate@example.com"


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings

    from apps.accounts.models import User
    from apps.interviews.models import InterviewSession, SessionState

    def session_for(user: User) -> str:
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    sessions = {
        "visitor": "anonymous",
        "candidate": session_for(User.objects.get(email=CANDIDATE_EMAIL)),
    }
    pages = {
        "visitor": [
            ("home", "/"),
            ("pricing", "/pricing/"),
            ("jobs", "/jobs/"),
            ("demo ad page", "/sponsored/example-learning/"),
        ],
        "candidate": [
            ("dashboard", "/dashboard/"),
            ("jobs", "/jobs/"),
            ("analysis", "/cv/analysis/"),
        ],
    }
    running = (
        InterviewSession.objects.filter(state=SessionState.IN_PROGRESS, application=None)
        .select_related("candidate__user")
        .first()
    )
    if running is not None:
        sessions["interviewing"] = session_for(running.candidate.user)
        pages["interviewing"] = [("interview question", f"/interviews/{running.public_id}/")]
    return {"sessions": sessions, "pages": pages}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument(
        "--out",
        default=str(runner.ROOT / "var" / "benchmark" / "footer-ads-interview-a11y.json"),
    )
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
