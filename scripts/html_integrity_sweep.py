"""
Rendered-HTML integrity sweep: what an axe scan of a handful of pages can miss.

Crawls the site as several personas (development database, rolled back) and
checks every HTML page for: duplicate `id`s; `for`, `aria-labelledby`,
`aria-describedby` and `aria-controls` pointing at an `id` that is not on the
page; `<img>` with no `alt`; buttons and links with no accessible name; debug
leftovers in the text. Prints each finding with the page it was on.

    python scripts/html_integrity_sweep.py --out var/benchmark/html-integrity.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.test import Client  # noqa: E402

from apps.accounts import mfa_policy  # noqa: E402
from apps.accounts.models import User  # noqa: E402
from apps.companies.models import CompanyMembership  # noqa: E402

mfa_policy.surface_requires = lambda request: False  # type: ignore[assignment]

PERSONAS = {
    "anonymous": None,
    "candidate": "sita.rai@example.com",
    "demo_candidate": "demo.candidate@example.com",
    "owner": "hr@fonepay.example.com",
    "demo_employer": "demo.employer@example.com",
    "staff": "ops@careerai.example.com",
}
MAX_PAGES = 260
SKIP = re.compile(r"/logout|/static/|/media/|/admin/|/impersonat|/files/|/export|/download|/print/")
DEBUG_TEXT = re.compile(r"\bTODO\b|\bFIXME\b|\bXXX\b|lorem ipsum|DEBUG:|console\.log|\{\{|\{%")
REFERENCES = ("for", "aria-labelledby", "aria-describedby", "aria-controls", "aria-errormessage")


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: Counter[str] = Counter()
        self.refs: list[tuple[str, str, str]] = []
        self.hrefs: list[str] = []
        self.imgs_without_alt: list[str] = []
        self.unnamed: list[str] = []
        self._stack: list[dict] = []
        self.text: list[str] = []
        self._skip_text = 0

    def handle_starttag(self, tag, attrs):
        attrs = {key: (value or "") for key, value in attrs}
        if attrs.get("id"):
            self.ids[attrs["id"]] += 1
        for key in REFERENCES:
            if attrs.get(key) and not (key == "for" and tag != "label"):
                for target in attrs[key].split():
                    self.refs.append((tag, key, target))
        if tag == "a" and attrs.get("href"):
            self.hrefs.append(attrs["href"])
        if tag == "img" and "alt" not in attrs:
            self.imgs_without_alt.append(attrs.get("src", "")[:80])
        if tag in ("script", "style"):
            self._skip_text += 1
        if tag in ("button", "a") and "href" in attrs or tag == "button":
            named = bool(
                attrs.get("aria-label") or attrs.get("aria-labelledby") or attrs.get("title")
            )
            self._stack.append({"tag": tag, "named": named, "text": "", "attrs": attrs})
        elif (
            self._stack and tag in ("img", "svg") and (attrs.get("alt") or attrs.get("aria-label"))
        ):
            self._stack[-1]["named"] = True

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_text:
            self._skip_text -= 1
        if self._stack and self._stack[-1]["tag"] == tag:
            element = self._stack.pop()
            if not element["named"] and not element["text"].strip():
                label = (
                    element["attrs"].get("class", "")[:40] or element["attrs"].get("href", "")[:40]
                )
                self.unnamed.append(f"<{tag} {label}>")

    def handle_data(self, data):
        if self._skip_text:
            return
        self.text.append(data)
        for element in self._stack:
            element["text"] += data


def starts(user) -> list[str]:
    if user is None:
        return ["/", "/jobs/", "/accounts/login/", "/accounts/signup/", "/pricing/"]
    urls = [
        "/dashboard/",
        "/cv/builder/",
        "/applications/",
        "/interviews/",
        "/messages/",
        "/notifications/",
        "/settings/",
        "/jobs/",
    ]
    for membership in CompanyMembership.objects.filter(user=user).select_related("company"):
        urls.append(f"/employer/{membership.company.slug}/")
    if user.is_staff or hasattr(user, "staff_role"):
        urls.append("/staff/")
    return urls


def sweep(name: str, email: str | None) -> dict:
    client = Client()
    user = User.objects.get(email=email) if email else None
    if user:
        client.force_login(user)
    seen: set[str] = set()
    queue = deque(starts(user))
    findings: list[dict] = []
    pages = 0
    while queue and pages < MAX_PAGES:
        url = queue.popleft()
        path = urlsplit(url).path
        if url in seen or SKIP.search(path):
            continue
        seen.add(url)
        try:
            response = client.get(url)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            findings.append({"page": url, "kind": "exception", "detail": repr(exc)[:200]})
            continue
        if response.status_code != 200 or "text/html" not in response.get("Content-Type", ""):
            continue
        pages += 1
        page = Page()
        page.feed(response.content.decode("utf-8", "replace"))
        for element_id, count in page.ids.items():
            if count > 1:
                findings.append(
                    {"page": url, "kind": "duplicate-id", "detail": f"{element_id} x{count}"}
                )
        for tag, key, target in page.refs:
            if target not in page.ids:
                findings.append(
                    {"page": url, "kind": "dangling-reference", "detail": f"<{tag} {key}={target}>"}
                )
        for src in page.imgs_without_alt:
            findings.append({"page": url, "kind": "img-without-alt", "detail": src})
        for element in page.unnamed:
            findings.append({"page": url, "kind": "no-accessible-name", "detail": element})
        text = " ".join(page.text)
        for match in DEBUG_TEXT.finditer(text):
            findings.append(
                {
                    "page": url,
                    "kind": "debug-text",
                    "detail": text[max(0, match.start() - 40) : match.end() + 40],
                }
            )
        for href in page.hrefs:
            target = urljoin(url, href)
            parts = urlsplit(target)
            if (
                parts.netloc in ("", "testserver")
                and parts.path.startswith("/")
                and not parts.fragment
            ):
                full = parts.path + (f"?{parts.query}" if parts.query else "")
                if full not in seen:
                    queue.append(full)
    return {"persona": name, "pages": pages, "findings": findings}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="var/benchmark/html-integrity.json")
    options = parser.parse_args()
    results = []
    for name, email in PERSONAS.items():
        with transaction.atomic():
            result = sweep(name, email)
            transaction.set_rollback(True)
        results.append(result)
        kinds = Counter(f["kind"] for f in result["findings"])
        print(f"{name}: {result['pages']} pages, findings {dict(kinds)}", flush=True)
    out = ROOT / options.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print("report:", out)


if __name__ == "__main__":
    main()
