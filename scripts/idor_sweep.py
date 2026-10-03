"""
Object-level authorization sweep (IDOR): one persona's links, replayed as another.

For each pair (A, B): crawl the site as A, collecting every internal URL and
every POST form whose path carries an object reference (a UUID, a number, or
A's company slug). Then replay each as B:

  * a GET that B can open (200) and that B's own crawl never reached is flagged;
  * a POST to A's object that B is not refused (403/404/405, or sent to sign in)
    is flagged.

A flag is a lead, not a verdict: a public page (a job advert) is legitimately
open to anybody, which is why B's own crawl is the first filter and every
remaining flag is printed for review. Runs on the development database inside a
transaction that is rolled back, so nothing A or B "does" persists.

    python scripts/idor_sweep.py --out var/benchmark/idor-sweep.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.db import connection, transaction  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

from apps.accounts import mfa_policy  # noqa: E402
from apps.accounts.models import User  # noqa: E402
from apps.companies.models import CompanyMembership  # noqa: E402

PAIRS = [
    ("sita.rai@example.com", "demo.candidate@example.com"),
    ("demo.candidate@example.com", "sita.rai@example.com"),
    ("hr@fonepay.example.com", "prakriti.rana@example.com"),
    ("prakriti.rana@example.com", "hr@fonepay.example.com"),
    ("demo.employer@example.com", "hr@fonepay.example.com"),
    ("sita.rai@example.com", "hr@fonepay.example.com"),
]
MAX_PAGES = 350
SKIP = re.compile(r"/logout|/static/|/media/|/admin/|/impersonat|/healthz|/readyz|/stop/?$")
OBJECT = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(/|$)|/\d+(/|$)", re.IGNORECASE
)
DENIED = {403, 404, 405, 410}
#: A value in a hidden field that names an object.
ID_VALUE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{1,10})$", re.I
)
WRITE = re.compile(r'^\s*(INSERT INTO|UPDATE|DELETE FROM)\s+"?([a-z0-9_]+)"?', re.I)
#: Writes any signed-in request makes, whoever it is and whatever it asks.
BENIGN_TABLES = {"django_session", "accounts_user"}
#: Variants of one action endpoint replayed (different hidden ids).
PER_ACTION = 8

# MFA enforcement is tested on its own; here it would only stop the crawl at
# /settings/security/ for owners past their grace period.
mfa_policy.surface_requires = lambda request: False  # type: ignore[assignment]


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.forms: list[dict] = []
        self._form: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.hrefs.append(attrs["href"])
        for key in ("hx-get",):
            if attrs.get(key):
                self.hrefs.append(attrs[key])
        if tag == "form":
            self._form = {
                "method": (attrs.get("method") or "get").lower(),
                "action": attrs.get("action") or "",
                "fields": {},
            }
            self.forms.append(self._form)
        for key in ("hx-post", "hx-delete", "hx-put", "hx-patch"):
            if attrs.get(key):
                self.forms.append({"method": "post", "action": attrs[key], "fields": {}})
        if (
            tag == "input"
            and self._form is not None
            and attrs.get("name")
            and attrs.get("type", "text") in ("hidden", "submit")
            and attrs["name"] != "csrfmiddlewaretoken"
        ):
            self._form["fields"][attrs["name"]] = attrs.get("value", "")
        if tag in ("button",) and self._form is not None and attrs.get("name"):
            self._form["fields"].setdefault(attrs["name"], attrs.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None


def starts_for(user: User) -> list[str]:
    starts = [
        "/dashboard/",
        "/cv/",
        "/applications/",
        "/interviews/",
        "/messages/",
        "/letters/",
        "/jobs/saved/",
        "/notifications/",
        "/settings/",
    ]
    for membership in CompanyMembership.objects.filter(user=user).select_related("company"):
        starts.append(f"/employer/{membership.company.slug}/")
    return starts


def own_slugs(user: User) -> set[str]:
    return set(CompanyMembership.objects.filter(user=user).values_list("company__slug", flat=True))


def is_object_url(path: str, slugs: set[str]) -> bool:
    return bool(OBJECT.search(path)) or any(f"/employer/{slug}/" in path for slug in slugs)


def crawl(client: Client, starts: list[str]) -> tuple[set[str], list[dict]]:
    seen: set[str] = set()
    forms: list[dict] = []
    queue = deque(starts)
    while queue and len(seen) < MAX_PAGES:
        url = queue.popleft()
        if url in seen or SKIP.search(url):
            continue
        seen.add(url)
        try:
            response = client.get(url, follow=False)
        except Exception as exc:  # noqa: BLE001 - a crawl failure is reported, not fatal
            print("  crawl error", url, exc)
            continue
        if response.status_code != 200 or "html" not in response.get("Content-Type", ""):
            continue
        parser = Links()
        parser.feed(response.content.decode("utf-8", "replace"))
        for href in parser.hrefs:
            target = urljoin(url, href)
            parts = urlsplit(target)
            if parts.netloc and parts.netloc not in ("testserver", "localhost"):
                continue
            path = parts.path + (f"?{parts.query}" if parts.query else "")
            if path.startswith("/") and path not in seen:
                queue.append(path)
        for form in parser.forms:
            if form["method"] == "post":
                action = urlsplit(urljoin(url, form["action"] or url)).path
                forms.append({"action": action, "fields": form["fields"], "page": url})
    return seen, forms


def replay(owner: str, other: str) -> dict:
    a = User.objects.get(email=owner)
    b = User.objects.get(email=other)
    slugs = own_slugs(a)
    client_a, client_b = Client(), Client()
    client_a.force_login(a)
    client_b.force_login(b)

    pages_a, forms_a = crawl(client_a, starts_for(a))
    pages_b, _ = crawl(client_b, starts_for(b))
    candidates = sorted(
        p for p in pages_a if is_object_url(urlsplit(p).path, slugs) and p not in pages_b
    )

    get_flags = []
    for url in candidates:
        response = client_b.get(url, follow=False)
        location = response.get("Location", "")
        if response.status_code == 200:
            body = response.content.decode("utf-8", "replace")
            leaked = [
                marker
                for marker in (a.email, a.full_name if hasattr(a, "full_name") else "")
                if marker and marker in body
            ]
            get_flags.append({"url": url, "status": 200, "owner_marker_in_body": leaked})
        elif response.status_code in (301, 302) and "/accounts/login" not in location:
            get_flags.append({"url": url, "status": response.status_code, "location": location})

    post_flags = []
    tried: set[tuple] = set()
    per_action: dict[str, int] = {}
    for form in forms_a:
        action = form["action"]
        if SKIP.search(action):
            continue
        has_ids = any(ID_VALUE.match(str(value or "")) for value in form["fields"].values())
        if not (is_object_url(action, slugs) or has_ids):
            continue
        key = (action, tuple(sorted(form["fields"].items())))
        if key in tried or per_action.get(action, 0) >= PER_ACTION:
            continue
        tried.add(key)
        per_action[action] = per_action.get(action, 0) + 1
        with transaction.atomic():
            with CaptureQueriesContext(connection) as queries:
                response = client_b.post(action, data=form["fields"], follow=False)
            transaction.set_rollback(True)
        writes = sorted(
            {
                match.group(2)
                for query in queries.captured_queries
                if (match := WRITE.match(query["sql"])) and match.group(2) not in BENIGN_TABLES
            }
        )
        location = response.get("Location", "")
        refused = response.status_code in DENIED or "/accounts/login" in location
        if writes or not refused:
            post_flags.append(
                {
                    "action": action,
                    "status": response.status_code,
                    "location": location,
                    "writes": writes,
                    "fields": form["fields"],
                    "from_page": form["page"],
                }
            )

    return {
        "owner": owner,
        "other": other,
        "pages_crawled_owner": len(pages_a),
        "pages_crawled_other": len(pages_b),
        "object_urls_replayed": len(candidates),
        "object_forms_replayed": len(tried),
        "get_flags": get_flags,
        "post_flags": post_flags,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="var/benchmark/idor-sweep.json")
    options = parser.parse_args()
    results = []
    for owner, other in PAIRS:
        with transaction.atomic():
            result = replay(owner, other)
            transaction.set_rollback(True)
        results.append(result)
        print(
            f"{owner} -> {other}: crawled {result['pages_crawled_owner']}/{result['pages_crawled_other']}, "
            f"replayed {result['object_urls_replayed']} GET / {result['object_forms_replayed']} POST, "
            f"flags {len(result['get_flags'])} GET / {len(result['post_flags'])} POST",
            flush=True,
        )
        for flag in result["get_flags"]:
            print("   GET ", flag)
        for flag in result["post_flags"]:
            print("   POST", flag)
    out = ROOT / options.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print("report:", out)


if __name__ == "__main__":
    main()
