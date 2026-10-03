"""
Automated accessibility and layout scan of the notification feed and the
notification preferences, from both sides.

Reuses the runner in `applications_a11y.py` (axe-core in Chromium, light and
dark at 1280 px, overflow at 375 and 320 px) with a different page plan. The
employer account is the one created through the UI in the employer onboarding
loop (`loop.owner@example.test`); the candidate is the demo candidate.

    python scripts/notifications_a11y.py --out var/benchmark/notifications-a11y.json
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import applications_a11y as runner  # noqa: E402

EMPLOYER_EMAIL = "loop.owner@example.test"
CANDIDATE_EMAIL = "sita.rai@example.com"


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings
    from django.contrib.auth import get_user_model

    User = get_user_model()

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    pages = [
        ("feed", "/notifications/"),
        ("feed unread", "/notifications/?unread=1"),
        ("feed empty filter", "/notifications/?kind=no.such_kind"),
        ("preferences", "/settings/notifications/"),
    ]
    return {
        "sessions": {
            "employer": session_for(EMPLOYER_EMAIL),
            "candidate": session_for(CANDIDATE_EMAIL),
        },
        "pages": {"employer": pages, "candidate": pages},
    }


runner.prepare = prepare

if __name__ == "__main__":
    sys.argv = [
        sys.argv[0],
        *(sys.argv[1:] or ["--out", str(ROOT / "var" / "benchmark" / "notifications-a11y.json")]),
    ]
    runner.main()
