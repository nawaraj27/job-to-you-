"""
axe-core and overflow scan of the screens changed by the 2026-09-21 loop.

Reuses the runner in `applications_a11y.py` (light and dark at 1280 px,
overflow at 375 and 320 px), signed in as the manual-testing personas that
`seed_demo --personas-only` creates:

  * uploads, reading progress and home (CV progress and return, CVP-01);
  * the company page with its follow control (FOL-01);
  * guided builder and analysis report with CV readiness (CVR-01), the
    analysis page's "jobs that fit" and target-job picker (MAT-02, ENH-01);
  * the assistant aimed at a target job (ENH-01).

    python scripts/loop_2026_09_21_a11y.py --out var/benchmark/loop-2026-09-21-a11y.json
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
    from apps.companies.models import Company
    from apps.core import demo_personas as personas
    from apps.resumes.models import ResumeDocument

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    document = ResumeDocument.objects.alive().get(
        profile__user__email=personas.CANDIDATE_EMAIL, name="Maya Karki CV"
    )
    company = Company.objects.get(name=personas.COMPANY)
    base = f"/cv/builder/{document.public_id}"
    return {
        "sessions": {"candidate": session_for(personas.CANDIDATE_EMAIL)},
        "pages": {
            "candidate": [
                ("home", "/dashboard/"),
                ("uploads", "/cv/upload/"),
                ("reading progress", "/cv/upload/processing/"),
                ("company page with follow", f"/jobs/company/{company.slug}/"),
                ("CV analysis", f"/cv/analysis/?document={document.public_id}"),
                ("guided builder", f"{base}/guided/"),
                ("assistant aimed at a job", f"{base}/describe/?aimed=1"),
                # 2026-09-24 gap loop: the progress chip rides on every page;
                # notifications and settings carry the new follower notices.
                ("jobs with progress chip", "/jobs/"),
                ("notifications", "/notifications/"),
                ("notification settings", "/settings/notifications/"),
            ]
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument(
        "--out", default=str(runner.ROOT / "var" / "benchmark" / "loop-2026-09-21-a11y.json")
    )
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
