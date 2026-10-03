# Notification Centre and Preferences — Section Loop Report

> **Second pass, same day: delivery rules looped.** The first pass covered the feed and preferences only. The second covered the weekly digest, the daily cap and quiet hours; see "Second pass" at the end. FR-11.3 bounce ingestion remains blocked.

**Date:** 2026-09-14.
**Scope looped:** FR-11.1 (six groups × two channels; a muted event is never delivered), FR-11.2 (in-app feed: unread counts, mark read, filter by kind, deep links), the preferences screen from both sides, and the notification this platform added in the employer loop (`company.verification_reviewed`).
**Not looped:** FR-11.3 suppression ingestion (blocked, below), FR-11.4 quiet hours and caps beyond reading the code, FR-11.5 weekly digest cadence, FR-11.7 burst collapsing (existing tests only).

**Local status: feed and preferences STABLE. Section NOT READY.** Manual-only checks: [`Manual-Verification-Consolidated.md`](Manual-Verification-Consolidated.md) row 2.5g, plus 1.3b and 1.3g from earlier loops.

**Why this section came next:** the employer loop added a notification that tells a company a reviewer needs more information. That is only as good as the feed it lands in and the settings that govern it, and FR-11.1/11.2/11.3 are Musts.

## Method

- **Real app** (dev server 8137), as the employer owner created in the employer loop: `/settings/notifications/` and `/notifications/`, before and after the fixes.
- **Probe on the development database** (`notification_probe.py`, scratchpad, rolled back): filtered "Mark all read"; malformed `page`, `kind`, `unread` parameters; an unknown notification id; the muting note against the event registry; employer wording; whether the settings screen creates a candidate profile.
- **Browser scan:** `scripts/notifications_a11y.py` — feed (all, unread, empty filter) and preferences, for the employer owner and the demo candidate; axe light and dark at 1280 px, overflow at 375 and 320 px. 32 scans.

## Defects found and fixed

| # | Defect | Severity | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| N1 | The preferences screen promised that "an interview time, a withdrawn offer — is sent regardless". **False:** every interview and offer notice belongs to Application updates, and a switched-off group is never delivered (FR-11.1). A person could mute it trusting the promise and miss an interview | High (misleading copy with a real consequence) | Copy written against a rule the delivery code never had | The note now says interview times, offers and stage changes are part of Application updates and stop if both its channels are off. The rule was left alone: FR-11.1 is explicit | `MutingCopyTests` (asserts every audience-bearing `interview.`, `application.`, `offer.` event is in that group, and the page says so) |
| N2 | Feed filter chips: axe **critical** `aria-allowed-attr` on every feed variant (3 nodes employer, 5 candidate). The current filter was never announced | Critical (accessibility) | `aria-selected` on plain links inside `role="group"` | `aria-current="page"` on the active chip only; the segmented style already reads it | `FeedFilterSemanticsTests`; rescan clean |
| N3 | "Mark all read" on a list filtered to one kind marked **every** unread notification read | Medium (lost unread items) | The button sent the filter; `mark_all_read` ignored it | `mark_all_read(kind=…)` reads only visible, unread rows of that kind; the view passes the filter | `FilteredMarkAllReadTests` (4) |
| N4 | An employer's preferences described candidate events ("New job matches", "Messages from employers") | Medium (wrong settings) | `WORDING` had an employer column; `rows_for` was always called with `role="candidate"` | `role_for(user)`: membership → employer wording, candidate profile → candidate wording, both → combined label and "As a job seeker… As an employer…". The save message uses the same wording | `PreferenceWordingTests` (5) |

## Verified working without change

- Malformed `page`, `kind` and `unread` values render the feed (200); an unknown notification id returns to the list.
- Opening the notification settings creates no candidate profile for an employer-only account.
- Muted groups are removed from the feed and the unread count (existing tests; unchanged).
- The employer loop's verification notification appears in the owner's feed with the reviewer's reason and opens the verification screen.

## Measurements

| Item | Result |
|---|---|
| Probe | Before: 9/11 PASS (filtered mark-all-read, employer wording). The copy check passed before only because it looked for an event name that does not exist (`interview.booked`); corrected to scan the registry. After: 11/11 |
| axe | 32 scans. Before: 12 critical results (`aria-allowed-attr`, every feed page, both sides, light and dark). After: 0 |
| Overflow | 0 at 375 and 320 px |

## Checks

| Check | Result |
|---|---|
| Targeted: `apps.notifications portals.candidate portals.employer apps.companies apps.accounts` | **538 OK** (`var/benchmark/targeted-notifications-loop.log`) |
| New regression tests | 11: `apps/notifications/tests/test_centre_loop_regressions.py` |
| Full project suite | Ran **3165** tests in 486 s — **OK** (`var/benchmark/full-suite-notifications-loop.log`; 3154 after the employer loop + 11 new) |
| mypy / Ruff / migrations | mypy `apps` (357 files) clean; Ruff clean on changed files; `makemigrations --check --dry-run`: no changes |

## Blocked or needing a decision

> **Item 3 resolved by the owner on 2026-09-18 — decision D-09** (SRS §3.11; Loop-Closure-Ledger §8): preferences stay account-level; the settings screens are role-aware (employer rail and a way back to the company for an employer without a candidate profile, no profile created by opening settings), and notification settings are linked from the employer rail and the account menu.

1. **FR-11.3 (Must): bounces and complaints never reach the suppression list.** `channels.suppress()` has no caller outside tests; sending is plain SMTP (`EMAIL_HOST`) and there is no provider webhook to ingest. Blocked on choosing an email provider and its event format.
2. **Quiet hours (FR-11.4, Should) have no screen**, so nobody can set them. The code comment says an email held by quiet hours is "picked up by the digest"; that is only true when the person has the weekly digest email on and the group on in-app. Whether a held email should be sent when quiet hours end is a product rule.
3. An employer's notification settings live inside the candidate settings shell (tabs include "Your data" and "Privacy" written for candidates). Not changed: moving settings into the employer portal is a navigation decision.

## Second pass — delivery rules (FR-11.4, FR-11.5)

**Probe** (`digest_probe.py`, scratchpad, rolled back): **0/6 before, 6/6 after.**

| # | Defect | Severity | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| N5 | **Readers past the first thousand never got a digest.** `run()` sliced the first `limit` preference rows with no ordering, once a week | High (FR-11.5 silently partial) | `[:limit]` on an unordered queryset | Every active opted-in preference, ordered, streamed with `iterator(chunk_size=…)` | `test_every_opted_in_reader_is_reached_however_small_the_chunk` |
| N6 | **A repeated weekly run sent everybody a second digest** (Celery delivers scheduled tasks at least once) | Medium | No record of this week's send was consulted | The user row is locked per send; a digest sent in the last 6 days stops another | `test_a_second_run_in_the_same_week_sends_nothing`, `test_last_week_s_digest_does_not_block_this_week_s` |
| N7 | **Deactivated and erased accounts still received digests** at their tombstone address | Medium (privacy/deliverability) | Erasure leaves preference rows; the run did not filter `is_active` | Run filters active users; `send_to` refuses an inactive account | `test_a_deactivated_account_gets_no_digest` |
| N8 | **The digest ignored the daily email cap** its own docstring promised | Low | Only suppression was checked | `channels.over_daily_cap` shared by both paths | `test_the_daily_cap_applies_to_the_digest` |
| N9 | Preferences said the digest comes **on Sunday**; the job runs **Monday 08:00** (Asia/Kathmandu) | Low (copy) | Copy never matched `config/celery.py` | "One email on Monday morning"; test ties the copy to the beat schedule | `DigestDayCopyTests` |
| N10 | **Quiet hours (FR-11.4) could not be set.** Delivery honoured a window nobody could save | Medium | No service or control | `set_quiet_hours`: one window for all six groups in the person's timezone, `HH:MM` validated, equal start/end refused, blank clears, audited. Preferences screen: From/To time inputs, save and switch-off, and copy that states emails in the window are **not sent later** (the in-app row and the digest still carry them). A misleading code comment that said the digest "picks up" held email was corrected | `QuietHoursTests` (6) |

**Real app:** as the employer owner, the preferences screen shows the quiet-hours section and "One email on Monday morning"; a window was saved and switched off through the form (see Checks).

**Checks:** targeted `apps.notifications portals.candidate apps.accounts` **367 OK** (`var/benchmark/targeted-delivery-loop.log`); 12 new tests in `apps/notifications/tests/test_delivery_loop_regressions.py`; axe/overflow rescan of feed and preferences 32 scans, 0 serious/critical, 0 overflow; mypy `apps` clean; Ruff clean on changed files; migrations: no changes. Full suite: Ran **3177** tests in 959 s — **OK** (`var/benchmark/full-suite-delivery-loop.log`; 3165 + 12 new).

**Not concurrency-tested:** two weekly runs at the same instant. The row lock is in place; the regression test covers sequential repeats only.

## Not verified in this loop

Burst collapsing on real traffic (existing unit tests only), web push (C), and anything about real email delivery — rows 1.3b, 1.3f, 1.3g, 1.3j are manual.
