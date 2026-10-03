"""
Whole-site link sweep: every link a page offers must open for the person it
was offered to.

For each persona, starts from their home pages and follows internal `href`s
breadth-first, two levels deep. A link that answers 4xx or 5xx for the same
person who was shown it is a dead button: the page offered something the
server then refused. Runs on the dev database inside a rolled-back transaction.

    python scripts/link_sweep.py
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urldefrag, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
os.environ["DJANGO_EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.test import Client  # noqa: E402

PERSONAS = {
    "anonymous": (None, ["/", "/jobs/", "/pricing/"]),
    "candidate": ("sita.rai@example.com", ["/dashboard/", "/applications/", "/settings/"]),
    "owner": ("hr@fonepay.example.com", ["/employer/fonepay/", "/employer/fonepay/applicants/"]),
    "recruiter": ("anita.gurung@example.com", ["/employer/fonepay/", "/employer/fonepay/jobs/"]),
    "hiring_manager": ("sweep.hiring_manager@example.test", ["/employer/fonepay/"]),
    "interviewer": ("sweep.interviewer@example.test", ["/employer/fonepay/"]),
    "viewer": ("loop.viewer@example.test", ["/employer/fonepay/", "/employer/fonepay/applicants/"]),
    "both": ("sweep.both@example.test", ["/dashboard/", "/employer/kora-studio/"]),
    "staff_support": ("sweep.staff.support@example.test", ["/staff/"]),
    "staff_superadmin": ("ops@careerai.example.com", ["/staff/", "/staff/companies/"]),
}

HREF = re.compile(r'href="([^"#][^"]*)"')
SKIP_PREFIXES = ("/static/", "/media/", "/accounts/logout", "/files/", "/admin/")
SKIP_SUFFIXES = (".json", ".zip", ".pdf", ".docx", ".csv", ".ics")
MAX_PER_PAGE = 60
DEPTH = 2


FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S | re.I)
ATTR = re.compile(r'(\w[\w-]*)="([^"]*)"')
HIDDEN = re.compile(r"<input\b[^>]*>", re.I)
BUTTON = re.compile(r"<button\b([^>]*)>", re.I)
SUBMIT_INPUT = re.compile(r'<input\b([^>]*type="submit"[^>]*)>', re.I)
#: The sentences policies use to refuse a person. A form that answers with one
#: of these was offered to somebody who could not use it.
REFUSAL = re.compile(
    r"permission to do that|Your role (in this company )?(does not|can read)|"
    r"needs (moderator|a staff role|company-wide)|Only an owner|Only a superadmin|"
    r"can read this application but not|made by the people assigned",
    re.I,
)
#: Forms whose submission would end the sweep's own session or leave the site.
SKIP_ACTIONS = ("/accounts/logout", "/staff/impersonation/stop/")


def _post_forms(body: str, page: str) -> list[dict]:
    """Every POST form on a page, with the fields it would send without typing.

    Hidden inputs, plus the first named submit button, which is how most
    action forms here say what they do. Typed fields are left empty: the point
    is whether the server refuses the *person*, not the input.
    """
    forms = []
    for attrs_text, inner in FORM.findall(body):
        attrs = dict(ATTR.findall(attrs_text))
        if attrs.get("method", "get").lower() != "post":
            continue
        action = attrs.get("action") or page
        action = action.replace("&amp;", "&")
        if not action.startswith("/") or action.startswith(SKIP_ACTIONS):
            continue
        data: dict[str, str] = {}
        for tag in HIDDEN.findall(inner):
            fields = dict(ATTR.findall(tag))
            if fields.get("type") == "hidden" and fields.get("name"):
                data[fields["name"]] = fields.get("value", "")
        buttons = BUTTON.findall(inner) + SUBMIT_INPUT.findall(inner)
        if not any("disabled" not in attrs_ for attrs_ in buttons):
            # No control a person could press: a read-only view of a form.
            continue
        label = data.get("action") or data.get("command") or ""
        for tag in BUTTON.findall(inner):
            fields = dict(ATTR.findall(tag))
            if fields.get("name") and fields.get("value"):
                data.setdefault(fields["name"], fields["value"])
                label = label or fields["value"]
                break
        forms.append({"action": action, "data": data, "label": label})
    return forms


def main() -> None:
    from apps.accounts.models import User

    broken = []
    visited_total = 0
    for persona, (email, starts) in PERSONAS.items():
        client = Client(HTTP_HOST="localhost", raise_request_exception=False)
        with transaction.atomic():
            if email:
                client.force_login(User.objects.get(email=email))
            seen: set[str] = set()
            frontier = [(url, "(start)") for url in starts]
            for depth in range(DEPTH + 1):
                next_frontier = []
                for url, source in frontier:
                    if url in seen:
                        continue
                    seen.add(url)
                    sid = transaction.savepoint()
                    response = client.get(url)
                    visited_total += 1
                    status = response.status_code
                    if status in (301, 302):
                        location = response.get("Location", "")
                        if location.startswith("/"):
                            status = client.get(location).status_code
                    transaction.savepoint_rollback(sid)
                    if status >= 400:
                        broken.append(
                            {"persona": persona, "link": url, "status": status, "found_on": source}
                        )
                        continue
                    if depth == DEPTH or "html" not in response.get("Content-Type", ""):
                        continue
                    body = response.content.decode("utf-8", "replace")
                    for form in _post_forms(body, url):
                        sid = transaction.savepoint()
                        submitted = client.post(form["action"], form["data"])
                        refused_by_message = False
                        location = submitted.get("Location", "")
                        if submitted.status_code == 302 and location.startswith("/"):
                            landed = client.get(location).content.decode("utf-8", "replace")
                            refused_by_message = bool(REFUSAL.search(landed))
                        elif submitted.status_code < 300:
                            text = submitted.content.decode("utf-8", "replace")
                            refused_by_message = bool(REFUSAL.search(text))
                        transaction.savepoint_rollback(sid)
                        visited_total += 1
                        if refused_by_message:
                            broken.append(
                                {
                                    "persona": persona,
                                    "link": "POST " + form["action"] + " " + form["label"],
                                    "status": "refused",
                                    "found_on": url,
                                }
                            )
                        if submitted.status_code in (403, 404):
                            broken.append(
                                {
                                    "persona": persona,
                                    "link": "POST " + form["action"] + " " + form["label"],
                                    "status": submitted.status_code,
                                    "found_on": url,
                                }
                            )
                    links = []
                    for raw in HREF.findall(body):
                        href = urldefrag(raw.replace("&amp;", "&"))[0]
                        parsed = urlparse(href)
                        if parsed.scheme or parsed.netloc or not href.startswith("/"):
                            continue
                        if href.startswith(SKIP_PREFIXES) or parsed.path.endswith(SKIP_SUFFIXES):
                            continue
                        links.append(href)
                    for href in list(dict.fromkeys(links))[:MAX_PER_PAGE]:
                        if href not in seen:
                            next_frontier.append((href, url))
                frontier = next_frontier
            transaction.set_rollback(True)

    out = ROOT / "var" / "benchmark" / "link-sweep.json"
    out.write_text(json.dumps({"visited": visited_total, "broken": broken}, indent=1))
    print("visited", visited_total, "broken", len(broken))
    grouped = collections.defaultdict(list)
    for row in broken:
        pattern = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}|\d+", "<id>", row["link"])
        grouped[(row["persona"], pattern, row["status"])].append(row["found_on"])
    for (persona, pattern, status), sources in sorted(grouped.items()):
        print(
            f"BROKEN {persona:16} {status} {pattern}  (on {sources[0]}{' +' + str(len(sources) - 1) if len(sources) > 1 else ''})"
        )


if __name__ == "__main__":
    main()
