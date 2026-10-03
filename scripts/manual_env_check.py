"""
Check that every manual-testing account works, before a person relies on it.

Signs in to each LOCAL DEMO account through the real sign-in form (CSRF
enforced, the real password, the real redirect), then checks what that account
must see and what it must not. Uses Django's test client against the
development database — it exercises the same views, middleware and
permissions as a browser, but it is **not** a browser and not a human check.

    python manage.py seed_demo --personas-only
    python scripts/manual_env_check.py

Read-only apart from the sign-ins themselves (sessions) and one deliberately
wrong password, which counts once against that account's sign-in rate limit.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from apps.applications.models import Application  # noqa: E402
from apps.companies.models import Company  # noqa: E402
from apps.core import demo_personas as p  # noqa: E402
from apps.interviews.services import scheduling  # noqa: E402
from apps.jobs.models import Job  # noqa: E402

RESULTS: list[tuple[str, str, bool, str]] = []


def check(account: str, what: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((account, what, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {what}{(' - ' + detail) if detail and not ok else ''}")


def sign_in(email: str, password: str = p.PASSWORD) -> tuple[Client, object]:
    # One documentation-range address (RFC 5737) per account: the sign-in limit
    # is 20 per address per 15 minutes, and ten accounts signed in from one
    # address twice in a row trip it. The per-account limit (5) still applies.
    index = [account_email for _letter, account_email, _p in p.ACCOUNTS].index(email)
    client = Client(
        enforce_csrf_checks=True, HTTP_HOST="localhost", REMOTE_ADDR=f"192.0.2.{10 + index}"
    )
    page = client.get(reverse("accounts:login"))
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode())
    response = client.post(
        reverse("accounts:login"),
        {
            "username": email,
            "password": password,
            "csrfmiddlewaretoken": token.group(1) if token else "",
        },
    )
    return client, response


def get(client: Client, url: str) -> tuple[int, str]:
    response = client.get(url, follow=False)
    return response.status_code, response.content.decode(errors="replace")


def denied(status: int) -> bool:
    # A refusal is a 404 (object hidden), 403, or a redirect away (sign-in/onboarding).
    return status in (302, 403, 404)


summit = Company.objects.get(name=p.COMPANY)
lakeside = Company.objects.get(name=p.OTHER_COMPANY)
gandaki = Company.objects.get(name=p.PENDING_COMPANY)


def job(company, title):
    return (
        Job.objects.filter(company=company, title=title, status="published").order_by("pk").first()
    )


def application(email, title):
    return Application.objects.get(candidate__user__email=email, job__title=title)


maya_backend = application(p.CANDIDATE_EMAIL, "Backend Developer")
arjun_uiux = application(p.INTERVIEW_CANDIDATE_EMAIL, "UI/UX Designer")
sita_data = application(p.SECOND_CANDIDATE_EMAIL, "Junior Data Scientist")
sita_ops = application(p.SECOND_CANDIDATE_EMAIL, "Operations Coordinator")


def employer_applicant(company, app):
    return reverse("employer:application_detail", args=[company.slug, app.public_id])


def candidate_application(app):
    return reverse("applications:detail", args=[app.public_id])


def expect_login(label, email, home_fragment):
    client, response = sign_in(email)
    location = response.headers.get("Location", "")
    if location.rstrip("/") == "/employer":
        # The employer home picks the company; follow that one hop.
        location = client.get(location).headers.get("Location", location)
    check(
        label,
        f"sign-in with the documented password lands on {location}",
        response.status_code == 302 and home_fragment in location,
        f"status {response.status_code}, to {location!r}",
    )
    return client


print("\n== Wrong password is refused (Account A)")
client, response = sign_in(p.CANDIDATE_EMAIL, "not-the-password")
check(
    "A",
    "wrong password stays on the sign-in form, not signed in",
    response.status_code in (200, 400) and "_auth_user_id" not in client.session,
)

print("\n== A  Maya (candidate)")
c = expect_login("A", p.CANDIDATE_EMAIL, "/dashboard")
status, body = get(c, reverse("dashboard:index"))
check("A", "dashboard renders", status == 200)
status, body = get(c, reverse("applications:index"))
check("A", "sees her Backend Developer application", status == 200 and "Backend Developer" in body)
status, _ = get(c, candidate_application(sita_data))
check("A", "cannot open Sita's application", denied(status), f"status {status}")
status, _ = get(c, reverse("employer:overview", args=[summit.slug]))
check("A", "cannot open the Summit employer portal", denied(status), f"status {status}")
status, _ = get(c, reverse("staff:verification"))
check("A", "cannot open the staff queue", denied(status), f"status {status}")
status, body = get(c, reverse("interviews:hub"))
check("A", "interviews hub shows the booked live interview", status == 200 and "Coming up" in body)
status, body = get(c, reverse("notifications:index"))
check("A", "notifications page renders", status == 200)
status, _ = get(c, reverse("accounts:logout"))
response = c.post(reverse("accounts:logout"), {"csrfmiddlewaretoken": c.cookies["csrftoken"].value})
status, _ = get(c, reverse("dashboard:index"))
check("A", "after sign-out the dashboard redirects to sign-in", status == 302)

print("\n== B  Arjun (interview-ready candidate)")
c = expect_login("B", p.INTERVIEW_CANDIDATE_EMAIL, "/dashboard")
status, body = get(c, candidate_application(arjun_uiux))
check(
    "B",
    "application shows 'Pick a time' (Interview stage, open times)",
    status == 200 and "Pick a time" in body,
)
status, body = get(c, reverse("interviews:book", args=[arjun_uiux.public_id]))
open_count = scheduling.open_slots(company=summit).count()
check(
    "B",
    f"booking page lists the {open_count} open times",
    status == 200 and open_count >= 1 and "Book your interview" in body,
)
status, body = get(c, reverse("interviews:hub"))
check(
    "B",
    "practice interview for UI/UX Designer is waiting",
    status == 200 and "UI/UX Designer" in body,
)
status, _ = get(c, candidate_application(maya_backend))
check("B", "cannot open Maya's application", denied(status), f"status {status}")

print("\n== C  Rohan (Owner, Summit)")
c = expect_login("C", p.EMPLOYER_EMAIL, f"/employer/{summit.slug}")
status, body = get(c, reverse("employer:applicants", args=[summit.slug]))
check(
    "C",
    "applicants list shows Maya, Arjun and Sita",
    status == 200 and all(n in body for n in ("Maya", "Arjun", "Sita")),
)
for label, url in (
    ("team", reverse("employer:team", args=[summit.slug])),
    ("billing", reverse("employer:billing", args=[summit.slug])),
    ("jobs", reverse("employer:job_list", args=[summit.slug])),
    ("interviews", reverse("employer:interviews", args=[summit.slug])),
):
    status, _ = get(c, url)
    check("C", f"{label} page renders", status == 200, f"status {status}")
status, _ = get(c, reverse("employer:overview", args=[lakeside.slug]))
check("C", "cannot open Lakeside's employer portal", denied(status), f"status {status}")
status, _ = get(c, employer_applicant(lakeside, sita_ops))
check("C", "cannot open Lakeside's applicant", denied(status), f"status {status}")

print("\n== D  Bina (Company admin, Summit)")
c = expect_login("D", p.ADMIN_EMAIL, f"/employer/{summit.slug}")
status, body = get(c, reverse("employer:applicants", args=[summit.slug]))
check(
    "D",
    "applicants list shows applicants on every job",
    status == 200 and all(n in body for n in ("Maya", "Arjun", "Sita")),
)
status, body = get(c, reverse("employer:team", args=[summit.slug]))
check("D", "team page renders", status == 200)
status, body = get(c, reverse("employer:billing", args=[summit.slug]))
check("D", "billing page viewable (D-11: admin views, owner manages)", status == 200)
status, _ = get(c, reverse("employer:overview", args=[lakeside.slug]))
check("D", "cannot open Lakeside's employer portal", denied(status), f"status {status}")

print("\n== E  Nabin (Recruiter, Summit, Backend Developer only)")
c = expect_login("E", p.RECRUITER_EMAIL, f"/employer/{summit.slug}")
status, body = get(c, reverse("employer:applicants", args=[summit.slug]))
check("E", "applicants list shows Maya (assigned job)", status == 200 and "Maya" in body)
check(
    "E",
    "applicants list hides Arjun and Sita (unassigned jobs)",
    "Arjun" not in body and "Sita" not in body,
)
status, _ = get(c, employer_applicant(summit, maya_backend))
check("E", "can open Maya's application (Backend Developer)", status == 200, f"status {status}")
status, _ = get(c, employer_applicant(summit, arjun_uiux))
check(
    "E",
    "cannot open Arjun's application (UI/UX Designer, not assigned)",
    denied(status),
    f"status {status}",
)
status, _ = get(c, employer_applicant(summit, sita_data))
check(
    "E",
    "cannot open Sita's application (Junior Data Scientist, not assigned)",
    denied(status),
    f"status {status}",
)

print("\n== F  Asha (staff moderator)")
c = expect_login("F", p.STAFF_EMAIL, "/staff")
status, body = get(c, reverse("staff:verification"))
check("F", "verification queue shows Gandaki Traders", status == 200 and "Gandaki Traders" in body)
status, _ = get(c, reverse("employer:overview", args=[summit.slug]))
check("F", "staff role grants no employer portal", denied(status), f"status {status}")

print("\n== G  Sita (second candidate)")
c = expect_login("G", p.SECOND_CANDIDATE_EMAIL, "/dashboard")
status, body = get(c, reverse("applications:index"))
check(
    "G",
    "sees her two applications",
    status == 200 and "Junior Data Scientist" in body and "Operations Coordinator" in body,
)
status, _ = get(c, candidate_application(maya_backend))
check("G", "cannot open Maya's application", denied(status), f"status {status}")
status, _ = get(c, candidate_application(arjun_uiux))
check("G", "cannot open Arjun's application", denied(status), f"status {status}")

print("\n== H  Sunita (Owner, Lakeside)")
c = expect_login("H", p.OTHER_EMPLOYER_EMAIL, f"/employer/{lakeside.slug}")
status, body = get(c, reverse("employer:applicants", args=[lakeside.slug]))
check("H", "applicants list shows Sita", status == 200 and "Sita" in body)
check(
    "H",
    "applicants list shows nobody from Summit's jobs",
    "Maya" not in body and "Arjun" not in body,
)
status, _ = get(c, employer_applicant(summit, maya_backend))
check("H", "cannot open Summit's applicant (Maya)", denied(status), f"status {status}")
status, _ = get(c, employer_applicant(summit, sita_data))
check("H", "cannot open Sita's Summit application", denied(status), f"status {status}")

print("\n== I  Kiran (Owner, Gandaki, verification pending)")
c = expect_login("I", p.VERIFY_OWNER_EMAIL, f"/employer/{gandaki.slug}")
status, body = get(c, reverse("employer:verification", args=[gandaki.slug]))
check("I", "verification page renders", status == 200)

print("\n== J  Ramesh (no CV)")
c = expect_login("J", p.NEW_CANDIDATE_EMAIL, "/dashboard")
status, body = get(c, reverse("dashboard:index"))
check("J", "dashboard renders the empty state", status == 200)
status, body = get(c, reverse("applications:index"))
check("J", "no applications", status == 200 and "Backend Developer" not in body)

failed = [r for r in RESULTS if not r[2]]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
for account, what, _ok, detail in failed:
    print(f"  FAIL {account}: {what} {detail}")
sys.exit(1 if failed else 0)
