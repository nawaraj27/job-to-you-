"""
axe-core and overflow scan of the screens the whole-site loop added or changed.

Reuses the runner in `applications_a11y.py` (light and dark at 1280 px, overflow
at 375 and 320 px): account security and password reset, the two-factor
prompt, the staff companies, people, staff-roles and operations screens, the
employer board and overview as a Viewer, a paused-capable job page, and the
saved jobs list.

    python scripts/wholesite_a11y.py --out var/benchmark/wholesite-a11y.json
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
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    from apps.accounts.models import User
    from apps.jobs.models import Job

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    sita = User.objects.get(email="sita.rai@example.com")
    reset = (
        f"/accounts/password/reset/{urlsafe_base64_encode(force_bytes(sita.pk))}/"
        f"{default_token_generator.make_token(sita)}/"
    )
    job = Job.objects.filter(company__slug="fonepay", status="published").first()
    return {
        "sessions": {
            "anonymous": "none",
            "candidate": session_for("sita.rai@example.com"),
            "owner": session_for("hr@fonepay.example.com"),
            "viewer": session_for("loop.viewer@example.test"),
            "staff": session_for("ops@careerai.example.com"),
        },
        "pages": {
            "anonymous": [
                ("sign in", "/accounts/login/"),
                ("password reset request", "/accounts/password/reset/"),
                ("password reset confirm", reset),
            ],
            "candidate": [
                ("security settings", "/settings/security/"),
                ("account settings", "/settings/"),
                ("saved jobs", "/jobs/saved/"),
                ("two-factor prompt (signed in)", "/accounts/login/code/"),
            ],
            "owner": [
                ("job detail with pause", f"/employer/fonepay/jobs/{job.public_id}/"),
                ("overview", "/employer/fonepay/"),
            ],
            "viewer": [
                ("applicant board (viewer)", "/employer/fonepay/applicants/"),
                ("overview (viewer)", "/employer/fonepay/"),
                ("interviews (viewer)", "/employer/fonepay/interviews/"),
            ],
            "staff": [
                ("operations board", "/staff/operations/"),
                ("companies", "/staff/companies/"),
                ("company", "/staff/companies/fonepay/"),
                ("people search", "/staff/people/?q=sita"),
                ("person", f"/staff/people/{sita.pk}/"),
                ("staff roles", "/staff/team/"),
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument(
        "--out", default=str(runner.ROOT / "var" / "benchmark" / "wholesite-a11y.json")
    )
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
