"""
axe-core and overflow scan of the screens changed by the Loop Closure run.

Reuses the runner in `applications_a11y.py` (light and dark at 1280 px, overflow
at 375 and 320 px) with its own page plan: a Viewer's employer overview and job
list, the owner's talent search with a skill filter and the applicant offer
form, and the candidate's privacy (with a blocked company) and data screens.

    python scripts/closure_a11y.py --out var/benchmark/closure-a11y.json
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
    from apps.applications.models import Application
    from apps.candidates.models import CandidateProfile
    from apps.candidates.services import blocking
    from apps.companies.models import Company

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    sita = CandidateProfile.objects.get(user__email="sita.rai@example.com")
    blocking.block_company(
        profile=sita,
        actor=sita.user,
        company=Company.objects.get(slug="kora-studio"),
        reason="closure a11y scan",
    )
    application = (
        Application.objects.filter(job__company__slug="fonepay", stage__label="Interview")
        .exclude(offers__state__in=["draft", "sent", "accepted"])
        .first()
    )
    from apps.messaging.models import Conversation

    thread = Conversation.objects.filter(company__slug="fonepay").exclude(state="blocked").first()
    sita_thread = Conversation.objects.filter(candidate=sita).exclude(state="blocked").first()
    employer_pages = []
    candidate_pages = []
    if thread is not None:
        employer_pages.append(
            ("employer inbox, thread", f"/employer/fonepay/messages/?thread={thread.public_id}")
        )
    if sita_thread is not None:
        candidate_pages.append(
            ("candidate inbox, thread", f"/messages/?thread={sita_thread.public_id}")
        )

    return {
        "sessions": {
            "viewer": session_for("loop.viewer@example.test"),
            "employer": session_for("hr@fonepay.example.com"),
            "candidate": session_for("sita.rai@example.com"),
        },
        "pages": {
            "viewer": [
                ("overview (viewer)", "/employer/fonepay/"),
                ("job list (viewer)", "/employer/fonepay/jobs/"),
            ],
            "employer": [
                ("talent search, skills", "/employer/fonepay/talent/?skills=Python%2C+Basketry"),
                (
                    "applicant detail, offer form",
                    f"/employer/fonepay/applicants/{application.public_id}/",
                ),
                ("team", "/employer/fonepay/team/"),
                ("verification, domain proof", "/employer/fonepay/verification/"),
                ("analytics, new measures", "/employer/fonepay/analytics/?days=90"),
                ("talent pools", "/employer/fonepay/pools/"),
                *employer_pages,
            ],
            "candidate": [
                ("privacy, blocked list", "/settings/privacy/"),
                ("data, zip export", "/settings/data/"),
                *candidate_pages,
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument(
        "--out", default=str(runner.ROOT / "var" / "benchmark" / "closure-a11y.json")
    )
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
