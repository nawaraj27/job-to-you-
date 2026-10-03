"""
axe-core and overflow scan of the screens changed by the CV-template /
company-verification loop.

Reuses the runner in `applications_a11y.py` (light and dark at 1280 px,
overflow at 375 and 320 px):

  * the template gallery, one template's detail page and the CV hub, which
    gained the worked-example route and the "try another template" links;
  * the CV editor, which gained the example notice and the gallery link;
  * the employer verification screen, which gained the required-document
    checklist and the incomplete-submission notice;
  * the staff verification queue, which gained the "not sent" line.

Needs the development database with `seed_demo` data: the candidate
`priya.sharma@example.com` with at least one CV, the staff account
`ops@careerai.example.com`, and a company the demo owner can reach.

    python scripts/cv_templates_verification_a11y.py --out var/benchmark/cv-templates-verification-a11y.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import applications_a11y as runner  # noqa: E402

CANDIDATE_EMAIL = "priya.sharma@example.com"
STAFF_EMAIL = "ops@careerai.example.com"


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings

    from apps.accounts.models import User
    from apps.companies.models import CompanyMembership
    from apps.resumes.models import ResumeDocument, ResumeTemplate

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
    template = ResumeTemplate.objects.filter(is_active=True).order_by("order", "name").first()
    # Whoever owns a company in the demo data; the verification screen is
    # per-company and every owner reaches the same template.
    membership = (
        CompanyMembership.objects.filter(role="owner", deleted_at__isnull=True)
        .select_related("company", "user")
        .order_by("pk")
        .first()
    )

    pages = {
        "candidate": [
            ("CV template gallery", "/cv/templates/"),
            ("CV template detail", f"/cv/templates/{template.slug}/"),
            ("CV hub", "/cv/builder/"),
        ],
        "staff": [("staff verification queue", "/staff/verification/")],
    }
    if document is not None:
        pages["candidate"].append(("CV editor", f"/cv/builder/{document.public_id}/"))
        pages["candidate"].append(
            ("CV editor from the example", f"/cv/builder/{document.public_id}/?started=example")
        )

    sessions = {"candidate": session_for(CANDIDATE_EMAIL), "staff": session_for(STAFF_EMAIL)}
    if membership is not None:
        sessions["owner"] = session_for(membership.user.email)
        pages["owner"] = [
            (
                "employer verification",
                f"/employer/{membership.company.slug}/verification/",
            )
        ]

    return {"sessions": sessions, "pages": pages}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument(
        "--out",
        default=str(runner.ROOT / "var" / "benchmark" / "cv-templates-verification-a11y.json"),
    )
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
