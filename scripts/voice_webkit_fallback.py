"""
The spoken interview in a browser that cannot record.

Playwright's WebKit build for Windows has no `navigator.mediaDevices`. That is
not Safari — Safari on macOS and iOS can record — but it is a real engine in
the state an unsupported or locked-down browser presents, so it tests the
fallback: the page must say recording is unavailable, offer no button that
fails, and let the candidate type and submit.

    python scripts/voice_webkit_fallback.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from voice_e2e import prepare, turn_state  # noqa: E402


async def main() -> int:
    from asgiref.sync import sync_to_async
    from playwright.async_api import async_playwright

    base = "http://127.0.0.1:8137"
    session_key, sessions = await sync_to_async(prepare)("priya.sharma@example.com")
    sid = sessions[0]
    results = []

    def record(name, ok, detail):
        results.append({"engine": "webkit", "test": name, "passed": bool(ok), "detail": detail})
        print(f"[webkit] {'PASS' if ok else 'FAIL'} {name}: {str(detail)[:200]}", flush=True)

    async with async_playwright() as p:
        browser = await p.webkit.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(
            [{"name": "sessionid", "value": session_key, "domain": "127.0.0.1", "path": "/"}]
        )
        page = await context.new_page()
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        await page.goto(f"{base}/interviews/{sid}/")
        ua = await page.evaluate("navigator.userAgent")
        media = await page.evaluate(
            "!!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)"
        )
        record("engine without media capture", not media, ua)

        panel = "[aria-labelledby=voice-heading]"
        alerts = await page.eval_on_selector_all(
            f"{panel} [role=alert]",
            "els => els.filter(e => e.offsetParent !== null).map(e => e.textContent.trim().replace(/\\s+/g, ' '))",
        )
        buttons = await page.eval_on_selector_all(
            f"{panel} button",
            "els => els.filter(e => e.offsetParent !== null).map(e => e.textContent.trim())",
        )
        record(
            "says recording is unavailable on load",
            any("No microphone could be used" in a for a in alerts),
            alerts,
        )
        record(
            "offers no record button that would fail",
            not any(
                b in buttons
                for b in ("Microphone only", "Turn on microphone and camera", "Start answering")
            ),
            buttons,
        )

        await page.locator("#answer").fill(
            "I rebuilt the nightly reconciliation job and cut it from four hours to thirty minutes."
        )
        # Wait for the POST and its redirect, not the already-loaded page: the
        # first version read the database before the submit had happened.
        async with page.expect_navigation(wait_until="load", timeout=60000):
            await page.get_by_role("button", name="Submit answer").click()
        state = await sync_to_async(turn_state)(sid)
        answered = [t for t in state if t["answer"]]
        record(
            "typed answer submits",
            bool(answered) and "reconciliation" in answered[0]["answer"],
            answered[0] if answered else state,
        )
        record("no console errors", not errors, errors[:3])
        await browser.close()

    out = ROOT / "var" / "benchmark" / "voice-webkit-fallback.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [r for r in results if not r["passed"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
