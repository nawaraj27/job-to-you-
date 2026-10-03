"""
Automated accessibility scan of the interview screens with axe-core, in real
browser engines.

This is automated checking only. It finds missing labels, contrast failures,
invalid ARIA and similar rule violations; it cannot tell whether a screen-reader
user can actually complete the journey. That needs a person with a screen reader
(docs/benchmarks/Interview-Manual-Verification.md, section C).

    python scripts/interview_a11y.py --base http://127.0.0.1:8137 --out var/benchmark/a11y-axe.json
"""

from __future__ import annotations

import argparse
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

    from apps.candidates.services import profile as profile_service
    from apps.interviews.models import InterviewKind, InterviewSession, InterviewTemplate
    from apps.interviews.services import engine

    User = get_user_model()

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    candidate = User.objects.get(email="priya.sharma@example.com")
    profile = profile_service.get_or_create_profile(user=candidate)
    template = InterviewTemplate.objects.get(slug="general-practice")
    text = engine.start(candidate=profile, template=template, actor=candidate)
    voice = engine.start(
        candidate=profile, template=template, actor=candidate, kind=InterviewKind.AI_VOICE
    )
    for session in (text, voice):
        engine.next_question(session=session)
    sessions = InterviewSession.objects.filter(candidate=profile)
    done = sessions.filter(state="completed").first()
    live = sessions.filter(kind=InterviewKind.LIVE).first()

    pages = {
        "candidate": [
            ("hub", "/interviews/"),
            ("text question", f"/interviews/{text.public_id}/"),
            ("voice question", f"/interviews/{voice.public_id}/"),
        ],
        "employer": [("employer list", "/employer/fonepay/interviews/")],
    }
    if done:
        pages["candidate"].append(("result", f"/interviews/{done.public_id}/result/"))
    if live:
        pages["candidate"].append(("booked result", f"/interviews/{live.public_id}/result/"))
        pages["candidate"].append(("join page", f"/interviews/{live.public_id}/join/"))
        pages["candidate"].append(
            ("book a time", f"/interviews/book/{live.application.public_id}/")
        )
        pages["employer"].append(
            ("employer detail", f"/employer/fonepay/interviews/{live.public_id}/")
        )
    return {
        "sessions": {
            "candidate": session_for("priya.sharma@example.com"),
            "employer": session_for("hr@fonepay.example.com"),
        },
        "pages": pages,
    }


async def run(base: str, out: Path) -> int:
    from asgiref.sync import sync_to_async
    from axe_playwright_python.async_playwright import Axe
    from playwright.async_api import async_playwright

    plan = await sync_to_async(prepare)()
    host = base.split("//", 1)[1].split(":")[0]
    axe = Axe()
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for scheme in ("light", "dark"):
            for side, pages in plan["pages"].items():
                context = await browser.new_context(
                    color_scheme=scheme, viewport={"width": 1280, "height": 900}
                )
                await context.add_cookies(
                    [
                        {
                            "name": "sessionid",
                            "value": plan["sessions"][side],
                            "domain": host,
                            "path": "/",
                        }
                    ]
                )
                page = await context.new_page()
                for name, path in pages:
                    await page.goto(base + path)
                    await page.evaluate(
                        "document.querySelectorAll('details').forEach(d => d.open = true)"
                    )
                    report = await axe.run(page)
                    violations = report.response.get("violations", [])
                    rows = [
                        {
                            "id": v["id"],
                            "impact": v.get("impact"),
                            "nodes": len(v.get("nodes", [])),
                            "help": v.get("help"),
                            "targets": [n.get("target") for n in v.get("nodes", [])][:3],
                        }
                        for v in violations
                    ]
                    results.append(
                        {
                            "scheme": scheme,
                            "side": side,
                            "page": name,
                            "path": path,
                            "violations": rows,
                            "passes": len(report.response.get("passes", [])),
                        }
                    )
                    summary = (
                        ", ".join(f"{r['id']}({r['impact']}×{r['nodes']})" for r in rows) or "none"
                    )
                    print(
                        f"[{scheme}] {name}: {len(rows)} violation rule(s): {summary}", flush=True
                    )
                await context.close()
        await browser.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    serious = [
        (r["scheme"], r["page"], v["id"])
        for r in results
        for v in r["violations"]
        if v["impact"] in {"serious", "critical"}
    ]
    print(f"\npages scanned: {len(results)}; serious/critical: {len(serious)} {serious}")
    return 1 if serious else 0


def main() -> None:
    import asyncio

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument("--out", default=str(ROOT / "var" / "benchmark" / "a11y-axe.json"))
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.base, Path(args.out))))


if __name__ == "__main__":
    main()
