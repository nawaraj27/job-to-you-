"""
Automated accessibility and layout scan of employer onboarding, team,
verification, invitation and the staff queues.

Reuses the runner in `applications_a11y.py` (axe-core in Chromium, light and
dark at 1280 px, overflow at 375 and 320 px) with a different page plan.

Needs, in the development database, the loop fixtures created through the UI:
`loop.owner@example.test` owning `loop-himalaya-traders`, and the staff account
`ops@careerai.example.com`. The anonymous plan uses a session id that does not
exist, which is a signed-out visitor.

    python scripts/employer_a11y.py --out var/benchmark/employer-a11y.json
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import applications_a11y as runner  # noqa: E402

OWNER_EMAIL = "loop.owner@example.test"
STAFF_EMAIL = "ops@careerai.example.com"
COMPANY = "loop-himalaya-traders"


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.urls import reverse

    User = get_user_model()

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    invite_path = reverse("employer:accept_invite", args=["not-a-real-token-for-layout"])
    return {
        "sessions": {
            "owner": session_for(OWNER_EMAIL),
            "staff": session_for(STAFF_EMAIL),
            "anonymous": "signed-out-visitor",
        },
        "pages": {
            "owner": [
                ("register company", "/employer/new/"),
                ("verification", f"/employer/{COMPANY}/verification/"),
                ("team", f"/employer/{COMPANY}/team/"),
                ("company overview", f"/employer/{COMPANY}/"),
                ("updates for followers", f"/employer/{COMPANY}/updates/"),
            ],
            "staff": [
                ("verification queue", "/staff/verification/"),
                ("anomaly queue", "/staff/anomalies/?days=30"),
            ],
            "anonymous": [
                ("sign in from invitation", f"/accounts/login/?next={invite_path}"),
                ("sign up from invitation", f"/accounts/signup/?next={invite_path}"),
            ],
        },
    }


runner.prepare = prepare

if __name__ == "__main__":
    sys.argv = [
        sys.argv[0],
        *(sys.argv[1:] or ["--out", str(ROOT / "var" / "benchmark" / "employer-a11y.json")]),
    ]
    runner.main()
