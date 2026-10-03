"""
Automated accessibility and layout scan of talent search and candidate privacy.

Reuses the runner in `applications_a11y.py` (axe-core in Chromium, light and
dark at 1280 px, overflow at 375 and 320 px) with a different page plan.

Opening a talent profile is the act that records a view, so running this adds
view rows for the demo candidate to the development database.

    python scripts/talent_a11y.py --out var/benchmark/talent-a11y.json
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import applications_a11y as runner  # noqa: E402

CANDIDATE_EMAIL = "sita.rai@example.com"
EMPLOYER_EMAIL = "hr@fonepay.example.com"


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings
    from django.contrib.auth import get_user_model

    from apps.candidates.models import CandidateProfile

    User = get_user_model()

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    profile = CandidateProfile.objects.get(user__email=CANDIDATE_EMAIL)
    return {
        # Employer first, so the candidate's "who opened your profile" list has
        # rows by the time it is scanned.
        "sessions": {
            "employer": session_for(EMPLOYER_EMAIL),
            "candidate": session_for(CANDIDATE_EMAIL),
        },
        "pages": {
            "employer": [
                ("talent search empty", "/employer/fonepay/talent/"),
                ("talent search results", "/employer/fonepay/talent/?seeking=open"),
                ("talent search no match", "/employer/fonepay/talent/?q=zzzzqqq"),
                ("talent search refused", "/employer/fonepay/talent/?min_months=-1"),
                ("talent profile", f"/employer/fonepay/talent/{profile.public_id}/"),
            ],
            "candidate": [
                ("visibility", "/applications/visibility/"),
                ("privacy settings", "/settings/privacy/"),
                ("data settings", "/settings/data/"),
            ],
        },
    }


runner.prepare = prepare

if __name__ == "__main__":
    sys.argv = [
        sys.argv[0],
        *(sys.argv[1:] or ["--out", str(ROOT / "var" / "benchmark" / "talent-a11y.json")]),
    ]
    runner.main()
