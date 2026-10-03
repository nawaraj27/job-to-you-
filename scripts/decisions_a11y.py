"""
axe-core and overflow scan of the screens owner decisions D-01..D-10 changed.

Reuses the runner in `applications_a11y.py` (light and dark at 1280 px, overflow
at 375 and 320 px). The screens need particular states, so `prepare` builds
idempotent dev fixtures (`loop.d*@example.test`, companies `loop-d02-*` and
`loop-d04-conflict`) before scanning:

  * D-02: a rejected company still waiting, one whose 30 days have passed, and
    the staff queue with a second attempt and its earlier decision;
  * D-03: an invitation opened by the wrong account;
  * D-04: the staff queue showing a reviewer their conflict (the dev superadmin
    holds an open invitation to `loop-d04-conflict`);
  * D-05: a refused one-letter talent search;
  * D-06: the candidate privacy screen;
  * D-08: the plan screen with a scheduled move to Free, and with an overdue
    invoice;
  * D-09: every settings screen for an employer with no candidate profile.

    python scripts/decisions_a11y.py --out var/benchmark/decisions-a11y.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import applications_a11y as runner  # noqa: E402


def _fixtures() -> dict:
    from django.utils import timezone

    from apps.accounts.models import User
    from apps.billing.models import Invoice, Plan, Subscription
    from apps.billing.services import invoices, subscriptions
    from apps.candidates.services import profile as profile_service
    from apps.companies.models import (
        CompanyInvite,
        CompanyMembership,
        CompanyVerification,
        CompanyVerificationDocument,
        VerificationState,
    )
    from apps.companies.services import company as company_service
    from apps.companies.services import membership as membership_service
    from apps.files.constants import Visibility
    from apps.files.models import Attachment

    staff = User.objects.get(email="ops@careerai.example.com")

    def person(email: str, name: str) -> User:
        user, created = User.objects.get_or_create(email=email, defaults={"full_name": name})
        if created:
            user.set_unusable_password()
            user.save()
        return user

    def company(slug: str, owner: User):
        existing = CompanyMembership.objects.filter(user=owner, company__slug__startswith=slug)
        if existing.exists():
            return existing.first().company
        made = company_service.create_company(actor=owner, name=slug.replace("-", " ").title())
        made.slug = slug
        made.save(update_fields=["slug"])
        CompanyVerificationDocument.objects.create(
            verification=made.verification,
            kind="registration",
            attachment=Attachment.objects.create(
                storage_key=f"loop/{slug}.pdf",
                filename=f"{slug}-registration.pdf",
                mime="application/pdf",
                size=10,
                sha256="d" * 64,
                visibility=Visibility.COMPANY,
                owner_company=made,
                scan_status="clean",
            ),
            uploaded_by=owner,
        )
        return made

    def rejected(slug: str, owner: User, *, days_ago: int):
        made = company(slug, owner)
        verification = CompanyVerification.objects.get(company=made)
        if verification.state == VerificationState.NOT_SUBMITTED:
            company_service.submit_verification(actor=owner, company=made)
            company_service.claim_verification(actor=staff, company=made)
            company_service.review_verification(
                actor=staff,
                company=made,
                decision=VerificationState.REJECTED,
                reason="The registration number does not match the certificate.",
            )
        verification.refresh_from_db()
        if verification.state == VerificationState.REJECTED:
            verification.reviewed_at = timezone.now() - dt.timedelta(days=days_ago)
            verification.save(update_fields=["reviewed_at"])
        return made

    def ensure_bytes() -> None:
        # A fixture document is a real file, or the staff queue links to
        # bytes that are not there (the link sweep reads those links).
        from django.core.files.base import ContentFile
        from django.core.files.storage import storages

        from apps.files.tests.factories import MINIMAL_PDF

        for attachment in Attachment.objects.filter(storage_key__startswith="loop/"):
            storage = storages[attachment.storage_alias]
            if not storage.exists(attachment.storage_key):
                storage.save(attachment.storage_key, ContentFile(MINIMAL_PDF))

    # D-02: waiting, open, and a second attempt in the staff queue.
    waiting_owner = person("loop.d02.waiting@example.test", "Loop Waiting")
    rejected("loop-d02-waiting", waiting_owner, days_ago=3)
    open_owner = person("loop.d02.open@example.test", "Loop Reapply")
    rejected("loop-d02-open", open_owner, days_ago=31)
    second_owner = person("loop.d02.second@example.test", "Loop Second")
    second = rejected("loop-d02-second", second_owner, days_ago=40)
    if CompanyVerification.objects.get(company=second).state == VerificationState.REJECTED:
        company_service.submit_verification(actor=second_owner, company=second)

    # D-04: the dev superadmin holds an open invitation to a pending company.
    conflict_owner = person("loop.d04.owner@example.test", "Loop Conflict")
    conflict = company("loop-d04-conflict", conflict_owner)
    owner_membership = CompanyMembership.objects.get(company=conflict, user=conflict_owner)
    if CompanyVerification.objects.get(company=conflict).state == VerificationState.NOT_SUBMITTED:
        company_service.submit_verification(actor=conflict_owner, company=conflict)
    CompanyInvite.objects.filter(
        company=conflict, email=staff.email, accepted_at__isnull=True
    ).update(revoked_at=timezone.now())
    membership_service.invite(
        actor=conflict_owner,
        actor_membership=owner_membership,
        company=conflict,
        email=staff.email,
        role="viewer",
    )

    # D-03: an invitation opened by the wrong account.
    CompanyInvite.objects.filter(
        company=conflict, email="loop.d03.invited@example.test", accepted_at__isnull=True
    ).update(revoked_at=timezone.now())
    _invite, token = membership_service.invite(
        actor=conflict_owner,
        actor_membership=owner_membership,
        company=conflict,
        email="loop.d03.invited@example.test",
        role="recruiter",
    )

    # D-08: Pro with a scheduled move to Free; an ended plan with an overdue invoice.
    pro = Plan.objects.get(code="pro")
    free = Plan.objects.get(code="free")
    downgrading = person("loop.d08.downgrade@example.test", "Loop Downgrade")
    profile = profile_service.get_or_create_profile(user=downgrading)
    live = Subscription.objects.filter(candidate=profile, state__in=("active", "past_due")).first()
    if live is None:
        live = subscriptions.subscribe(owner=profile, plan=pro)
        invoices.issue(subscription=live)
    if live.plan_id == pro.pk and live.pending_plan_id is None:
        subscriptions.change_plan(subscription=live, plan=free)

    overdue_user = person("loop.d08.overdue@example.test", "Loop Overdue")
    overdue_profile = profile_service.get_or_create_profile(user=overdue_user)
    if not Invoice.objects.filter(subscription__candidate=overdue_profile).exists():
        ended = subscriptions.subscribe(owner=overdue_profile, plan=pro)
        invoice = invoices.issue(subscription=ended)
        Invoice.objects.filter(pk=invoice.pk).update(
            due_on=timezone.localdate() - dt.timedelta(days=5)
        )
        subscriptions.expire(subscription=ended, reason="unpaid")

    ensure_bytes()
    return {"token": token}


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings

    from apps.accounts.models import User

    fixtures = _fixtures()

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    return {
        "sessions": {
            "employer_only": session_for("deepak.yadav@example.com"),
            "candidate": session_for("sita.rai@example.com"),
            "owner": session_for("hr@fonepay.example.com"),
            "waiting": session_for("loop.d02.waiting@example.test"),
            "reapply": session_for("loop.d02.open@example.test"),
            "downgrade": session_for("loop.d08.downgrade@example.test"),
            "overdue": session_for("loop.d08.overdue@example.test"),
            "staff": session_for("ops@careerai.example.com"),
        },
        "pages": {
            "employer_only": [
                ("D-09 notification settings (employer)", "/settings/notifications/"),
                ("D-09 account settings (employer)", "/settings/"),
                ("D-09 privacy (employer)", "/settings/privacy/"),
                ("D-09 your data (employer)", "/settings/data/"),
            ],
            "candidate": [
                ("D-06 privacy (candidate)", "/settings/privacy/"),
                ("D-09 notification settings (candidate)", "/settings/notifications/"),
                (
                    "D-03 invitation, wrong account",
                    f"/employer/invites/{fixtures['token']}/",
                ),
            ],
            "owner": [
                ("D-05 one-letter talent search refused", "/employer/fonepay/talent/?q=a"),
                ("D-05 short skill search", "/employer/fonepay/talent/?q=Go"),
                ("D-07 verification (fonepay)", "/employer/fonepay/verification/"),
            ],
            "waiting": [
                ("D-02 rejected, waiting", "/employer/loop-d02-waiting/verification/"),
            ],
            "reapply": [
                ("D-02 rejected, may apply again", "/employer/loop-d02-open/verification/"),
            ],
            "downgrade": [("D-08 plan, move to Free scheduled", "/dashboard/plan/")],
            "overdue": [("D-08 plan, overdue invoice", "/dashboard/plan/")],
            "staff": [("D-02/D-04 verification queue", "/staff/verification/")],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument(
        "--out", default=str(runner.ROOT / "var" / "benchmark" / "decisions-a11y.json")
    )
    args = parser.parse_args()
    runner.prepare = prepare
    sys.exit(asyncio.run(runner.run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
