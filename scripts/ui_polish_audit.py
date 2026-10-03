"""
UI polish audit: decorations, borders, overflow and clipping, with screenshots.

Manual testing reported "strange underlines", "dashed labels", "weird borders"
and "text crossing lines" (salary, location, verified, status badges). This
scans the rendered pages the demo personas use, in a real browser, and writes a
screenshot of each one as evidence:

  * **decoration** — any element other than a link carrying an underline,
    overline or line-through it did not ask for;
  * **dashed** — dashed or dotted borders, listed with their classes so each
    can be checked against the design system (only `.chip-gap` is meant to be);
  * **clipped** — text cut off by its box without an ellipsis;
  * **overflow** — the page scrolling sideways.

    python scripts/ui_polish_audit.py --out var/benchmark/ui-polish-audit.json
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

PROBE = r"""
() => {
  const out = {decoration: [], dashed: [], clipped: [], overflow: false};
  const own = (el) => [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
  const describe = (el) => (el.tagName.toLowerCase() + '.' + [...el.classList].join('.')).slice(0, 90)
      + ' "' + (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40) + '"';
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) continue;
    const line = cs.textDecorationLine;
    if (line && line !== 'none' && own(el) && !el.closest('a, u, ins, del, s, abbr, [data-decoration-ok]')) {
      out.decoration.push(describe(el) + ' ' + line);
    }
    for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
      const style = cs['border' + side + 'Style'];
      if ((style === 'dashed' || style === 'dotted') && parseFloat(cs['border' + side + 'Width']) > 0) {
        out.dashed.push(describe(el) + ' ' + style);
        break;
      }
    }
    if (own(el) && (cs.overflowX === 'hidden' || cs.overflow === 'hidden') && cs.textOverflow !== 'ellipsis'
        && el.scrollWidth > el.clientWidth + 2 && !el.classList.contains('sr-only')) {
      out.clipped.push(describe(el));
    }
  }
  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  out.dashed = [...new Set(out.dashed)];
  return out;
}
"""


def prepare() -> dict:
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings

    from apps.accounts.models import User
    from apps.companies.models import Company
    from apps.core import demo_personas as personas
    from apps.jobs.models import Job, JobStatus
    from apps.resumes.models import ResumeDocument

    def session_for(email: str) -> str:
        user = User.objects.get(email=email)
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        store["_auth_user_id"] = str(user.pk)
        store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
        store["_auth_user_hash"] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    document = ResumeDocument.objects.alive().get(
        profile__user__email=personas.CANDIDATE_EMAIL, name="Maya Karki CV"
    )
    company = Company.objects.get(name=personas.COMPANY)
    job = Job.objects.filter(company=company, status=JobStatus.PUBLISHED).order_by("pk").first()
    slug = company.slug
    return {
        "candidate": (
            session_for(personas.CANDIDATE_EMAIL),
            [
                ("home", "/dashboard/"),
                ("jobs", "/jobs/"),
                ("job detail", f"/jobs/{job.slug}/"),
                ("matched", "/jobs/matched/"),
                ("company page", f"/jobs/company/{slug}/"),
                ("uploads", "/cv/upload/"),
                ("progress", "/cv/upload/processing/"),
                ("analysis", f"/cv/analysis/?document={document.public_id}"),
                ("guided", f"/cv/builder/{document.public_id}/guided/"),
                ("applications", "/applications/"),
                ("interviews", "/interviews/"),
                ("notifications", "/notifications/"),
                ("notification settings", "/settings/notifications/"),
            ],
        ),
        "employer": (
            session_for(personas.EMPLOYER_EMAIL),
            [
                ("overview", f"/employer/{slug}/"),
                ("jobs", f"/employer/{slug}/jobs/"),
                ("applicants", f"/employer/{slug}/applicants/"),
                ("interviews", f"/employer/{slug}/interviews/"),
                ("updates", f"/employer/{slug}/updates/"),
                ("verification", f"/employer/{slug}/verification/"),
            ],
        ),
        "staff": (
            session_for(personas.STAFF_EMAIL),
            [("verification queue", "/staff/verification/")],
        ),
    }


async def run(base: str, out: Path, shots: Path) -> int:
    from playwright.async_api import async_playwright

    plan = await asyncio.to_thread(prepare)
    shots.mkdir(parents=True, exist_ok=True)
    rows = []
    async with async_playwright() as play:
        browser = await play.chromium.launch()
        for persona, (session, pages) in plan.items():
            for width, height in ((1280, 900), (375, 812)):
                context = await browser.new_context(viewport={"width": width, "height": height})
                await context.add_cookies(
                    [{"name": "sessionid", "value": session, "domain": "127.0.0.1", "path": "/"}]
                )
                page = await context.new_page()
                for name, path in pages:
                    response = await page.goto(base + path, wait_until="networkidle")
                    found = await page.evaluate(PROBE)
                    shot = shots / f"{persona}-{name.replace(' ', '-')}-{width}.png"
                    await page.screenshot(path=str(shot), full_page=True)
                    row = {
                        "persona": persona,
                        "page": name,
                        "path": path,
                        "width": width,
                        "status": response.status if response else None,
                        "screenshot": str(shot.relative_to(ROOT)),
                        **found,
                    }
                    rows.append(row)
                    print(
                        f"[{persona} {width}] {name} {row['status']}: "
                        f"decoration={len(found['decoration'])} dashed={len(found['dashed'])} "
                        f"clipped={len(found['clipped'])} overflow={found['overflow']}"
                    )
                await context.close()
        await browser.close()
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    problems = [r for r in rows if r["decoration"] or r["clipped"] or r["overflow"]]
    print(f"pages: {len(rows)}; with decoration/clipping/overflow: {len(problems)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument("--out", default=str(ROOT / "var" / "benchmark" / "ui-polish-audit.json"))
    parser.add_argument("--shots", default=str(ROOT / "var" / "benchmark" / "screens"))
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.base, Path(args.out), Path(args.shots))))


if __name__ == "__main__":
    main()
