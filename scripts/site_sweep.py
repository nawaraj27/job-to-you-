"""
Whole-site route sweep: every URL pattern, every persona, on the dev database.

GETs every non-admin route as each persona with parameters drawn from real
dev rows — the persona's own objects where the route is "mine", Fonepay's for
company routes, and another company's for tenant-isolation rows. Every persona
runs inside a transaction that is rolled back, so view logs, mode switches and
lazily-created rows never reach the dev database.

Reports: any 5xx, any raised exception, and a status table the permission
matrix is read from.

    python scripts/site_sweep.py --out var/benchmark/site-sweep.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import URLPattern, URLResolver, get_resolver  # noqa: E402

PERSONAS = {
    "anonymous": None,
    "candidate": "sita.rai@example.com",
    "owner": "hr@fonepay.example.com",
    "admin": "sweep.admin@example.test",
    "recruiter": "anita.gurung@example.com",
    "hiring_manager": "sweep.hiring_manager@example.test",
    "interviewer": "sweep.interviewer@example.test",
    "viewer": "loop.viewer@example.test",
    "other_owner": "prakriti.rana@example.com",
    "removed_member": "loop.recruiter@example.test",
    "both": "sweep.both@example.test",
    "staff_support": "sweep.staff.support@example.test",
    "staff_moderator": "sweep.staff.moderator@example.test",
    "staff_superadmin": "ops@careerai.example.com",
}

SKIP = {"accounts:logout", "?", "files:access", "staff:impersonation_stop"}
TEXT_SMELLS = re.compile(
    # Word boundaries: a random CSRF token or CSP nonce once contained "FIXme".
    r"lorem ipsum|\bTODO\b|\bFIXME\b|NotImplemented|Traceback|\{\{|\{%|None None|placeholder data",
    re.IGNORECASE,
)


def routes():
    def walk(patterns, prefix="", ns=None):
        for p in patterns:
            if isinstance(p, URLResolver):
                name = p.namespace
                full = ns if not name else (f"{ns}:{name}" if ns else name)
                yield from walk(p.url_patterns, prefix + str(p.pattern), full)
            elif isinstance(p, URLPattern):
                yield prefix + str(p.pattern), (f"{ns}:" if ns else "") + (p.name or "?")

    return [r for r in walk(get_resolver().url_patterns) if not r[0].startswith("admin/")]


def params_for(email):
    from apps.accounts.models import User
    from apps.applications.models import Application, CoverLetter
    from apps.candidates.models import CandidateProfile
    from apps.companies.models import CompanyVerificationDocument
    from apps.interviews.models import InterviewSession
    from apps.jobs.models import Job
    from apps.notifications.models import Notification
    from apps.resumes.models import ResumeDocument, ResumeRender, ResumeUpload

    user = User.objects.filter(email=email).first() if email else None
    sita = User.objects.get(email="sita.rai@example.com")
    mine = user or sita

    def first(qs, attr="public_id"):
        row = qs.first()
        return getattr(row, attr) if row is not None else None

    fp_app = Application.objects.filter(job__company__slug="fonepay").first()
    return {
        "company_slug": "fonepay",
        "job_public_id": first(Job.objects.filter(company__slug="fonepay")),
        "job_slug": first(Job.objects.filter(company__slug="fonepay", status="published"), "slug"),
        "company_public_slug": "fonepay",
        "application_public_id": fp_app.public_id if fp_app else None,
        "candidate_application": first(Application.objects.filter(candidate__user=mine)),
        "interview_company": first(
            InterviewSession.objects.filter(application__job__company__slug="fonepay")
        ),
        "interview_mine": first(InterviewSession.objects.filter(candidate__user=mine))
        if hasattr(InterviewSession, "candidate")
        else first(InterviewSession.objects.filter(application__candidate__user=mine)),
        "talent_profile": first(CandidateProfile.objects.exclude(user=mine)),
        "document": first(ResumeDocument.objects.filter(profile__user=mine)),
        "upload": first(ResumeUpload.objects.filter(profile__user=mine)),
        "render": first(ResumeRender.objects.filter(version__document__profile__user=mine), "pk"),
        "letter": first(CoverLetter.objects.filter(profile__user=mine))
        if hasattr(CoverLetter, "profile")
        else None,
        "notification": first(Notification.objects.filter(recipient=mine)),
        "verification_document": first(CompanyVerificationDocument.objects.all(), "pk"),
        "apply_application": first(Application.objects.filter(candidate__user=mine)),
        "person_id": sita.pk,
    }


def fill(pattern, name, p):
    values = {
        "company_slug": p["company_slug"],
        "slug": p["job_slug"] if name.startswith("jobs:") else "atlas",
        "document_id": p["document"],
        "upload_id": p["upload"],
        "render_id": p["render"],
        "document_id_staff": p["verification_document"],
        "token": "not-a-real-token",
    }
    if name in ("jobs:company", "staff:company"):
        values["slug"] = "fonepay"
    if name == "staff:person":
        values["user_id"] = p["person_id"]
    if name.startswith("employer:"):
        if "interview" in name:
            values["public_id"] = p["interview_company"]
        elif "job" in name:
            values["public_id"] = p["job_public_id"]
        elif "talent" in name:
            values["public_id"] = p["talent_profile"]
        else:
            values["public_id"] = p["application_public_id"]
    elif name.startswith("interviews:"):
        values["public_id"] = p["interview_mine"]
        values["application_id"] = p["candidate_application"]
    elif name.startswith("applications:"):
        values["public_id"] = p["candidate_application"]
    elif name.startswith("letters:"):
        values["public_id"] = p["letter"]
    elif name.startswith("notifications:"):
        values["public_id"] = p["notification"]
    if name == "staff:verification_document":
        values["document_id"] = p["verification_document"]

    missing = []

    def sub(match):
        key = match.group(2)
        value = values.get(key)
        if value is None:
            missing.append(key)
            return "MISSING"
        return str(value)

    return re.sub(r"<(?:(\w+):)?(\w+)>", sub, pattern), missing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "var" / "benchmark" / "site-sweep.json"))
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    from apps.accounts.models import User

    table = []
    failures = []
    for persona, email in PERSONAS.items():
        if args.only and persona not in args.only.split(","):
            continue
        client = Client(HTTP_HOST="localhost", raise_request_exception=False)
        try:
            with transaction.atomic():
                if email:
                    client.force_login(User.objects.get(email=email))
                p = params_for(email)
                for pattern, name in routes():
                    if name in SKIP or pattern.startswith("^"):
                        continue
                    url, missing = fill(pattern, name, p)
                    row = {"persona": persona, "name": name, "url": "/" + url}
                    if missing:
                        row["status"] = "no-data:" + ",".join(missing)
                        table.append(row)
                        continue
                    try:
                        if True:
                            response = client.get(row["url"])
                            status = response.status_code
                            row["status"] = status
                            if status in (301, 302):
                                row["location"] = response.get("Location", "")
                            ctype = response.get("Content-Type", "")
                            if status == 200 and "html" in ctype and not response.streaming:
                                body = response.content.decode("utf-8", "replace")
                                smell = TEXT_SMELLS.search(
                                    re.sub(r"<script.*?</script>|<!--.*?-->", "", body, flags=re.S)
                                )
                                if smell:
                                    row["smell"] = smell.group(0)
                            if status >= 500:
                                failures.append(row)
                    except Exception as exc:  # noqa: BLE001
                        row["status"] = "exception"
                        row["error"] = f"{type(exc).__name__}: {exc}"
                        row["trace"] = traceback.format_exc()[-1500:]
                        failures.append(row)
                    table.append(row)
                transaction.set_rollback(True)
        except Exception as exc:  # noqa: BLE001
            failures.append({"persona": persona, "error": repr(exc)})

    Path(args.out).write_text(json.dumps({"rows": table, "failures": failures}, indent=1))
    print(f"rows {len(table)}  failures {len(failures)}")
    for f in failures:
        print(
            "FAIL",
            f.get("persona"),
            f.get("name"),
            f.get("url"),
            f.get("status"),
            f.get("error", ""),
        )
    smells = [r for r in table if r.get("smell")]
    for s in smells:
        print("SMELL", s["persona"], s["name"], s["smell"])


if __name__ == "__main__":
    main()
