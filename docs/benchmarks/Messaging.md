# Messaging — Section Loop Report

**Date:** 2026-09-13.
**Scope:** SRS §12 and FR-7.1–FR-7.7. Chosen next after Jobs/Applications, because employers start conversations from an application and the section carries privacy rules (INV-4 access grant, block, report).

**Local status: STABLE. Not READY.** Manual-only checks are listed in [`Manual-Verification-Consolidated.md`](Manual-Verification-Consolidated.md).

## Method

- **Journey probes on the development database.** Each ran inside a rolled-back transaction, with the outbox and fan-out run so notifications were read as delivered. Covered:
  - employer opens a thread from an application and sends;
  - candidate reads and replies;
  - empty, script-containing and over-long messages;
  - mute, then a message while muted;
  - report with an invalid reason, then a valid one;
  - block, then the employer tries to send.
- **Access-control probes:** another candidate, another company, the wrong company slug, anonymous visitors, missing CSRF, a malformed thread id.
- **Browser (Playwright Chromium):**
  - axe in light and dark on both inboxes, list and open thread;
  - overflow at 375 and 320 px.
- **Performance:** median of 5 requests, query counts captured.

## Defects found and fixed

| # | Defect | Severity | Reproduced by | Root cause | Fix | Regression test |
|---|---|---|---|---|---|---|
| M1 | A malformed `?thread=` or `thread` form value returned **500** on both inboxes (GET and POST) | High (stability) | Access-control probe | The selector passed the raw string to a UUID filter, which raised `ValidationError` | Selectors return `None` for an invalid id, so the page shows 404 | `MalformedThreadIdTests` (2) |
| M2 | New-message notification read "New message / You have a new message about a role", with no sender, no role, and an empty link (FR-7.4 requires a link to the thread) | Medium | Fan-out on send | Payload carried ids only | Payload now carries the sender's name, the role and a link to the recipient's own inbox with the thread open. Now reads "New message from Fonepay / About Fraud Analyst. Open the conversation to reply." | `NotificationTests` (2) |
| M3 | A 9,000-character message was silently cut to 8,000 | Low / medium | Probe | Truncated with `[:MAX_BODY_LENGTH]` | Refused with "Messages can be up to 8,000 characters. This one is 9,000…"; reply box has `maxlength` | `LengthLimitTests` (2) |
| M4 | Inboxes wider than the screen: candidate 377 px at 375 and 320, employer 339 px at 320 | Medium (responsive) | Playwright overflow scan | The thread list is a grid column, and its truncated one-line titles stopped it shrinking below their width | `min-w-0` on the thread list in both inboxes | `LayoutTests`; rescan: 0 overflow |
| M5 | Unread badges ran one COUNT per thread, both in the inbox and in the navigation badge shown on every page | Medium (performance) | Query capture: 4 identical COUNTs with 2 threads | Per-row `_unread()` calls | One annotated count for the list and one aggregate for the badge; per-thread method kept as fallback | `UnreadQueryTests` (2) |

## Verified working without change

- **Access control:** 10/10 probes. Another candidate or company gets 404 on read, send and block, and no message is written; a candidate opening the employer inbox gets 404; anonymous visitors are redirected; missing CSRF gets 403.
- **Threads:** opening one is idempotent (a second attempt reuses the thread). Messages containing script or HTML are escaped. Opening a thread marks it read.
- **Mute:** silences notifications (0 delivered while muted).
- **Report:** an invalid reason is refused; a valid report is stored for staff.
- **Block:** closes the thread, and the employer's next send is refused. Blocking does not withdraw an application already sent; the page says so.
- **Rate limits:** per company per candidate, and per company per day (existing tests; not re-driven here).

## Measurements

| Screen | Before | After |
|---|---|---|
| Candidate inbox | 48 ms median, 14 queries (2 threads) | 89 ms median, **10 queries** |
| Employer inbox | 52 ms median, 16 queries | 96 ms median, **12 queries** |
| Query growth with thread count | +1 COUNT per thread | constant (test: ≤ +1 going from 1 to 6 threads) |

- **Timings:** the "after" runs were taken while the full test suite was using the same CPU and database. Only the query counts are comparable; the milliseconds are not.
- **Browser:** axe found 0 violations on 8 scans (both inboxes, list and thread, light and dark). Layout at 375 and 320 px showed 0 overflow on 8 checks after the fix.

## Checks

| Check | Result |
|---|---|
| Targeted: messaging, candidate/employer portals, notifications | 429 OK |
| New regression tests | 10 in `apps/messaging/tests/test_loop_regressions.py` |
| Full project suite | Ran 3073 tests in 1266 s — **OK** (`var/benchmark/full-suite-messaging-loop.log`) |
| mypy | clean after fixing 3 type errors introduced in this loop |
| Ruff / migrations | clean on changed files / no drift |

## Remaining in this section

- **Attachments:** attaching files to messages (FR-7.5) is implemented in the service but not offered in either inbox form, so it was not exercised through the UI.
- **Editing and deleting:** both exist in the service with a 15-minute edit window, but no screen exposes them.
- **Staff moderation queue:** reports are stored and `open_reports()` exists; the staff screen was not part of this loop.
- **Rate limits:** not re-driven against Redis in this pass.
- **Message data export (FR-7.6):** belongs with the account and privacy section, not verified here.
- **Manual-only:** screen-reader use of both inboxes and the block/report disclosure; email rendering of the new notification.
