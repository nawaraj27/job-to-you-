"""
End-to-end browser test of the spoken practice interview.

Drives the real page in real browser engines (Playwright) against the running
dev server, with **fake media devices**: Chromium plays a WAV file into a fake
microphone. This exercises getUserMedia, MediaRecorder, the upload, the real
speech engine, transcript editing, submission and cleanup — everything except a
physical microphone, a real voice and a real person's permission click.

    python scripts/voice_e2e.py --base http://127.0.0.1:8137 --out var/benchmark/voice-e2e.json

Requires: the dev server running, `pip install playwright`, `playwright install
chromium` (and firefox/webkit to test those engines), and demo data seeded.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

SPEECH = ROOT / "var" / "benchmark" / "speech"


def prepare(email: str) -> tuple[str, list[str]]:
    """A Django session for the candidate and fresh spoken practice sessions."""
    import django

    django.setup()
    from importlib import import_module

    from django.conf import settings
    from django.contrib.auth import get_user_model

    from apps.candidates.services import profile as profile_service
    from apps.interviews.models import InterviewKind, InterviewTemplate
    from apps.interviews.services import engine

    user = get_user_model().objects.get(email=email)
    store = import_module(settings.SESSION_ENGINE).SessionStore()
    store["_auth_user_id"] = str(user.pk)
    store["_auth_user_backend"] = settings.AUTHENTICATION_BACKENDS[0]
    store["_auth_user_hash"] = user.get_session_auth_hash()
    store.create()

    profile = profile_service.get_or_create_profile(user=user)
    template = InterviewTemplate.objects.get(slug="general-practice")
    sessions = []
    for _ in range(24):
        session = engine.start(
            candidate=profile, template=template, actor=user, kind=InterviewKind.AI_VOICE
        )
        sessions.append(str(session.public_id))
    return store.session_key, sessions


def silence(path: Path, seconds: int = 6) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000 * seconds)
    return path


def turn_state(public_id: str) -> dict:
    from apps.interviews.models import InterviewTurn

    turns = InterviewTurn.objects.filter(session__public_id=public_id).order_by("order")
    return [{"order": t.order, "answer": t.answer_text, "delivery": t.delivery_meta} for t in turns]


def audio_temp_files() -> set[str]:
    return {
        name
        for name in os.listdir(tempfile.gettempdir())
        if name.endswith((".webm", ".ogg", ".m4a", ".wav", ".audio"))
    }


async def run(base: str, out: Path, engines: list[str]) -> int:
    from asgiref.sync import sync_to_async
    from playwright.async_api import async_playwright

    session_key, sessions = await sync_to_async(prepare)("priya.sharma@example.com")
    host = base.split("//", 1)[1].split(":")[0]
    results: list[dict] = []
    empty = silence(Path(tempfile.gettempdir()) / "careerai-silence.wav")
    temp_before = audio_temp_files()

    def record(engine: str, name: str, ok: bool, detail: dict | str) -> None:
        results.append({"engine": engine, "test": name, "passed": bool(ok), "detail": detail})
        print(f"[{engine}] {'PASS' if ok else 'FAIL'} {name}: {str(detail)[:180]}", flush=True)

    async with async_playwright() as p:
        for engine_name in engines:
            launcher = getattr(p, engine_name)

            async def open_page(
                audio: Path | None,
                *,
                allow: bool = True,
                viewport=None,
                mobile=False,
                engine_name=engine_name,
                launcher=launcher,
            ):
                args = []
                prefs = None
                if engine_name == "chromium":
                    args = ["--use-fake-device-for-media-stream"]
                    if allow:
                        args.append("--use-fake-ui-for-media-stream")
                    if audio is not None:
                        args.append(f"--use-file-for-fake-audio-capture={audio}")
                    if not allow:
                        args.append("--deny-permission-prompts")
                elif engine_name == "firefox":
                    prefs = {
                        "media.navigator.streams.fake": True,
                        "media.navigator.permission.disabled": allow,
                    }
                try:
                    browser = (
                        await launcher.launch(
                            headless=True, args=args or None, firefox_user_prefs=prefs
                        )
                        if engine_name == "firefox"
                        else await launcher.launch(headless=True, args=args or None)
                    )
                except Exception as exc:  # engine not installed
                    return None, None, str(exc).splitlines()[0]
                context_args = {}
                if viewport:
                    context_args["viewport"] = viewport
                if mobile and engine_name != "firefox":
                    context_args.update({"is_mobile": True, "has_touch": True})
                context = await browser.new_context(**context_args)
                if allow and engine_name != "chromium":
                    with contextlib.suppress(Exception):  # not every engine supports it
                        await context.grant_permissions(["microphone", "camera"], origin=base)
                await context.add_cookies(
                    [{"name": "sessionid", "value": session_key, "domain": host, "path": "/"}]
                )
                page = await context.new_page()
                console_errors: list[str] = []
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )
                page.console_errors = console_errors  # type: ignore[attr-defined]
                return browser, page, ""

            async def visible_text(page, selector: str) -> list[str]:
                return await page.eval_on_selector_all(
                    selector,
                    "els => els.filter(e => e.offsetParent !== null).map(e => e.textContent.trim().replace(/\\s+/g, ' '))",
                )

            def next_session() -> str:
                return sessions.pop(0)

            # 1. granted, record, stop, transcribe, edit, submit
            browser, page, error = await open_page(SPEECH / "reconciliation_normal.wav")
            if browser is None:
                record(engine_name, "engine available", False, error)
                continue
            record(
                engine_name, "engine available", True, (await page.evaluate("navigator.userAgent"))
            )
            sid = next_session()
            await page.goto(f"{base}/interviews/{sid}/")
            await page.get_by_role("button", name="Microphone only").click()
            try:
                await page.get_by_role("button", name="Start answering").wait_for(timeout=8000)
                ready = True
            except Exception:
                ready = False
            record(
                engine_name,
                "permission granted -> ready",
                ready,
                await visible_text(
                    page,
                    "[aria-labelledby=voice-heading] [role=status], [aria-labelledby=voice-heading] [role=alert]",
                ),
            )
            if ready:
                t0 = time.monotonic()
                await page.get_by_role("button", name="Start answering").click()
                await page.wait_for_timeout(7000)
                recording_clock = await page.locator(
                    "[aria-labelledby=voice-heading] [role=status] span"
                ).first.text_content()
                await page.get_by_role("button", name="Stop and transcribe").click()
                try:
                    await page.get_by_text("Your answer is in the box below").wait_for(
                        timeout=90000
                    )
                    transcript = await page.locator("#answer").input_value()
                    sealed = await page.locator("#delivery-sealed").input_value()
                    took = round(time.monotonic() - t0 - 7, 1)
                    record(
                        engine_name,
                        "record -> transcribe",
                        bool(transcript.strip()) and bool(sealed),
                        {
                            "clock": recording_clock,
                            "transcribe_s_after_stop": took,
                            "transcript": transcript[:120],
                        },
                    )
                    # 7. refresh after transcription: draft and timings survive
                    await page.reload()
                    restored = await page.locator("#answer").input_value()
                    sealed_after = await page.locator("#delivery-sealed").input_value()
                    record(
                        engine_name,
                        "refresh keeps transcript draft",
                        restored == transcript,
                        restored[:80],
                    )
                    record(
                        engine_name,
                        "refresh keeps sealed timings",
                        bool(sealed_after),
                        "sealed present" if sealed_after else "sealed lost on reload",
                    )
                    await page.locator("#answer").fill(restored + " Edited by the candidate.")
                    await page.get_by_role("button", name="Submit answer").click()
                    await page.wait_for_load_state("load")
                    state = await sync_to_async(turn_state)(sid)
                    answered = [t for t in state if t["answer"]]
                    record(
                        engine_name,
                        "edited transcript submitted with timings",
                        bool(answered)
                        and "Edited by the candidate." in answered[0]["answer"]
                        and bool(answered[0]["delivery"]),
                        answered[0] if answered else state,
                    )
                except Exception as exc:
                    record(engine_name, "record -> transcribe", False, repr(exc)[:200])
            record(
                engine_name,
                "no console errors (granted flow)",
                not page.console_errors,
                page.console_errors[:3],
            )
            await browser.close()

            # 2. camera preview
            browser, page, _ = await open_page(SPEECH / "reconciliation_normal.wav")
            await page.goto(f"{base}/interviews/{next_session()}/")
            await page.get_by_role("button", name="Turn on microphone and camera").click()
            await page.get_by_role("button", name="Start answering").wait_for(timeout=8000)
            tracks = await page.evaluate(
                "() => { const v = document.querySelector('video'); return v && v.srcObject ? {video: v.srcObject.getVideoTracks().length, audio: v.srcObject.getAudioTracks().length, shown: v.offsetParent !== null} : null }"
            )
            record(
                engine_name,
                "camera preview shown (local only)",
                bool(tracks and tracks["video"] and tracks["shown"]),
                tracks,
            )
            await browser.close()

            # 3. permission denied
            browser, page, _ = await open_page(None, allow=False)
            await page.goto(f"{base}/interviews/{next_session()}/")
            await page.get_by_role("button", name="Microphone only").click()
            await page.wait_for_timeout(3000)
            alerts = await visible_text(page, "[aria-labelledby=voice-heading] [role=alert]")
            record(
                engine_name,
                "permission denied -> recovery guidance",
                any("blocked" in a or "No microphone" in a for a in alerts),
                alerts,
            )
            await browser.close()

            if engine_name != "chromium":
                continue  # fake audio files are a Chromium feature

            # 4. empty audio
            browser, page, _ = await open_page(empty)
            await page.goto(f"{base}/interviews/{next_session()}/")
            await page.get_by_role("button", name="Microphone only").click()
            await page.get_by_role("button", name="Start answering").click()
            await page.wait_for_timeout(4000)
            await page.get_by_role("button", name="Stop and transcribe").click()
            try:
                await (
                    page.locator("[aria-labelledby=voice-heading] [role=alert]")
                    .filter(has_text="speech")
                    .first.wait_for(timeout=60000)
                )
                ok = True
            except Exception:
                ok = False
            record(
                engine_name,
                "silent recording -> 'no speech heard'",
                ok,
                await visible_text(page, "[aria-labelledby=voice-heading] [role=alert]"),
            )
            await browser.close()

            # 5. noisy audio
            browser, page, _ = await open_page(SPEECH / "reconciliation_noisy.wav")
            await page.goto(f"{base}/interviews/{next_session()}/")
            await page.get_by_role("button", name="Microphone only").click()
            await page.get_by_role("button", name="Start answering").click()
            await page.wait_for_timeout(8000)
            await page.get_by_role("button", name="Stop and transcribe").click()
            try:
                await page.get_by_text("Your answer is in the box below").wait_for(timeout=90000)
                text = await page.locator("#answer").input_value()
                record(
                    engine_name, "noisy recording transcribed", len(text.split()) > 5, text[:120]
                )
            except Exception as exc:
                record(engine_name, "noisy recording transcribed", False, repr(exc)[:160])
            await browser.close()

            # 6. upload failure (network) and server failure (503)
            for label, handler in (
                ("network failure -> retry guidance", lambda route: route.abort()),
                (
                    "server 503 -> type instead",
                    lambda route: route.fulfill(
                        status=503,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "error": "Speech recognition is not available right now. Type your answer below."
                            }
                        ),
                    ),
                ),
            ):
                browser, page, _ = await open_page(SPEECH / "reconciliation_normal.wav")
                await page.route("**/transcribe/", handler)
                await page.goto(f"{base}/interviews/{next_session()}/")
                await page.get_by_role("button", name="Microphone only").click()
                await page.get_by_role("button", name="Start answering").click()
                await page.wait_for_timeout(3000)
                await page.get_by_role("button", name="Stop and transcribe").click()
                await page.wait_for_timeout(2500)
                alerts = await visible_text(page, "[aria-labelledby=voice-heading] [role=alert]")
                retry = await page.get_by_role("button", name="Record again").is_visible()
                typing = await page.locator("#answer").is_editable()
                record(engine_name, label, bool(alerts) and retry and typing, alerts)
                await browser.close()

            # 8. keyboard only
            browser, page, _ = await open_page(SPEECH / "reconciliation_normal.wav")
            await page.goto(f"{base}/interviews/{next_session()}/")
            focus_js = (
                "() => { const e = document.activeElement; if (!e) return null; const s = getComputedStyle(e);"
                " return {name: (e.textContent || e.getAttribute('aria-label') || e.id || '').trim().slice(0, 40),"
                " ring: (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || !/0px 0px 0px 0px/.test(s.boxShadow) && s.boxShadow !== 'none'}; }"
            )
            stops = []
            for _ in range(60):
                await page.keyboard.press("Tab")
                here = await page.evaluate(focus_js)
                stops.append(here)
                if here and here["name"] == "Microphone only":
                    break
            reached = bool(stops) and stops[-1] and stops[-1]["name"] == "Microphone only"
            ring_on_recorder = bool(reached and stops[-1]["ring"])
            stop_visible = False
            ring_on_start = False
            if reached:
                await page.keyboard.press("Enter")
                await page.get_by_role("button", name="Start answering").wait_for(timeout=8000)
                # Focus stays where it was; the next Tab has to reach the new button.
                for _ in range(10):
                    await page.keyboard.press("Tab")
                    here = await page.evaluate(focus_js)
                    if here and here["name"] == "Start answering":
                        ring_on_start = here["ring"]
                        await page.keyboard.press("Space")
                        break
                await page.wait_for_timeout(3000)
                stop_visible = await page.get_by_role(
                    "button", name="Stop and transcribe"
                ).is_visible()
            record(
                engine_name,
                "keyboard: reach and operate recorder, visible focus",
                bool(reached and stop_visible and ring_on_recorder and ring_on_start),
                {
                    "tabs_to_recorder": len(stops),
                    "ring_on_recorder": ring_on_recorder,
                    "ring_on_start": ring_on_start,
                    "stop_visible": stop_visible,
                },
            )
            await browser.close()

            # 9. mobile layout
            browser, page, _ = await open_page(
                SPEECH / "reconciliation_normal.wav",
                viewport={"width": 375, "height": 812},
                mobile=True,
            )
            await page.goto(f"{base}/interviews/{next_session()}/")
            await page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
            layout = await page.evaluate(
                "() => { const d = document.documentElement; const small = [...document.querySelectorAll('[aria-labelledby=voice-heading] button')].filter(b => b.offsetParent).map(b => Math.round(b.getBoundingClientRect().height)); return {width: d.clientWidth, overflow: d.scrollWidth > d.clientWidth, button_heights: small}; }"
            )
            record(
                engine_name,
                "mobile 375px: no overflow, touch-size buttons",
                (not layout["overflow"]) and all(h >= 36 for h in layout["button_heights"]),
                layout,
            )
            await browser.close()

    leftover = sorted(audio_temp_files() - temp_before)
    record("server", "no audio temp files left behind", not leftover, leftover)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [r for r in results if not r["passed"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed; report {out}")
    return 1 if failed else 0


def main() -> None:
    import asyncio

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8137")
    parser.add_argument("--out", default=str(ROOT / "var" / "benchmark" / "voice-e2e.json"))
    parser.add_argument("--engines", default="chromium,firefox,webkit")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.base, Path(args.out), args.engines.split(","))))


if __name__ == "__main__":
    main()
