"""
Automated accessibility and layout scan of the Jobs/Applications screens.

axe-core in Chromium, light and dark, plus horizontal overflow at 320, 375 and
1280 px. Automated checking only: it cannot say whether a screen-reader user
can complete the journey.

    python scripts/applications_a11y.py --out var/benchmark/applications-a11y.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings
    from django.contrib.auth import get_user_model

    from apps.applications.models import Application
    from apps.jobs.models import Job

    User = get_user_model()

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    priya_app = Application.objects.filter(
        candidate__user__email="priya.sharma@example.com"
    ).first()
    fonepay_app = Application.objects.filter(job__company__slug="fonepay").first()
    applied = set(
        Application.objects.filter(candidate__user__email="priya.sharma@example.com").values_list(
            "job_id", flat=True
        )
    )
    open_job = Job.objects.filter(status="published").exclude(pk__in=applied).first()
    job = Job.objects.filter(company__slug="fonepay", status="published").first()
    return {
        "sessions": {
            "candidate": session_for("priya.sharma@example.com"),
            "employer": session_for("hr@fonepay.example.com"),
        },
        "pages": {
            "candidate": [
                ("job search", "/jobs/?q=engineer"),
                ("job detail", f"/jobs/{open_job.slug}/"),
                ("apply", f"/jobs/{open_job.slug}/apply/"),
                ("saved jobs", "/jobs/saved/"),
                ("matched jobs", "/jobs/matched/"),
                ("applications", "/applications/"),
                ("application detail", f"/applications/{priya_app.public_id}/"),
                ("visibility", "/applications/visibility/"),
            ],
            "employer": [
                ("applicant board", "/employer/fonepay/applicants/"),
                ("applicant detail", f"/employer/fonepay/applicants/{fonepay_app.public_id}/"),
                ("pipeline settings", "/employer/fonepay/pipeline/"),
                ("talent pools", "/employer/fonepay/pools/"),
                ("job list", "/employer/fonepay/jobs/"),
                ("job detail", f"/employer/fonepay/jobs/{job.public_id}/"),
                ("job edit", f"/employer/fonepay/jobs/{job.public_id}/edit/"),
                ("new job", "/employer/fonepay/jobs/new/"),
            ],
        },
    }


OVERFLOW_JS = """() => {
  const d = document.documentElement;
  const wide = [...document.querySelectorAll('main *')].filter(e => {
    if (!e.offsetParent) return false;
    const r = e.getBoundingClientRect();
    if (r.right <= d.clientWidth + 1) return false;
    for (let p = e.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden') return false;
    }
    return true;
  }).map(e => e.tagName.toLowerCase() + '.' + String(e.className).slice(0, 40));
  return {overflow: d.scrollWidth > d.clientWidth, width: d.clientWidth, scroll: d.scrollWidth, wide: wide.slice(0, 4)};
}"""


async def run(base: str, out: Path) -> int:
    from asgiref.sync import sync_to_async
    from axe_playwright_python.async_playwright import Axe
    from playwright.async_api import async_playwright

    plan = await sync_to_async(prepare)()
    axe = Axe()
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for side, pages in plan["pages"].items():
            for scheme, width in (("light", 1280), ("dark", 1280), ("light", 375), ("light", 320)):
                context = await browser.new_context(
                    color_scheme=scheme, viewport={"width": width, "height": 900}
                )
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
                page.on(
                    "console",
                    lambda m, errors=errors: errors.append(m.text) if m.type == "error" else None,
                )
                for name, path in pages:
                    response = await page.goto(base + path)
                    row = {
                        "side": side,
                        "page": name,
                        "path": path,
                        "scheme": scheme,
                        "width": width,
                        "status": response.status if response else None,
                    }
                    if width == 1280:
                        report = await axe.run(page)
                        row["violations"] = [
                            {
                                "id": v["id"],
                                "impact": v.get("impact"),
                                "nodes": len(v.get("nodes", [])),
                                "targets": [n.get("target") for n in v.get("nodes", [])][:3],
                            }
                            for v in report.response.get("violations", [])
                        ]
                    else:
                        row["layout"] = await page.evaluate(OVERFLOW_JS)
                    results.append(row)
                    detail = (
                        ", ".join(
                            f"{v['id']}({v['impact']}x{v['nodes']})"
                            for v in row.get("violations", [])
                        )
                        if width == 1280
                        else json.dumps(row["layout"])
                    )
                    print(
                        f"[{side} {scheme} {width}] {name} {row['status']}: {detail or 'clean'}",
                        flush=True,
                    )
                row_errors = [e for e in errors if "favicon" not in e]
                if row_errors:
                    print(f"  console errors ({side} {scheme} {width}): {row_errors[:3]}")
                await context.close()
        await browser.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    serious = [
        (r["side"], r["page"], r["scheme"], v["id"])
        for r in results
        for v in r.get("violations", [])
        if v["impact"] in {"serious", "critical"}
    ]
    overflow = [
        (r["side"], r["page"], r["width"]) for r in results if r.get("layout", {}).get("overflow")
    ]
    print(f"\nscans: {len(results)}; serious/critical: {serious}; overflow: {overflow}")
    return 1 if serious or overflow else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument("--out", default=str(ROOT / "var" / "benchmark" / "applications-a11y.json"))
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
