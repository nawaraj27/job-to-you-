"""
axe-core, overflow and console scan for the Kaarya refinement pages.

Every page at 320, 375, 768 and 1280 px, each in light and dark: axe-core
(serious and critical counted), horizontal overflow, console errors and any
failed (4xx/5xx) request the page makes. Automated checking only.

  * visitor: home, pricing, the job list with each verification filter,
    sign-in and sign-up;
  * candidate (`--email`): dashboard, CV hub, the Assisted CV start screen and
    a conversation, a template preview, the plan page, matched jobs,
    practice interviews;
  * employer (`demo.employer@example.com`): jobs and billing.

    python scripts/kaarya_a11y.py --email someone@example.test \
        --out var/benchmark/kaarya/a11y.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import applications_a11y as runner  # noqa: E402

WIDTHS = (320, 375, 768, 1280)
SCHEMES = ("light", "dark")
EMPLOYER = "demo.employer@example.com"


def _session(email: str) -> str:
    from importlib import import_module

    from django.conf import settings

    from apps.accounts.models import User

    user = User.objects.get(email=email)
    store = import_module(settings.SESSION_ENGINE).SessionStore()
    store["_auth_user_id"] = str(user.pk)
    store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
    store["_auth_user_hash"] = user.get_session_auth_hash()
    store.create()
    return store.session_key


def prepare(email: str) -> dict:
    import django

    django.setup()
    from django.urls import reverse

    from apps.companies.models import CompanyMembership
    from apps.resumes.models import ResumeDocument

    candidate = [
        ("dashboard", reverse("dashboard:index")),
        ("cv hub", reverse("resume:builder")),
        ("assisted start", reverse("resume:ai") + "?new=1"),
        ("template preview", reverse("resume:template_detail", kwargs={"slug": "meridian"})),
        ("plan", reverse("dashboard:plan")),
        ("matched jobs", reverse("jobs:recommended")),
        ("interviews", reverse("interviews:hub")),
    ]
    document = (
        ResumeDocument.objects.filter(profile__user__email=email, builder_mode="assistant")
        .order_by("-updated_at")
        .first()
    )
    if document is not None:
        candidate.insert(
            3,
            (
                "assisted conversation",
                reverse("resume:assistant", kwargs={"document_id": document.public_id}),
            ),
        )
    slug = (
        CompanyMembership.objects.filter(user__email=EMPLOYER)
        .values_list("company__slug", flat=True)
        .first()
    )
    return {
        "sessions": {"visitor": "", "candidate": _session(email), "employer": _session(EMPLOYER)},
        "pages": {
            "visitor": [
                ("home", "/"),
                ("pricing", reverse("core:pricing")),
                ("jobs", reverse("jobs:list")),
                ("jobs verified", reverse("jobs:list") + "?verification=verified"),
                ("jobs unverified", reverse("jobs:list") + "?verification=unverified"),
                ("sign in", reverse("accounts:login")),
                ("sign up", reverse("accounts:signup")),
            ],
            "candidate": candidate,
            "employer": [
                ("jobs", f"/employer/{slug}/jobs/"),
                ("billing", f"/employer/{slug}/billing/"),
            ],
        },
    }


async def run(base: str, out: Path, email: str) -> int:
    from asgiref.sync import sync_to_async
    from axe_playwright_python.async_playwright import Axe
    from playwright.async_api import async_playwright

    plan = await sync_to_async(prepare)(email)
    axe = Axe()
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for side, pages in plan["pages"].items():
            for scheme in SCHEMES:
                for width in WIDTHS:
                    context = await browser.new_context(
                        color_scheme=scheme, viewport={"width": width, "height": 900}
                    )
                    if plan["sessions"][side]:
                        await context.add_cookies(
                            [
                                {
                                    "name": "sessionid",
                                    "value": plan["sessions"][side],
                                    "domain": "127.0.0.1",
                                    "path": "/",
                                }
                            ]
                        )
                    page = await context.new_page()
                    errors: list[str] = []
                    failed: list[str] = []
                    page.on(
                        "console",
                        lambda m, e=errors: e.append(m.text) if m.type == "error" else None,
                    )
                    page.on(
                        "response",
                        lambda r, f=failed: (
                            f.append(f"{r.status} {r.url}") if r.status >= 400 else None
                        ),
                    )
                    for name, path in pages:
                        errors.clear()
                        failed.clear()
                        response = await page.goto(base + path, wait_until="networkidle")
                        report = await axe.run(page)
                        row = {
                            "side": side,
                            "page": name,
                            "path": path,
                            "scheme": scheme,
                            "width": width,
                            "status": response.status if response else None,
                            "violations": [
                                {
                                    "id": v["id"],
                                    "impact": v.get("impact"),
                                    "nodes": len(v.get("nodes", [])),
                                    "targets": [n.get("target") for n in v.get("nodes", [])][:3],
                                }
                                for v in report.response.get("violations", [])
                            ],
                            "layout": await page.evaluate(runner.OVERFLOW_JS),
                            "console": [e for e in errors if "favicon" not in e],
                            "failed": list(failed),
                        }
                        results.append(row)
                        bad = [
                            f"{v['id']}({v['impact']}x{v['nodes']})"
                            for v in row["violations"]
                            if v["impact"] in {"serious", "critical"}
                        ]
                        flags = []
                        if bad:
                            flags.append("axe " + ", ".join(bad))
                        if row["layout"]["overflow"]:
                            flags.append(f"overflow {row['layout']}")
                        if row["console"]:
                            flags.append(f"console {row['console'][:2]}")
                        if row["failed"]:
                            flags.append(f"failed {row['failed'][:3]}")
                        print(
                            f"[{side} {scheme} {width}] {name} {row['status']}: "
                            f"{'; '.join(flags) or 'clean'}",
                            flush=True,
                        )
                    await context.close()
        await browser.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    serious = sorted(
        {
            (r["side"], r["page"], r["scheme"], v["id"])
            for r in results
            for v in r["violations"]
            if v["impact"] in {"serious", "critical"}
        }
    )
    overflow = [(r["side"], r["page"], r["width"]) for r in results if r["layout"]["overflow"]]
    console = [(r["side"], r["page"], r["width"]) for r in results if r["console"]]
    failed = [(r["side"], r["page"], r["failed"][:2]) for r in results if r["failed"]]
    print(
        f"\nscans: {len(results)}; serious/critical: {serious}; overflow: {overflow}; "
        f"console errors: {console}; failed requests: {failed}"
    )
    return 1 if serious or overflow or console or failed else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--out", default=str(runner.ROOT / "var" / "benchmark" / "kaarya" / "a11y.json")
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.base, Path(args.out), args.email)))


if __name__ == "__main__":
    main()
