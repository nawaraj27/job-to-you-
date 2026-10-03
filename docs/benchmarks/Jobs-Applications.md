# Jobs → Apply → Employer Pipeline — Section Loop Report

Date: 2026-09-13. Section chosen after the Interview loop (NOT READY, 7.85) and the CV loop (9.4).

**Local status: STABLE. Not READY.** Manual-only checks remain; they are listed in [`Manual-Verification-Consolidated.md`](Manual-Verification-Consolidated.md).

## Why this section was next

- **Core journey.** It is the spine of SRS §19.1 (guest → first application) and §19.2 (employer registration → hire).
- **Dependencies.** CV (done, 9.4) feeds it. Interview booking depends on it, because booking opens only at the interview stage.
- **Coverage.** The employer portal had the fewest tests (71) and no quality loop had run on it.

## Method

- **Real journeys on the development database.** Candidate `priya.sharma`, owner `hr@fonepay`, other candidates and another company were driven through the Django test client. Each probe ran inside a transaction that was rolled back afterwards, and the outbox and fan-out were run so notifications were read as delivered.
- **Real browser.** Playwright Chromium against the running dev server: the apply form, a letter conflict, the employer detail page at 375 px.
- **Automated accessibility and layout.** `scripts/applications_a11y.py` covers 16 screens (8 candidate, 8 employer) with axe-core in light and dark at 1280 px, plus overflow at 375 and 320 px — 64 scans in total.
- **Access-control probes.** Another candidate, another company, anonymous visitors, missing CSRF tokens, and a foreign bulk move.

## Defects found and fixed

| # | Defect | Severity | Reproduced by | Root cause | Fix | Regression test |
|---|---|---|---|---|---|---|
| J1 | A private stage's internal name reached the candidate by notification and email ("You are now at the Hiring manager doubts stage") | **High** (privacy, FR-6.10) | Private stage created on the dev DB, application moved, fan-out run | The event payload used `stage.label` | Payload uses `stage.public_label` | `PrivateStageNotificationTests` (3) |
| J2 | A refused application still saved the typed cover letter. One account had 10 orphan "For Backend Engineer II" letters; one refused POST took the count from 12 to 13 | **High** (data integrity) | POST to a job already applied to | The view saved the letter before the service validated | Letter saved inside the application transaction, after every guard | `ApplyLetterTests` (6) |
| J3 | A hired candidate was told "Fonepay closed your application", and every rejection sent two notifications | **High** (wrong message) | Moved to Hired and to Not proceeding, fan-out run | Every terminal move emitted both `application.closed` and `stage_changed` notifications | The closed event no longer notifies the candidate; the stage change carries the reason | `PipelineNotificationTests` (4) |
| J4 | Offers could be sent with a start date in the past | Medium | Drafted a start 5 days back; the candidate saw "starting 8 Sep" | No check on the start date | Refused, with a message | `StartDateTests` (3) |
| J5 | A second offer could be drafted after the candidate accepted one | Medium | Drafted again after acceptance | Only draft/sent offers counted as "live" | Refused when an accepted offer exists | `AfterAcceptanceTests` |
| J6 | After an accepted offer nothing prompted the employer to record the hire; the application stayed at Interview | Medium | Accept offer, open the employer page | No prompt (moving the application is deliberately a human act) | "The candidate accepted your offer … Move the application to Hired" | `AfterAcceptanceTests` |
| J7 | Bulk close with no reason reported "Moved 0 to Not proceeding. 4 were already there." | Medium | Board POST with 4 applications | Per-application errors were counted as "skipped" | Refused once for the batch; refusals reported separately from "already there" | `BulkMoveHonestyTests` (3) |
| J8 | Team notifications named no role or answer: "The candidate has responded to your offer", "They pulled out of a role you are hiring for" | Medium | Fan-out on accept and withdraw | No job title or answer in the payload | Titles now read "Offer accepted: Senior Backend Engineer…" and "A candidate withdrew from Fraud Analyst" | `AfterAcceptanceTests`, `PipelineNotificationTests` |
| J9 | A candidate whose posting closed still saw only "New" | Medium | Closed the job, opened the candidate application | No notice | "This posting is no longer accepting applications (since …). Your application is still with …" | `ClosedPostingTests` (2) |
| J10 | Pipeline settings: 27 unlabelled inputs and 9 unnamed selects; applicant detail: 1 unnamed select | Medium (accessibility, axe critical) | axe | Table headers only | `aria-label` on every control | `PipelineFormLabelTests`; axe rerun |
| J11 | Job detail "Why you're seeing this" was an invalid description list (axe serious ×13) | Medium (accessibility) | axe | dt/dd wrapped in an extra div | Restructured | axe rerun |
| J12 | Matched jobs skipped a heading level | Low (accessibility) | axe | h3 rows directly under h1 | `sr-only` h2 | axe rerun |
| J13 | Employer page showed "30.833333333333336 / 50" and raw event codes ("application.submitted — → New") | Low | Browser | Unformatted values | Rounded values; words instead of codes | `EmployerDetailReadabilityTests` (2) |
| J14 | The error page lost the typed letter and chosen CV; a saved letter plus typed text silently dropped the typing; 12 identical letter options | Low / medium | Browser | No re-population; silent precedence; title-only labels | Input kept; conflict refused; option labels show date and first words after the greeting | `ApplyLetterTests` |
| J15 | The Move form was offered on withdrawn applications (it always errored) | Low | Probe | No state check | Explains the withdrawal instead | `WithdrawnApplicationScreenTests` (2) |
| J16 | Home activity said "moved you on" for any stage, including rejection | Low | Code review during J1 | Fixed sentence | "moved your {job} application to {public stage}" | `test_the_home_activity_uses_the_public_label` |

## Verified working without change

- **Access control:** 13/13 probes. Another candidate and another company get 404 on view, move, offer and bulk move; anonymous visitors are redirected to sign in; missing CSRF gets 403.
- **Stale edits:** two editors on one posting — the second gets 400 and keeps their edits.
- **Jobs:** clone creates a draft and copies skills; a closed job disappears from search and cannot be applied to.
- **Stages:** reopen clears `closed_at`; a manual contact unlock requires a reason; contact details are hidden before the interview stage.
- **Search:** typo tolerance ("enginer" still finds 15 roles), XSS and SQL-looking input escaped, 43–87 ms per query after warm-up (594 ms cold); bad `page` and filter values degrade to defaults.
- **Saving:** save/unsave toggles over HTMX; saved-search names escaped; an invalid alert frequency is refused; other candidates cannot delete or change a saved search; an empty saved search is refused.
- **Duplicate apply** is refused.

## Checks

| Check | Result |
|---|---|
| Targeted suites (applications, candidate/employer portals, notifications, jobs) | 747 OK (pass 2), 576 OK (pass 3), 102 OK (final targeted) |
| New regression tests this section | 36 across `test_loop_regressions`, `test_offer_regressions`, `test_notification_regressions`, plus 2 updated bulk-move assertions (now also check `refused` and `reasons`) |
| Full project suite | This section's own run is invalid: a targeted run started while it was going and dropped the shared test database, causing 81 false errors (`full-suite-jobs-loop.log`). The next full run, after the Messaging loop and including every change in this section, passed: Ran 3073 tests in 1266 s — **OK** (`full-suite-messaging-loop.log`) |
| axe + overflow, 16 screens | 64 scans, **0 violations, 0 overflow** (before: 10 serious/critical rule hits on 3 screens) |
| mypy | clean (the one error found was introduced in this loop and fixed) |
| Migrations | no drift |
| Ruff | clean on changed files. 6 errors in `seed_demo.py` and 5 unformatted files in accounts, analytics and matching are pre-existing and untouched |

## Remaining in this section

- **Settings not in the UI:** offer expiry is not settable in the employer form (a 14-day default always applies). The screening-question kinds exist, but no demo job uses them, so the question form was checked only through tests.
- **Moving to Hired:** accepting an offer still requires the employer to move the application to Hired. This is deliberate ("a person moves the pipeline"); the new prompt makes it visible.
- **Automatic job close:** no automatic close after a hire (SRS §19.2 lists "job closed" as a human step).
- **Email:** HTML rendering in real mail clients is not checked; only the plain-text bodies were read.
- **Manual-only checks:** screen-reader use of apply, board and bulk move; real keyboard use in several browsers.
