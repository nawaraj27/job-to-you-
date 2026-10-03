# CareerAI — Loop Closure Ledger

Run: 2026-09-15; CV-04 and OPS-01 closed 2026-09-17; owner decisions D-11..D-13 implemented 2026-09-17 (§7); owner decisions D-01..D-10 implemented 2026-09-18 (§8); implementation freeze check 2026-09-19 (§9); CV template experience and company verification completeness 2026-09-20 (§10); manual-testing follow-up 2026-09-21 (§11, which replaces the §10 freeze). **No product decision is open.** One row per carry-forward item from the section reports in this folder, the SRS and the current code, plus defects found while closing them. Statuses: `OPEN`, `IN PROGRESS`, `LOCALLY VERIFIED`, `MANUAL GATE`, `BLOCKED`. Product decisions are listed separately (§4) and are not counted as defects.

`LOCALLY VERIFIED` = reproduced (probe, browser or a failing-side test), root cause found, fixed, regression test passing, relevant suites passing, and checked in the running app where the change is visible.

**Commands.** Tests always run with `--settings=config.settings.test`. A second concurrent run sets `CAREERAI_TEST_DB_NAME=test_careerai_b` (HAR-01). Full-suite runs are made from a `robocopy` snapshot so later edits cannot race them (HAR-02). Browser checks used the dev server on 8137, two cookie jars (`localhost`, `127.0.0.1`), and the dev database.

## 1. Evidence summary

| Check | Result | Evidence |
|---|---|---|
| Baseline full suite (before any change) | Ran 3192 — OK | `var/benchmark/baseline-closure-loop.log` |
| Full suite, pass 1 (live tree) | Ran 3218 — 2 errors, both `NoReverseMatch: export_archive`: a URL added while the run held the old URLconf. Re-run of the two tests on the final code: 3 OK | `full-suite-closure-pass1.log` |
| Full suite, pass 2 (snapshot) | Ran 3263 — OK | `full-suite-closure-pass2.log` |
| Full suite, final (snapshot) | Ran 3286 in 779 s — OK | `full-suite-closure-final.log` |
| mypy `apps` | Success, 360 files | — |
| ruff check / format, `apps portals config scripts` | clean (after CV-03) | — |
| `makemigrations --check` | only `companies/0003_ownership_transfer` added, generated this run | — |
| axe + overflow, changed screens (`scripts/closure_a11y.py`) | 48 scans, 0 serious/critical, 0 overflow (after fixing ANA-04 and DATA-01) | `var/benchmark/closure-a11y-final.json` |
| axe, sign-in/sign-up ×3 (`scripts/auth_a11y.py`) | before: 11/12 scans `color-contrast`; after: 24/24 clean | `auth-a11y-before.json`, `auth-a11y-after.json` |
| Rate limits on real Redis (scratchpad `ratelimit_probe.py`) | 7/7 PASS | — |
| Pools/analytics/staff probe (scratchpad `pools_analytics_probe.py`) | before: 6 FAIL then crash; after: 13/13 PASS | — |
| Interview thin pools, dev DB (`thin_pool_probe.py`) | 13 of 26 stored kits served a competency with 2 questions | — |
| Real local-model kit rewrite (`replan_probe.py`, qwen2.5:7b) | HR Officer: communication 2 → 3, no thin pools, 157.8 s | `var/benchmark/replan-probe-closure.log` |
| CV-04 / OPS-01 loop (2026-09-17) — full suite (snapshot, `CAREERAI_TEST_DB_NAME=test_careerai_full`) | Ran 3357 in 1190 s — OK | `var/benchmark/full-suite-cv04-ops01.log` |
| CV-04 / OPS-01 — mypy `apps`; ruff check + format `apps portals config scripts`; `makemigrations --check --dry-run` | Success, 362 files; clean; no changes | — |
| CV-04 / OPS-01 — axe + overflow (`scripts/cv04_ops01_a11y.py`) | before: guided dark `color-contrast` ×1, board `definition-list` ×2 + `dlitem` ×16; after: 16 scans, 0 serious/critical, 0 overflow | `var/benchmark/cv04-ops01-a11y.json` |
| CV-04 query counts | before 47 / 50 / 50 (editor / guided / assistant, test settings); after 19 / 25 / 25 first visit, 18 / 19 / 19 reload; dev data 16 / 20 / 20 first, 16 / 17 / 17 reload | `BuilderScreenBudgetTests` |
| OPS-04 Redis outage (dev server, Redis stopped) | before: board 500 after 38 s; after: board 200 in 6.6 s, editor 200 in 4.3 s, `/readyz` 503; recovery 0.2 s, `/readyz` 200 | `var/benchmark/ops-redis-down.html` |

## 2. Ledger

### CV

| ID | Source | Defect / unfinished behaviour | Root cause | Fix (files) | Regression test | Status |
|---|---|---|---|---|---|---|
| CV-01 | CV report limitation 5 | "I didn't actually run that project" left the bullet built from the earlier claim offered and acceptable | Retraction recognised only with a figure; the disowning path returned before touching earlier proposals | `_actions_taken_back` (verb + object), retired on every path and refused in the same turn — `apps/resumes/services/assistant.py` | `AnActionTakenBackIsRetiredTests` (4) in `apps/resumes/tests/test_assistant.py`; assistant tests 63 OK | LOCALLY VERIFIED |
| CV-02 | CV report limitation 6 | Work accepted with no employer, then "That was at Nexa Payments", became a second empty role | Defect-19 guard split any named employer from bullet-bearing roles | Explicit same-job cues ("that was at", "I did all of that for", "the company was called") keep one role; "Before that I was…" still splits | `NamingTheEmployerAfterTheWorkTests` (3 + inherited) | LOCALLY VERIFIED |
| CV-03 | CV report limitation 9 | `ruff check` 6 errors and 5 unformatted files outside `apps/resumes` | Left by earlier loops | `seed_demo.py` (unused import, loop variable, `noqa: S311` on demo randomness); 5 files formatted | ruff clean repo-wide | LOCALLY VERIFIED |
| CV-04 | CV report "42 queries"; SRS NFR-2 (≤ 25 queries per page) | Builder screens over budget. Reproduced 2026-09-16 with 12 CV skills, 6 linked roles, 10 versions/exports, 8 suggestions: editor 47, guided 50, assistant 50 (test settings, savepoints counted) | (1) N+1: `reconcile._skills_missing_from_profile` called `taxonomy.resolve` once per CV skill (alias + name query each, trigram per unknown term); (2) the candidate profile fetched 3× per render (view, greeting headline, rail counts); (3) `set_mode` wrote the document (UPDATE + transaction) on every GET, also moving `updated_at`; (4) `get_or_create_profile` opened a transaction for a pure read; (5) the bell's `unread_count` was two queries on every page | `taxonomy.services.skills.resolve_many` batched (one query per layer, one trigram scan for all unresolved keys; `resolve` delegates to it); reconcile uses it — `candidates.selectors.profile_for_request`/`remember_profile` shared by view and shell — `set_mode` no-op when unchanged, outside the transaction — lookup outside the transaction in `get_or_create_profile` — `unread_count` one query (subquery) | `BuilderScreenBudgetTests` (2; ≤ 25 on first visit and reload; +12 unresolvable skills adds ≤ 1 query) in `apps/common/tests/test_query_budgets.py`; `resolve_many` layer/agreement/flatness tests (3); `set_mode` no-op (1); profile request cache (4) + no-transaction lookup (1); bell one query (1). After: 19/25/25 first visit, 18/19/19 reload (test settings); dev data 16 / 20→17 / 20→17. Browser: all three screens render drift ("GraphQL"), suggestions, 3 exports, 4 versions | LOCALLY VERIFIED |
| CV-05 | CV limitation 3 | No screen-reader session on CV screens | Needs assistive technology | — | — | MANUAL GATE (row 2.7) |
| CV-06 | Found by CV-04 axe scan | Guided builder current-step label: serious `color-contrast` in dark mode | Raw palette class `text-brand-700` (dark on dark) instead of the theme token | `text-accent-content` in `templates/resume/_guided_steps.html` | axe rescan: 16 scans, 0 serious/critical (`var/benchmark/cv04-ops01-a11y.json`) | LOCALLY VERIFIED |

OCR (none, by decision) and row-major column detection remain recorded decisions in `CV-Extraction.md`, not defects.

### Interview

| ID | Source | Defect | Root cause | Fix | Test | Status |
|---|---|---|---|---|---|---|
| INT-01 | Interview report §11 ("some pools of 2") | Competencies serving 2 questions: every candidate got the same pair. 13/26 dev kits affected | A relevant kit was never rewritten, however thin its pools | `planning.thin_pools`; `cached_pools` queues a rewrite (throttled 1/day) when any pool < 3 — `apps/interviews/services/planning.py`; engine test fixture corrected (its "3 questions" collapsed to 2) | 2 new in `test_ai_defects.RelevanceServingTests`; interviews 384 run → 1 fixture failure → fixed, 82 OK on rerun; real rewrite 2→3 | LOCALLY VERIFIED |
| INT-02 | Interview report §11 | "Parquet is row-oriented" scored 3 instead of 1–2 | Model capability: the example is already in the prompt and qwen2.5:7b still misses it | None locally possible without a stronger model | — | BLOCKED (hosted model, `OPENROUTER_API_KEY`; row 3.5) |
| INT-03 | Interview report §11 | Benchmark ranges and relevance labels written by one person | Needs a second human rater | — | — | MANUAL GATE |
| INT-04 | Interview report §8 | Real JaaS call | No account/keys | — | — | BLOCKED (row 1.1) |
| INT-05 | §6 | Real microphone; Chrome/Edge/Safari/Android | Devices | — | — | MANUAL GATE (row 1.2) |
| INT-06 | §6 | Playwright Firefox crashes on this machine | Environment | — | — | MANUAL GATE (row 3.1) |
| INT-07 | §7 | Screen reader on interview screens | Assistive technology | — | — | MANUAL GATE (row 2.6) |
| INT-08 | §6.2 | Speech corpus is synthetic | Real voices needed | — | — | MANUAL GATE (row 1.2) |

### Employer, team and verification

| ID | Source | Defect | Root cause | Fix | Test / app check | Status |
|---|---|---|---|---|---|---|
| EMP-01 | Employer report remaining 4; SRS §2.3 | Viewer, Interviewer and Hiring Manager could create draft jobs by posting the form; "Post a job" shown to them | `require_can_manage_jobs` checked membership only | `jobs.policies.can_create_jobs` (owner/admin/recruiter or `job.write_any`), enforced on GET and POST; buttons and rail entry hidden | `CreateAccessTests` (3), `JobCreateRoleScreenTests` (3); browser as `loop.viewer`: 0 create links, `/jobs/new/` refused with message | LOCALLY VERIFIED |
| EMP-02 | Employer remaining 2; SRS §2.6 "both are notified" | Member joined/removed and ownership transfer notified nobody | Events had no audience | `payload_users` audience; wording on `company.member_joined` (inviter), `member_removed` (removed person), `ownership_transferred` (both) | `test_team_notifications.py` (3) | LOCALLY VERIFIED |
| EMP-03 | Employer remaining 1; SRS §19.2, §5.2 | Domain proof had no service or screen | `CompanyDomain` modelled, never used | `apps/companies/services/domains.py`: work email at the website domain (free-mail refused, other account's address refused) verified by the existing email token, then `prove_domain`; unique verified domain → Conflict; audited. Verification screen section; staff queue shows "domain proven" (prefetched) | `test_domain_proof.py` (6) including the full screen journey; browser: section renders; axe clean | LOCALLY VERIFIED (real email: MANUAL GATE 1.3m) |
| EMP-04 | Employer policy conflict 3 | Django `is_staff` read every private CV, message attachment and verification document with a token | `files.policies.can_access` and `selectors.for_actor` short-circuited on `is_staff`, contradicting `accounts/staff.py` | Shortcut removed; staff read files only through StaffRole-gated screens (`views.serve`) | `test_the_django_staff_flag_does_not_open_someone_elses_file`, `…does_not_list…` replace the old positive test; files/staff/companies/messaging/resumes/candidate 942 OK | LOCALLY VERIFIED (see decision D-01) |
| EMP-05 | SRS §2.6 two-party transfer | Ownership moved on the owner's click alone | No nomination step | `OwnershipTransfer` model (migration `companies/0003`, one pending per company), `nominate_owner` / `respond_to_nomination` / `cancel_nomination`; removing a nominee cancels; nomination lapses if nominator stops being owner; notifications; team screen banner, accept/decline, withdraw | `test_ownership_nomination.py` (12); team tests 76 OK | LOCALLY VERIFIED |
| EMP-06 | Employer remaining 3 | Sign-in/sign-up `color-contrast` serious, timing-dependent | 0.45 s opacity fade-in on the auth layout; axe measured mid-fade | Fade removed from `templates/layouts/auth.html` | `scripts/auth_a11y.py`: 11/12 flagged → 24/24 clean | LOCALLY VERIFIED |
| EMP-07 | Employer conflicts 1, 2, 4 | Rejected is terminal; invite binding to current account; reviewer conflict of interest | Product rules | D-02, D-03, D-04 implemented (§8) | `test_verification_decisions`, `InviteAddressDecisionTests` | LOCALLY VERIFIED (§8) |
| EMP-08 | Employer/Messaging reports | Staff message-report / moderation queue | Deferred by SRS §21 (line 1832: "a screen with no operator") | — | — | Out of scope by SRS |
| EMP-09 | — | Real invitation / verification / domain-proof email; ClamAV; two moderators | External | — | — | MANUAL GATE (1.3f–1.3i, 1.3m) |

### Talent and candidate privacy

| ID | Source | Defect | Root cause | Fix | Test / app check | Status |
|---|---|---|---|---|---|---|
| TAL-01 | FR-1.9 (Must) | Data export was JSON only | No packaging of originals | `erasure.build_archive`: zip of JSON + the candidate's own CVs, certificate files and sent message attachments; scanned-clean only; 100 MB cap with a "left out" list; each file logged `data_export`; `settings:export_archive` | `test_export_archive.py` (7); browser: 200 `application/zip`, 122 KB, 18 CVs + JSON | LOCALLY VERIFIED |
| TAL-02 | Talent remaining 2 | `q=a` matches almost everyone | Product rule | D-05 implemented (§8) | `test_talent_short_terms` | LOCALLY VERIFIED (§8) |
| TAL-03 | Talent remaining 3 | "Employers you've applied to" said only application holders could open the profile; pool and invitation grants survive | Copy out of step with the rule | Copy now states the surviving grants | `test_the_employers_option_describes_the_grants_that_survive` | LOCALLY VERIFIED — rule D-06 implemented (§8), copy rewritten |
| TAL-04 | Talent remaining 4 | No list of blocked companies, no unblock | Service only | Privacy screen section, named Unblock buttons, confirm dialog, scoped to the profile | `BlockedCompaniesScreenTests` (4); browser: list → dialog (focus on Cancel) → unblocked, message in `#announcer` | LOCALLY VERIFIED |
| TAL-05 | Talent remaining 5 | Anomaly "day" started at UTC midnight (05:45 Kathmandu) | `now.replace(hour=0)` on UTC | Local midnight via `timezone.localtime` | `test_the_day_is_the_platform_s_local_day_not_utc` | LOCALLY VERIFIED |
| TAL-06 | Talent remaining 6; FR-9.2 (Must) | No skill filter or salary filter in the form | Query parser supported them; the form did not | Skills field (comma-separated, taxonomy aliases, unknown names reported), salary ceiling field | `test_talent_filter_screens.py` (5); browser: skills kept, unknown reported, 4 results, 0 overflow | LOCALLY VERIFIED |
| TAL-07 / MSG-04 | Talent 7, Messaging | Rate limits not re-driven against Redis | — | No code change | Redis probe 7/7: message pair 21st refused, TTL, hashed key, company counter independent, login fails closed, job search fails open, 31st talent search in a minute → 429 | LOCALLY VERIFIED |

### Messaging

| ID | Source | Defect | Root cause | Fix | Test | Status |
|---|---|---|---|---|---|---|
| MSG-01 | FR-7.5 | Attachments not offered in either inbox | Service only | Multipart reply forms; `send(uploads=)` stores through `files.store` after every check, inside the message transaction | `AttachmentTests` (5) | LOCALLY VERIFIED |
| MSG-02 | Messaging remaining | Edit (15 min) and delete not reachable | Service only | Shared `templates/messages/_thread.html`; `presentation.own_message_action` scoped to the thread | `EditDeleteScreenTests` (6) | LOCALLY VERIFIED |
| MSG-05 | Found this run | Every attachment link in both inboxes was a 404 | Linked `files:access` without a token | `presentation.thread_rows` mints one-read links only for files the viewer may open; pending/foreign files named without a link | `test_a_file_sent_by_the_employer_opens_for_the_candidate_by_a_tokened_link` | LOCALLY VERIFIED |

### Jobs, applications and offers

| ID | Source | Defect | Root cause | Fix | Test / app check | Status |
|---|---|---|---|---|---|---|
| JOB-01 | Jobs remaining; FR-6.8 | Offer expiry, message and attached document not settable | Form lacked fields; service lacked document | Expiry date (end of local day, past refused), message, letter upload (company visibility; shared with the candidate at send via grant); links on both screens; `files.policies` honours grants on company files | `test_offer_terms_regressions.py` (7); `test_offers` fixture updated to reach "expired while unsent" | LOCALLY VERIFIED |
| JOB-02 | Jobs remaining | Screening questions exercised only by tests | — | No code change | Browser: owner added a "Choose one" question → candidate saw a labelled required select → applied → employer saw the answer | LOCALLY VERIFIED |
| JOB-03 | Found in JOB-02 | Every answer labelled "Screening answer"; no question text | Template iterated answers by id only | `_screening_answers` pairs answers with questions in posting order; removed questions named; whole numbers shown as typed | `test_screening_answer_display.py` (3) | LOCALLY VERIFIED |
| JOB-04 | Found in JOB-02 | Two submit buttons both announced "Add" | No accessible distinction | "Add skill" / "Add question" (screen-reader text) | — (markup) | LOCALLY VERIFIED |

### Notifications and billing

| ID | Source | Defect | Root cause | Fix | Test | Status |
|---|---|---|---|---|---|---|
| NOT-01 | FR-11.3 (Must) | Bounces/complaints never reach suppression | No email provider | — | — | BLOCKED |
| NOT-02 | Notifications report | Two simultaneous weekly digests untested | — | No code change | `ConcurrentDigestTests` (TransactionTestCase, widened window): one digest | LOCALLY VERIFIED |
| BIL-01 | Billing report | Two overlapping nightly renewals | Loop read stale rows and renewed without a lock: `subscription.renewed` emitted twice (period and invoice correct by identical arithmetic) | Row lock + re-read per subscription in `billing/tasks.renew_due` | `ConcurrentRenewalTests`: failed (2 != 1) before, passes after; billing 179 OK | LOCALLY VERIFIED |
| BIL-02 | Billing report | Real gateways | Deferred by SRS §21 (P18 row) | — | — | BLOCKED |

### Talent pools, analytics, staff board (first loop)

| ID | Source | Defect | Root cause | Fix | Test / probe | Status |
|---|---|---|---|---|---|---|
| IDS-01 | Probe | 500 on non-numeric `?pool=`, `pool_id` (pools and applicant page), `note_id`; same pattern in interview `slot`/`turn` | Raw form value into an integer pk filter | `apps/common/ids.as_pk` at 7 call sites | `MalformedIdTests` (2) | LOCALLY VERIFIED |
| POOL-01 | Probe; SRS §2.3 | Viewer/Interviewer could read pools (names), create, delete and remove members; save to pool from applicant page | Membership-only checks | `candidate.search` required for pool page, pool actions and applicant "Save to a pool"; rail hides pools, talent search, analytics accordingly | `PoolPermissionTests` (3) | LOCALLY VERIFIED |
| POOL-02 | Probe | A candidate who blocked the company stayed in its pools by name; saving them again wrote a live grant | Block revoked grants only; `visibility.grant` did not check blocks | Block soft-removes pool memberships; `visibility.grant` refuses a blocked company | `BlockedCandidatePoolTests` (2) | LOCALLY VERIFIED |
| ANA-01 | Probe; SRS §2.3 | Interviewer could read company analytics | Membership-only check | `application.read_any` required | `AnalyticsPermissionTests` | LOCALLY VERIFIED |
| ANA-02 | SRS §16.2 | No source effectiveness, applicant quality distribution or recruiter workload | Not built | `reports.source_effectiveness`, `quality_distribution` (FR-8.2 bands from the frozen snapshot), `recruiter_workload`; three sections | `test_employer_measures.py` (6), including a count-once test for a join over-count found in review; browser with dev data | LOCALLY VERIFIED |
| ANA-03 | Found by final axe scan | Analytics `dl`: serious `definition-list`/`dlitem` | `dt`/`dd` two divs deep | One `div.card.card-body` per group | axe rescan clean | LOCALLY VERIFIED |
| ANA-04 | SRS §21 (P17) | Platform activation funnel | Deferred by SRS (consent posture for `anon_id`) | — | — | Out of scope by SRS |
| OPS-01 | Not looped before; SRS §3.12, §17.4 | Board answered only part of §17.4: AI spend/failures for *yesterday* only (the rollup), no queue depth, no dependency health; no path to §3.12 admin tools | Board built around nightly rollups and DB tables; the broker and `/readyz` probes were never read by it | `core.selectors`: `ai_today` (live from `AIRun`, same expressions as `roll_ai_cost`), `queue_depths` (broker `_size`, own connection, 1 s bound, reported not raised), `dependency_health` (probes moved to `apps/common/health.py`, shared with `/readyz`); template sections Dependencies, Queue depth, AI today; platform-tool links shown only for admin permissions the viewer holds; wording ("1 day", "25% of runs") | `test_operations_board.py` (27: access anon/candidate/owner/`is_staff`/support..superadmin, POST 405, index alias, outbox, queues up/down, dependencies ok/failing, AI today vs yesterday, empty states, retention, flags, erasures, no person named, ≤ 25 queries, tool links, `dl` markup); `apps/core/tests/test_operations_snapshot.py` (16). Browser (dev, 8137, superadmin): all sections; real finding — 10 outbox rows ~3 days old (no worker running); seeded AI runs → "50% of runs"; inspector link opens; Redis stopped → board 200 showing `Cache and sessions: error: TimeoutError` and broker unreachable; `/readyz` 503 then 200 on recovery. 12 queries on dev data | LOCALLY VERIFIED |
| OPS-02 | Found in OPS-01 (§3.12 flags, NFR-9) | Creating, changing, deleting (single and bulk) a feature flag in admin wrote no audit entry | `FeatureFlagAdmin` saved and invalidated the cache only | `_audit` in `save_model`/`delete_model`/`delete_queryset`: verb, actor, key, state, changed fields | `test_platform_tools.py` `FeatureFlagAuditTests` (6) + `RecordInspectorTests` (4; audit tests failed with auditing disabled). Dev journey: created/changed/deleted `ops01_probe` → 3 rows attributed to ops@ | LOCALLY VERIFIED |
| OPS-03 | Found in OPS-02 journey | `updated_by` editable in the flag form: a change could be credited to someone else, and was blanked on every save | Field not read-only | `readonly_fields = ("updated_by",)` | `test_the_change_cannot_be_attributed_to_somebody_else` | LOCALLY VERIFIED |
| OPS-04 | Found by stopping Redis under the running app | Every signed-in page (board included) returned 500 after a 38 s hang | Django `cached_db` `load()` raises on the cache back-fill; feature-flag cache read unguarded in a context processor; no Redis socket timeouts | `apps/accounts/sessions.py` (read path tolerant; delete deliberately left to Django so a sign-out cannot half-happen); `features._flag_rows`/`invalidate_cache` tolerant; `socket_connect_timeout`/`socket_timeout` = 1 s on both caches | `test_cache_outage.py` (6; board test fails with stock engine, same traceback as dev); `test_security_baseline` updated to assert a `cached_db` subclass. Dev: board 200 in 6.6 s, editor 200 in 4.3 s during outage; 0.2 s after recovery | LOCALLY VERIFIED |
| OPS-05 | Found by OPS-01 axe scan | Board `dl`: serious `definition-list` ×2, `dlitem` ×16 (light and dark) | `dt`/`dd` two divs deep (ANA-03 pattern, old "Right now" block copied by the new one) | One `div.card.card-body` per group | `BoardMarkupTests`; axe rescan clean | LOCALLY VERIFIED |
| OPS-06 | §3.12 platform inventory | Abuse/moderation queue, support tickets, system settings, email templates | — | — | — | Out of scope by SRS §21 ("Outstanding after P19") |
| OPS-07 | §3.12 platform inventory | Announcements: listed in §3.12 and as event `system.announcement`, but no model in §5.14 and not named in §21 | Unspecified | D-10: deferred/out of scope — §3.12, §13.2 and §21 now say so; nothing registers or emits the event | — | RESOLVED — deferred / out of scope (D-10) |
| OPS-08 | §17.4 | Alerts on 5xx rate, DB connections, disk; nightly `pg_dump`/WAL and quarterly restore | Infrastructure, not app data | — | — | MANUAL GATE (rows 1.4, 1.5) |
| DATA-01 | Found by axe scan | Zip button 328 px on a 320 px phone | Unwrapped long label | Wrapping, shorter label | overflow rescan 0 | LOCALLY VERIFIED |

### Harness

| ID | Defect | Fix | Status |
|---|---|---|---|
| HAR-01 | Two `manage.py test` runs shared `test_careerai` and destroyed each other | `CAREERAI_TEST_DB_NAME` in `config/settings/test.py` | LOCALLY VERIFIED |
| HAR-02 | A full run on the live tree raced edits (pass 1's two errors) | Run from a snapshot copy | LOCALLY VERIFIED |

## 3. Remaining locally testable items

None in scope. CV-04 and OPS-01 closed 2026-09-17 with CV-06 and OPS-02..05 found and fixed on the way (§2). Still open elsewhere, none locally fixable: INT-02 (BLOCKED, hosted model), NOT-01 and BIL-02 (BLOCKED, external providers), the MANUAL GATE rows, OPS-06 (out of scope by §21), and decisions D-01..D-10 (§4) — **all ten decided and implemented 2026-09-18 (§8).**

## 4. Product decisions needed

**All resolved.** D-01..D-10 were decided by the owner on 2026-09-18 and are implemented in §8; D-11..D-13 in §7. The table is kept as the questions were asked; where the owner's decision differs from the recommendation (D-04 widened to invitees and submitters and to the decision itself, D-05 exact-match instead of a flat minimum, D-06 revoke instead of keep, D-09 role-aware account settings instead of a move into the portal), §8 is authoritative.

| ID | Decision | Current behaviour | Recommendation |
|---|---|---|---|
| D-01 | Should Django `is_staff` ever read private files? | Removed this run (EMP-04); staff use StaffRole screens | Keep removed |
| D-02 | May a rejected company re-apply, and after how long? | Rejected is terminal (SRS §7.5) | Allow a new submission after 30 days |
| D-03 | Invitation address mismatch: allow "bind to current account" (SRS §2.6)? | Refused | Keep refusing; forwarded links would grant access to anyone |
| D-04 | Reviewer who is a member of the company | Allowed | Refuse claim for members |
| D-05 | Minimum talent-search term length | `q=a` allowed | Require 2+ characters for text-only searches |
| D-06 | Should narrowing visibility revoke pool/invitation grants? | Kept; copy now says so | Keep grants, copy as fixed |
| D-07 | Should domain proof be required before submitting verification? | Optional | Keep optional; many local companies use free mail |
| D-08 | Self-serve Pro before payment; proration (Billing report) | Unchanged | As Billing report |
| D-09 | Employer notification settings inside the candidate settings shell | Unchanged | Move into the employer portal |
| D-10 | Announcements (SRS §3.12, event `system.announcement`): build now, or defer in §21 like tickets and settings? | Not built; no model in §5.14 | Defer in §21 with the other operator surfaces until an operations team exists; add the model and a staff screen together when it does |

## 5. Final full suite (closure run — superseded by §8 and §9)

**Ran 3357 tests in 1190 s — OK** (2026-09-17, `var/benchmark/full-suite-cv04-ops01.log`; 3286 at the previous closure + 71 new). Taken from a `robocopy` snapshot after the last code change; only this ledger, the manual-verification document and the project memory were edited afterwards.

---

## 6. Whole-site completion loop (2026-09-17)

Scope: the product as one application — authentication, candidate, employer portal and roles, staff/platform administration, cross-feature integration, hostile input, links and forms. CV and Interview internals were not re-looped.

**Method.** Four sweeps on the dev database, each inside a rolled-back transaction, plus an end-to-end test through HTTP views:

| Tool | What it does | First run | Final run |
|---|---|---|---|
| `scripts/site_sweep.py` | GET every non-admin route × 14 personas (anonymous, candidate, owner, admin, recruiter, hiring manager, interviewer, viewer, other-company owner, removed member, candidate+recruiter, staff support/moderator/superadmin) | 1,694 requests, 0 × 5xx; status matrix exposed the role leaks below | 1,848 requests, 0 × 5xx; matrix matches SRS §2.3 |
| `scripts/post_sweep.py` | POST every action endpoint with empty, unknown, non-numeric, negative, huge, UUID-shaped and 5,000-character input, 6 personas | 4,830 requests, **123 × 500** on 8 endpoints | 5,082 requests, 0 × 5xx |
| `scripts/link_sweep.py` | Crawl each persona's pages 2 levels; follow every link; submit every POST form that has a usable button; flag 403/404 and policy-refusal messages | 29 broken links, then 4 broken forms, then 9 refused forms | 1,476 requests, 0 broken |
| `scripts/notification_link_sweep.py` | Open every notification as its recipient and follow the link | 88 opened, 0 broken | — |
| `portals/tests/test_whole_site_journey.py` | Employer and candidate sign up from nothing → company → job → publish → apply → pipeline → interview slots → booking → offer → acceptance → notifications and their links → invite a Viewer → Viewer limits → remove → cross-tenant 404 | Found OPS-10 | Passes |

### AUTH

| ID | Requirement / source | Reproduction | Root cause | Fix | Regression test | Evidence | Status |
|---|---|---|---|---|---|---|---|
| AUTH-01 | FR-1.3 (Must) | Route list had no reset URL; sign-in page had no "forgot" link | `identity.request_password_reset` / `reset_password` and both rate limits existed with no view | `PasswordResetRequestView` (same answer for unknown addresses, rate-limited), `PasswordResetConfirmView` (single-use, validators), templates, "Forgot your password?" | `PasswordResetTests` (6) | Browser: link on sign-in, request page renders; axe clean | LOCALLY VERIFIED (real email: MANUAL GATE 1.3s) |
| AUTH-02 | Account settings | "Change password" linked to sign-in, which bounces a signed-in person; the email field was editable and required but ignored while the page said "saved" | Placeholder markup from before accounts existed | `settings:security` password change (current password, validators, keeps this session, ends others, audited); email shown read-only with a link to Security | `PasswordChangeTests` (4) | Browser: security page | LOCALLY VERIFIED |
| AUTH-03 | FR-1.4, security | A revoked session (and every session a password reset ends) kept working | `_delete_session` used the database store; sessions are `cached_db`, which serves Redis first | Delete through `settings.SESSION_ENGINE` | `SessionRevocationTests` (4; 2 fail with the old delete) | — | LOCALLY VERIFIED |
| AUTH-04 | FR-1.4 (S) | No list of sessions; once built, every browser ever used showed as signed in | No screen; `UserSession` rows never reconciled with expiry | Security page list with per-session and "everywhere else" sign-out, by row id (never session key); list joins unexpired `django_session` rows | `SessionRevocationTests`, `test_the_page_never_prints_a_session_key`, `ExpiredSessionListTests` | Browser: list shows sign-in time | LOCALLY VERIFIED |
| AUTH-05 | §2.1 "MFA-mandatory" staff; FR-1.5 (S) owners/admins, 14-day grace | No way to enable TOTP; nothing enforced it | `accounts.services.mfa` existed with no screens or login step | Enrol/confirm/recovery codes/disable on Security; sign-in challenge (5-minute pending, rate-limited, recovery codes, safe `next`); `accounts.mfa_policy` + `MfaRequirementMiddleware` (process_view, after tenancy and messages): grace notice, overdue → Security, device enabled but session unverified → code prompt; a required role cannot turn it off | `TwoFactorTests` (6), `TwoFactorRequirementTests` (5) | Browser: grace notice on staff and owner pages | LOCALLY VERIFIED (real authenticator app: MANUAL GATE 1.3t) |
| AUTH-06 | FR-1.2 | An unconfirmed address could never be confirmed if the sign-up email was lost | No resend | `accounts:verify_resend` (rate-limited) and staff resend (PLATFORM-ADMIN-01) | `VerificationResendTests` (2) | — | LOCALLY VERIFIED |
| AUTH-07 | ADR-A38 | Confirmed email tokens were recorded as password resets | Wrong `tokens.Purpose` | `Purpose.EMAIL_VERIFICATION` | covered by resend test | — | LOCALLY VERIFIED |
| AUTH-08 | Entry points | Signed-in person could open sign-up and create a second account, silently swapping the session | Only sign-in redirected authenticated users | Sign-up GET and POST redirect a signed-in person home | `test_sign_up_is_closed_to_somebody_already_signed_in` | Sweep | LOCALLY VERIFIED |
| AUTH-09 | §2.1 staff access | Staff signed in to a candidate dashboard; nothing linked to `/staff/` | `home_for` ignored `StaffRole`; no nav entry | Staff land on `staff:index`; account menu "Staff area" | `test_staff_land_in_the_staff_area`, menu test | Sweep | LOCALLY VERIFIED |
| AUTH-10 | §2.1 "both can be true; no code branches on `signup_intent`" | A candidate who joined a company had no hiring link; a hiring-intent user had no candidate links | Account menu branched on `signed_up_to_hire` | Menu from one annotated query (`has_company`, `is_platform_staff`) cached per request; Settings and Security links | `EntryPointTests` (3) | Query budgets unchanged (see PERF-01) | LOCALLY VERIFIED |

### EMPLOYER-ADMIN (role matrix, SRS §2.3)

| ID | Requirement | Reproduction | Root cause | Fix | Regression test | Evidence | Status |
|---|---|---|---|---|---|---|---|
| EMPADM-01 | §2.3 Applications/Candidates columns | Interviewer with no interviews saw 3 applicant names, applicant detail with 5 action forms, and candidate conversations; unassigned Hiring Manager saw every applicant; Viewer opened full applicant detail | Reads were membership-only; HM preset carried `application.read_any` | `applications.policies.read_scope`: Interviewer → own panels until 30 days after; HM → assigned jobs; strict companies → assignments below Admin; explicit `read_any` widens. `Application.objects.visible_to` uses it. Viewer: lists only (`require_can_open_detail`). HM preset loses `read_any` | `ApplicationReadScopeTests` (6) | Sweep + content probe before/after | LOCALLY VERIFIED |
| EMPADM-02 | §2.3 write rules | Viewer/Interviewer POSTs moved, noted, tagged and rated; any member could cancel, reschedule, conclude or share an interview | No write policy on actions | `can_write(kind)`: Owner/Admin; Recruiter on assigned jobs; HM comments on assigned, moves only with `application.decide`; Interviewer/Viewer none. Applied to every action, bulk moves (refused rows counted), offers, interview actions (scoring by panel members or commenters) | `ApplicationWriteTests` (6), `InterviewMatrixTests` (4) | POST sweep | LOCALLY VERIFIED |
| EMPADM-03 | §2.3 Jobs/Team/Company/Billing columns | Interviewer opened jobs, team, company profile, verification, webhooks; everyone below Admin opened billing | Section views checked membership only | `companies.policies.SECTION_ROLES` / `SECTION_GRANTS` + `require_section` on every section; rail hides the same routes | `SectionAccessTests` (2 × 60 subtests) | Sweep matrix | LOCALLY VERIFIED |
| EMPADM-04 | §2.3 strict scoping | `strict_assignment_scoping` narrowed nobody it was written for | Recruiter preset carries `read_any`, which bypassed strict | Only an *explicit* grant widens (EMPADM-01) | `test_strict_scoping_narrows_a_recruiter_to_assignments`, updated `test_policies` | — | LOCALLY VERIFIED |
| EMPADM-05 | §2.3 Interviewer window | Owners lost interview scorecards 30 days after an interview | Window applied to every role | Window only for Interviewers (and panel membership required) | `test_an_owner_keeps_the_record_after_thirty_days`, updated interview tests | — | LOCALLY VERIFIED |
| EMPADM-06 | UX of the matrix | Board offered a Viewer bulk move and candidate links (403); overview offered "Review", unfinished drafts and company links; board linked pipeline for Interviewers; Viewers offered "Publish times"; job create redirected an Interviewer to a 403 page; talent search form open to roles refused on submit | Templates rendered for membership, not capability | Flags from the same policies (`can_bulk`, `can_open_detail`, `can_open_*`, `can_publish_slots`); drafts filtered by `can_write`; direct 403 on job create; talent GET gated | `OfferedOnlyWhatOpensTests` (5) | `link_sweep.py` 29+9 → 0 | LOCALLY VERIFIED |

### EMPLOYER / JOBS

| ID | Requirement | Reproduction | Root cause | Fix | Regression test | Evidence | Status |
|---|---|---|---|---|---|---|---|
| EMP-10 | §2.3 Recruiter "create and edit own jobs" | Recruiter saved a draft and was refused editing it on the next click | Writes flow through assignment; creation assigned nobody | `create_job` assigns the author's membership | `test_a_recruiter_can_edit_the_job_they_just_created` | Dev probe | LOCALLY VERIFIED |
| JOBS-01 | FR-5.1 (Must), §7.5 `published → paused → published` | No paused state; the only ways off the site were "close" or "back to draft" | Not modelled | `JobStatus.PAUSED` (migration `jobs/0003`), transitions, `lifecycle.pause`/`resume` (resume does not re-fire `job.published`), actions and buttons, paused count, editable while paused, closable | `test_paused_lifecycle.PauseResumeTests` (7) | — | LOCALLY VERIFIED |
| JOBS-02 | Integration | Saved rows for closed/paused/suspended postings linked to a 404; a suspended company's posting reported `is_open` | Row always linked; `is_open` ignored company state | Unlisted saved rows render without a link; `Job.is_open` checks company state | `SavedJobLinkTests` (3) | Link sweep | LOCALLY VERIFIED |

### CANDIDATE

| ID | Requirement | Reproduction | Root cause | Fix | Regression test | Evidence | Status |
|---|---|---|---|---|---|---|---|
| CAND-10 | Navigation / §2.1 | Fresh account opening Applications, Saved jobs, Cover letters or Messages before the dashboard: 404 (all in the menu or rail) | Those four views required an existing profile; the rest create it | `get_or_create_profile` in all four | Whole-site journey; link sweep | Dev probe 404 → 200 | LOCALLY VERIFIED |
| CAND-11 | FR (cover letters) | "Draft a letter" on every job page answered 404 | Template read `raw_job`, which the job view never supplied, so the form posted an empty job | `raw_job` in context | `DraftLetterFromJobPageTests` | Form sweep | LOCALLY VERIFIED |

### STAFF / PLATFORM-ADMIN

| ID | Requirement | Reproduction | Root cause | Fix | Regression test | Evidence | Status |
|---|---|---|---|---|---|---|---|
| PADM-01 | §2.3 Moderator "unpublish jobs, suspend accounts and companies"; Support "read user and company records, resend verification" | No path: no company admin, job admin read-only, `is_active` only via the admin form (no reason, no audit, sessions kept) | Services partly existed; no screens | `/staff/companies/`, `/staff/companies/<slug>/` (suspend/reinstate/take down, reasons), `/staff/people/`, `/staff/people/<id>/` (resend verification; suspend/reinstate account ends every session; moderators cannot act on staff or themselves) | `test_moderation.py` (13) | Browser: company page; axe clean | LOCALLY VERIFIED |
| PADM-02 | §2.6 suspension cascade | Suspension left scheduled interviews, let candidates message the company, told nobody, and had no reinstate | Cascade only hid postings via company state; event had no audience | Cancels upcoming interviews (candidate notice via `interview.cancelled`), messaging frozen both ways, `company.suspended`/`reinstated` to company editors, `reinstate_company`, `job.taken_down` to editors with a link to the posting | `CompanySuspensionTests` (4), `TakeDownTests` (2) | Dev: notices delivered and opened | LOCALLY VERIFIED |
| PADM-03 | §2.3 Super Admin grants roles "with approval by a second superadmin" | No way to grant a staff role at all | Not built | `StaffRoleRequest` (migration `accounts/0008`), propose/approve/decline/withdraw/revoke (revoke ends sessions), `/staff/team/`, `manage.py bootstrap_superadmin` (once only) | `test_staff_roles.py` (8) | axe clean | LOCALLY VERIFIED |
| PADM-04 | FR-1.10 (S) impersonation | Not built | — | `accounts.impersonation`: reason, 60-minute expiry, no staff-on-staff, banner with Stop on every page, security/data/staff/admin closed while impersonating, every audit entry written with staff as `actor` and the person as `on_behalf_of` | `test_impersonation.py` (9) | — | LOCALLY VERIFIED |
| PADM-05 | UX | Support was offered Verification and Anomalies tabs that answer 403 | Static tab list | Tabs filtered by level | `test_tabs_only_offer_screens_the_level_can_open` | Sweep | LOCALLY VERIFIED |
| OPS-10 | DEPLOYMENT.md §2 | Whole-site journey: booking 404 "No interview template is configured" on a fresh database | First-deploy steps omit `seed_interview_templates` | Added to the deploy steps; operations board "Reference data" names any missing seed and its command | `ReferenceDataTests` (2) | Journey test passes | LOCALLY VERIFIED |

### SECURITY

| ID | Requirement | Reproduction | Root cause | Fix | Regression test | Evidence | Status |
|---|---|---|---|---|---|---|---|
| SEC-01 | NFR-4, hostile input | `post_sweep.py`: 123 × 500 on CV builder, CV analysis, apply, practice interview, bulk applicant moves, webhooks, company create | Raw POST values reached UUID columns (`ValidationError`); company create/edit had no length or foreign-key checks (`DataError`) | `common.ids.as_uuid` guards on every selector/service taking `public_id`; `DomainErrorMiddleware` maps a stray `ValidationError` to 400; company name/text lengths and industry/location existence validated | `test_hostile_input.py` (13) | POST sweep 123 → 0 | LOCALLY VERIFIED |
| SEC-02 | Open redirect hygiene | An error on POST redirected to whatever `Referer` said | Unchecked header | Same-site check | `test_an_error_redirect_stays_on_this_site` | — | LOCALLY VERIFIED |

### PERFORMANCE / INTEGRATION

| ID | Requirement | Finding | Fix | Evidence | Status |
|---|---|---|---|---|---|
| PERF-01 | NFR-2 | The account menu (AUTH-10) needed two more reads on every page, and the builder screens sit at the 25-query budget | One annotated query per request (skipped on employer pages, where the membership is known); the builder's separate "can undo" query folded into the document fetch as `Exists`, trusted only on GET | `BuilderScreenBudgetTests` still ≤ 25; operations board ≤ 25 | LOCALLY VERIFIED |
| INT-10 | Whole product | No test crossed both portals | `portals/tests/test_whole_site_journey.py` | Passes; notification links from the journey all open | LOCALLY VERIFIED |

### Whole-site evidence

| Check | Result | Evidence |
|---|---|---|
| Full suite, pass 1 (snapshot 14:03) | Ran 3435 — OK | `var/benchmark/full-suite-wholesite-pass1.log` |
| Full suite, final (snapshot 18:29, after the last code change) | **Ran 3477 in 738 s — OK** (3357 at the CV-04/OPS-01 closure + 120 new) | `var/benchmark/full-suite-wholesite-final.log` |
| mypy `apps` | Success, 370 files | — |
| ruff check / format `apps portals config scripts` | clean, 618 files | — |
| `makemigrations --check` | no changes (new: `accounts/0008_staff_role_request`, `jobs/0003_job_paused_status`) | — |
| axe + overflow (`scripts/wholesite_a11y.py`) | 72 scans, 18 pages all 200, 0 serious/critical, 0 overflow | `var/benchmark/wholesite-a11y.json` |
| Route, POST, link/form, notification sweeps | as in the method table | `var/benchmark/site-sweep.json`, `post-sweep.json`, `link-sweep.json`, `notification-link-sweep.json` |

### Remaining locally testable whole-site items

None known. Sweeps cover every non-admin route for 14 personas, every POST endpoint with hostile input for 6, and every link and usable form reachable two levels from each persona's home. The Django admin itself was checked for least privilege on the platform models touched (flags, audit log, AI runs), not swept form by form.

### Product decisions added by this loop

**All three resolved by the owner on 2026-09-17 and implemented — see §7.** The rows below are the questions as they were asked.

| ID | Decision | Current behaviour | SRS / conflict | Recommendation |
|---|---|---|---|---|
| D-11 | Company Admin and billing | Admin can view billing; plan changes need `billing.manage`, which only the Owner preset holds | §2.3 gives Company Admin "View, manage plan"; the Billing loop deliberately kept money movement to the Owner | Decide whether Admin manages the plan; if yes, add `billing.manage` to the Admin preset |
| D-12 | Sign-up reveals that an address is registered | "An account already uses this email" | Password reset hides existence; sign-up does not | Keep (common practice, low value to hide) or switch to a neutral message plus an email to the owner |
| D-13 | Recruiter writes are assignment-scoped | Enforced as §2.3 states; recruiters on a small team must be assigned to a job (authors are assigned automatically) | §2.3 explicit, but it changes day-to-day behaviour for existing companies | Keep as specified; consider a company setting "recruiters write on all jobs" via `job.write_any` if customers object |

---

## 7. Owner decisions D-11, D-12, D-13 (2026-09-17)

Approved by the owner and recorded in the SRS decision register (§0.4) and in §2.3 / FR-1.1. Every row below is `LOCALLY VERIFIED`: reproduced, fixed, regression-tested, suites and sweeps re-run.

| Decision | What the owner decided | Where it is enforced |
|---|---|---|
| **D-11** | Company Admin sees plan, status, usage and invoices; cannot change, cancel, resume or alter it. Owner keeps plan management. No new Billing Admin role | `companies.permissions.ROLE_GRANTS` (Admin preset lacks `billing.manage`), `companies.policies.SECTION_ROLES["billing"]`, `billing.policies.require_company_billing` on the POST, `require_can_hand_out` on invite and role change; template shows controls only for `can_manage` |
| **D-12** | Sign-up never discloses whether an address is registered; neutral response; reset and sign-up aligned; confirmation/resend preserved | `accounts.views.SignUpView` / `SignUpSentView` / `VerifyEmailView`, `SignupForm.clean_email` + `validate_unique`, `identity.email_is_registered`, `ratelimit.SIGNUP_NOTICE_PER_IDENTITY` |
| **D-13** | Recruiters reach applicants only on explicitly assigned jobs; Owner/Admin company-wide; no silent company-wide recruiter access; enforced on every route, action, query, export and object lookup | `applications.policies.read_scope` (Recruiter → assigned), `Application.objects.visible_to`, `reader_user_ids`; messaging selectors and attachment grants; `files.policies` (offer letters); `candidates.services.talent.searchable_for(company, membership)`; notification audiences; `jobs.policies.can_manage_assignments` |

| ID | Decision / source | Reproduction (before) | Root cause | Fix | Regression test | Status |
|---|---|---|---|---|---|---|
| DEC-11a | D-11 "relevant billing status" | Billing screen showed plan and renewal date but not the subscription state (active / past due / cancelled) | Template never rendered `state` | "Status: …" line; admin wording "Only a company owner can change, cancel or resume the plan" | `CompanyAdminBillingTests.test_the_admin_sees_plan_status_and_invoices_without_controls` | LOCALLY VERIFIED |
| DEC-11b | D-11 server-side | `membership.invite(..., permissions=["billing.manage"])` and `change_role(..., permissions=[...])` by a Company Admin succeeded — an admin could give plan management to an invitee or to themselves | Extra keys were validated as grant names only | `companies.policies.require_can_hand_out`: nobody hands out a grant they do not hold | `test_an_admin_cannot_hand_out_plan_management`, `test_the_owner_may_still_delegate_explicitly` | LOCALLY VERIFIED |
| DEC-11c | D-11 | Billing POST was gated by membership + grant only; the GET by section | Two different gates for one screen | POST now opens through `require_section("billing")` too; every write refused for Admin (cancel, resume, unknown, empty), roles below Admin and other companies | `test_the_admin_is_refused_every_write`, `test_roles_below_admin_and_other_companies_cannot_write`, `test_the_owner_keeps_plan_management` | LOCALLY VERIFIED |
| DEC-12a | D-12 | Sign-up with a registered address answered 400 "An account already uses this email"; Django's model unique check would have said "User with this Email already exists" | `clean_email` existence check; ModelForm `validate_unique` | Form checks no existence; `validate_unique` excludes `email`; service owns uniqueness | `test_a_form_error_does_not_mention_the_address_either`, `test_case_and_spacing_do_not_open_a_second_account_or_a_different_answer` | LOCALLY VERIFIED |
| DEC-12b | D-12 | A new address was signed in and redirected home; an existing one could not be — the difference itself enumerates | Sign-up logged the new account in | Every valid submission → 302 to `/accounts/signup/sent/`, identical page, no session. New address: confirmation email. Existing active address: "You already have a CareerAI account" (sign-in, reset, and a confirmation link if unconfirmed). Inactive: nothing sent. Password hashed on both paths (timing). Notices limited 3/hour per address, silently. Race on the unique constraint takes the existing-address path | `SignUpEnumerationTests` (11): same status/Location/page bytes (nonce and CSRF normalised), no session, account untouched, emails, limit, deactivated, reset parity | LOCALLY VERIFIED |
| DEC-12c | D-12 legitimate flows | With sign-up no longer signing in, the confirmation link redirected to the marketing page and the invitation `next` was lost | `VerifyEmailView` ignored `next` and assumed a session | Confirmation link carries a same-site `next`; signed-out → sign-in with `next`; sign-in lands at `next` or the person's home (employer intent → portal) | `ConfirmThenSignInTests` (3), `InviteSignupRoundTripTests` (updated), `LandingTests` (updated), whole-site journey (updated) | LOCALLY VERIFIED |
| DEC-13a | D-13 | Unassigned recruiter opened every applicant (detail 200) | Recruiter preset carried `application.read_any`; `read_scope` gave recruiters company scope | Preset loses the grant; `read_scope` → assigned for Recruiter and Hiring Manager; explicit grant still widens | `RecruiterAssignmentScopeTests` (detail, note, offer, message, bulk move, board, overview, interviews, explicit grant, assign/unassign later), `CrossCompanyApplicantTests` | LOCALLY VERIFIED |
| DEC-13b | D-13 object lookups | Employer inbox listed and opened every thread started from any application | `conversations_for_company` was company-scoped | `_company_threads`: application threads follow `visible_to`; talent threads unchanged | `test_threads_about_unassigned_applicants_are_not_in_the_inbox` | LOCALLY VERIFIED |
| DEC-13c | D-13 files | Candidate message attachments were granted to every active member (any role, any job) | `_share_attachments` recipients = all members | Recipients = members who can open the inbox and read the application; a member assigned later is caught up when they open the thread | `test_candidate_files_and_notices_go_only_to_readers_of_the_application`, `test_a_recruiter_assigned_later_can_open_earlier_files` | LOCALLY VERIFIED |
| DEC-13d | D-13 files | Offer letters (company-visibility files) readable by any member holding a link | Company rule had no applicant scope | `files.policies._member_reaches_offer` | `test_an_offer_letter_is_read_only_by_readers_of_the_application` | LOCALLY VERIFIED |
| DEC-13e | D-13 search/export | A candidate visible only to "employers I applied to" was findable, openable and exportable by a recruiter not on that job, through the application grant | `searchable_for(company)` counted every live grant | `searchable_for(company, membership)`: application-basis grants count only when the member reads that application; search, profile and export pass the member | `test_search_does_not_surface_applicants_of_unassigned_jobs` | LOCALLY VERIFIED |
| DEC-13f | D-13 notifications | "New application / withdrew / offer answered" went to every member — unassigned recruiters, hiring managers, interviewers and viewers (whose link answered 403/404) | Audience was company-wide unless strict scoping | `application_company_team` = `reader_user_ids` (detail-capable readers) | `test_application_notices_go_to_the_people_who_can_open_them`, updated `AudienceTests`, `test_fanout` | LOCALLY VERIFIED |
| DEC-13g | D-13 notifications | A candidate's message notified every member, including roles that cannot open the inbox | `conversation_other_side` = all members | Members who can open messages, narrowed to application readers for application threads | same test as DEC-13c | LOCALLY VERIFIED |
| DEC-13h | Regression guard | Taking `read_any` off the Recruiter preset would have closed analytics (aggregates only) to recruiters | Analytics was gated on `application.read_any` | Section `analytics` (Owner, Admin, Recruiter, Viewer; explicit grant also opens); rail uses the same gate | `test_analytics_aggregates_stay_open_to_recruiters`; `SectionAccessTests` | LOCALLY VERIFIED |
| DEC-13i | D-13 | Overview "Interviews coming up" named applicants on jobs the member cannot read (panel seat) and linked to a 404 | Participant filter only | Filtered by `visible_to` | overview case in `test_an_unassigned_job_is_invisible_on_every_route` | LOCALLY VERIFIED |
| DEC-13j | D-13 escalation | Any recruiter who could edit a posting could assign colleagues (granting them its applicants) or remove them | Job team POST gated by `can_write` | `jobs.policies.can_manage_assignments` (`job.write_any`: Owner/Admin); form hidden otherwise. Found by `link_sweep.py` (recruiter "Remove" refused) | `test_only_owner_and_admin_change_who_is_on_a_posting` | LOCALLY VERIFIED |
| JOBS-03 | §2.3 "create and edit own jobs" | Recruiter "Duplicate as draft" landed on a copy they could not edit. Found by `link_sweep.py` after assignment data existed | `clone_job` did not assign the author (EMP-10 fixed only `create_job`) | Shared `_assign_author` | `test_a_recruiter_can_edit_the_copy_they_just_duplicated` | LOCALLY VERIFIED |
| DEMO-01 | D-13 usability of demo data | Every demo recruiter (dev DB: 0 assignments on 28 jobs) opened an empty applicant board after D-13 | `seed_demo` never assigned recruiters | `seed_demo._assign_recruiters` (idempotent); applied to the dev DB (19 assignments) | `AssignRecruitersTests` | LOCALLY VERIFIED |
| INT-11 | D-13 × interviews | Full suite pass 1: two `test_jitsi` join tests failed — their "company member" was the factory default Recruiter, unassigned, now refused the interview (404) before the panel rule the tests are about | Fixtures predated D-13; behaviour is the decided one (panel seat alone does not open a job's applicant to a Recruiter; an Interviewer reads through the panel) | Not-on-panel case uses an assigned Recruiter; observer case uses an Interviewer | `JoinViewTests` (both); `apps.interviews` 385 OK | LOCALLY VERIFIED |
| SWEEP-01 | Harness | `site_sweep.py` reported "FIXme" smell on a candidate page | Case-insensitive `FIXME` matched inside a random CSRF token/nonce | Word boundaries on `TODO`/`FIXME` | Re-run: 0 smells | LOCALLY VERIFIED |

### D-11..D-13 evidence

| Check | Result | Evidence |
|---|---|---|
| New/updated focused tests | `test_owner_decisions` 22, `test_signup_enumeration` 14, role matrix +2, seed 1; updated landing, invite round-trip, audience, fan-out, journey | all OK |
| Focused re-runs | accounts/applications/companies/billing/candidates/messaging/notifications/files/employer/journey (1321, before fixes: 10 expected failures from the old semantics, all addressed); `portals.employer` + `apps.jobs` 332 OK | `var/benchmark/d11-13-focused-1.log` |
| Full suite pass 1 (snapshot) | Ran 3516 in 639 s — 1 failure, 1 error (INT-11) | `var/benchmark/full-suite-d11-d13-pass1.log` |
| Full suite, final (snapshot after the last change) | **Ran 3516 in 603 s — OK** (3477 at the whole-site loop + 39 new) | `var/benchmark/full-suite-d11-d13-final.log` |
| ruff check / format `apps portals config scripts` | clean, 621 files | — |
| mypy `apps` | Success, 370 files | — |
| `makemigrations --check --dry-run` | No changes | — |
| `site_sweep.py` (GET × 14 personas) | 1,862 requests, 0 × 5xx, 0 smells; diff vs previous: only recruiter applicant/interview detail 200 → 404 on unassigned rows (D-13) and signed-in sign-up 200 → 302 | `var/benchmark/site-sweep-d13-final.json` |
| `post_sweep.py` (hostile POST) | 5,124 requests, 0 × 5xx | — |
| `link_sweep.py` (links + forms + refusals) | first run 12 broken (JOBS-03, DEC-13j) → 0 broken, 1,566 visited | — |
| `notification_link_sweep.py` | 88 opened, 0 broken | — |
| axe + overflow `wholesite_a11y.py` | 72 scans, 0 serious/critical, 0 overflow | `var/benchmark/wholesite-a11y-d13.json` |
| axe + overflow `auth_a11y.py` (now includes "Check your email") | 36 scans, 0 serious/critical, 0 overflow | `var/benchmark/auth-a11y-d12.json` |
| Browser (dev server) | Sign-up with a registered dev address → "Check your email"; console mail "You already have a CareerAI account" with sign-in/reset links; screenshot checked | — |
| Dev DB probe (rolled back) | fonepay: Owner billing POST allowed; Admin 200 read, POST 403; Recruiter/HM billing 403; recruiter reads 8/8 after assignment, HM 0/8 unassigned | — |

### Remaining locally testable items after D-11..D-13

None. Manual-only additions: **1.3x** (D-12 real email), wording in **1.3** (team email recipients) and **1.3f** (invite round trip through confirmation), and the "Check your email" page in **2.5k**.

---

## 8. Owner decisions D-01..D-10 (2026-09-18)

Approved by the owner and recorded in the SRS decision register (§0.4) and in §2.6, §2.7, §3.9, §3.11, §3.12, §5.6, §5.14, §7.5, §10.6, §13.2, §15.2, §19.2, §19.5 and §21 (SRS v1.23). With them, **no product decision D-01..D-13 is open.**

| Decision | What the owner decided | Where it is enforced | Status |
|---|---|---|---|
| **D-01** | `is_staff` = Django admin only; never private files; staff file reads through StaffRole screens, audited | `files.policies.can_access` / `selectors.for_actor` (no staff shortcut, EMP-04); `files.admin.AttachmentAdmin` now also refuses change and delete; no admin registration has a `FileField`; verification documents and messages are not registered; `portals.staff.views.VerificationDocumentView` writes `AttachmentAccess` + audit | LOCALLY VERIFIED |
| **D-02** | Rejected company may re-apply 30 days after the decision; rejection never overwritten; attempts distinguishable; suspension still stops it | `companies.services.company`: `REAPPLY_AFTER_DAYS`, `reapply_opens_at`, `can_reapply`, `is_editable`, `REJECTED → PENDING` edge, `submit_verification` (window + `require_company_active`), `CompanyVerification.attempt` / `submitted_by`, `CompanyVerificationDecision` (written once per decision, documents snapshot; a decided document is kept when removed), `attempt` in audit metadata and both events; migration `companies/0004_verification_attempts` backfills history from the audit log; employer verification screen (date, "This starts attempt 2", earlier decisions); staff queue ("Attempt 2", earlier decisions, "Recently reviewed" from the history) | LOCALLY VERIFIED |
| **D-03** | Never bind an invitation to another account; main or verified secondary address only; otherwise sign in as the invited address or ask for a resend | `companies.policies.invite_matches` / `require_can_accept`; `AcceptInviteView` uses the same rule for the button; mismatch page offers Sign out and the resend route; stale docstrings (`CompanyInvite`, `require_can_accept`) rewritten | LOCALLY VERIFIED |
| **D-04** | Member, invitee or submitter may not claim or decide; no override; refusal visible and audited | `companies.services.company.reviewer_conflicts` (batched), `_refuse_conflict` (audit `company.verification_conflict_refused`, outside the operation's transaction) on `claim_verification` and `review_verification`; staff queue shows "You cannot review this company: …" instead of the claim button | LOCALLY VERIFIED |
| **D-05** | < 3 characters never substring; R, C, Go, UI exact; unknown one-letter term refused; filters-only, rate limit, cap, anomaly unchanged | `candidates.search._text_matches`, `_skill_named(exact=)`, `is_short_term`, `is_known_short_skill`; `services.talent.search` refusal (after the rate-limit check); field hint on the talent screen | LOCALLY VERIFIED |
| **D-06** | Narrowing revokes PUBLIC, POOL, INVITED; APPLICATION stays; pool memberships removed; audited with reason | `candidates.services.profile._withdraw_employer_access` (`_BREADTH`, `NARROWING_REVOKES`; one `candidate.visibility_revoked` row per company with bases, pool count, from/to); privacy copy in `portals/candidate/context.py`; blocking unchanged | LOCALLY VERIFIED |
| **D-07** | Domain proof optional; documents required; free mail never counts; reviewer sees it | Unchanged behaviour, now stated: `submit_verification` docstring, SRS §19.2, verification screen copy; `domains.website_domain` refuses free-mail domains; queue shows "domain … proven" / "domain not proven" | LOCALLY VERIFIED |
| **D-08** | Pro at once; overdue blocks subscribe/upgrade; Free → Pro new period + full invoice; Pro → Free at period end (pending), invoice still owed; no proration | `Subscription.pending_plan` (migration `billing/0003`); `subscriptions.change_plan` / `_schedule_downgrade` / `keep_plan` / `apply_pending_plan` / `apply_due_downgrades` / `_require_nothing_overdue`; `renew` applies the pending plan; `tasks.renew_due` applies it to `past_due` rows on the date; `invoices.overdue_for`; plan screen (date, "Stay on Pro", overdue notice, paid cards withheld); company billing shows a scheduled change | LOCALLY VERIFIED |
| **D-09** | Account-level preferences; role-aware settings; no candidate tabs without a candidate context; way back to the company; links from the employer rail and account menu | `core.context_processors._account_context` (settings and notifications namespaces → employer rail for the member's company, or "Your companies" with several); `navigation.EMPLOYER_RAIL_SPEC` + "Notification settings"; `partials/user_menu.html`; `CandidateView.settings_profile` — no settings screen creates a candidate profile for an employer-only account; privacy screen's employer branch finally selected; single `NotificationPreference` set unchanged | LOCALLY VERIFIED |
| **D-10** | Announcements deferred | SRS §3.12, §13.2 (event struck, "not registered, not emitted"), §21; no code exists or was added | RESOLVED — deferred / out of scope |

### Defects found while implementing and verifying D-01..D-10

| ID | Source | Reproduction (before) | Root cause | Fix | Regression test | Status |
|---|---|---|---|---|---|---|
| DEC-03a | D-03 | An account whose **verified secondary** address was the invited one saw "sent to a different address" and no Accept button, although the POST would have accepted | Template compared `invite.email` with the main address only | View passes `address_matches` from `policies.invite_matches` | `test_a_verified_secondary_address_is_offered_the_button_and_accepts` | LOCALLY VERIFIED |
| DEC-09a | D-09 | Dev probe: employer-only owner (deepak.yadav, Everest) on `/settings/notifications/` saw the **candidate rail** (CV builder, Matched to me, …) and no link to the company; opening `/settings/`, `/settings/privacy/` or `/settings/data/` **created a CandidateProfile**, after which the notifications page used candidate wording | Settings namespace fell back to `signup_intent`; `CandidateView.profile()` get-or-creates | `_account_context`; `settings_profile` | `EmployerOnlySettingsTests` (8), `ManyCompaniesSettingsTests`, `CandidateSettingsTests` (3) | LOCALLY VERIFIED |
| DEC-09b | D-09 | The privacy screen's employer branch ("What candidates see") was unreachable: `privacy_context` hard-coded `role: candidate` | As stated | `role: employer` without a profile; candidate commands answer 400 and create nothing | `test_the_privacy_screen_shows_the_employer_version`, `test_the_account_level_switch_still_works_and_candidate_commands_do_not` | LOCALLY VERIFIED |
| UI-01 | a11y scan | `decisions_a11y.py`: invitation page overflowed at 375/320 px (scroll 487 px) | Unbreakable email addresses in a `dd` and the notice | `min-w-0 break-all` on the values and addresses | Overflow rescan (0) | LOCALLY VERIFIED |
| UI-02 | Tailwind build | `break-words`, `h-auto`, `max-w-full`, `whitespace-normal`, `list-disc`, `max-w-sm`, `pl-7` were used by templates (e.g. the "Download with my files" button meant to wrap on a 320 px phone) but **absent from the built CSS**; a rebuild instead pulled ~980 unused classes from a stray agent worktree under `.claude/` | Stale `site.css`; Tailwind v4 auto-detection scanned `.claude/worktrees` | `@source not "../../../.claude"` in `static/css/src/input.css`; rebuilt (84 KB) | `apps/core/tests/test_css_classes.py` | LOCALLY VERIFIED |
| UI-03 | class check | Nine undefined classes rendered unstyled: `alert-caution` (reviewer note, and the four new D-02/D-03/D-04/D-08 notices), `text-content-critical` (error text in interviews, letters, apply, talent, CV report), `text-content-positive`, `badge-danger`, `panel-muted`, `stack-tight`, `text-heading`, `text-lead`, `border-border` | Names the design system never defined | Mapped to the defined tokens (`alert-warning`, `text-danger-fg`, `text-success-fg`, `badge-critical`, `bg-surface-sunken`, `space-y-2`, `text-title`, `border-hairline`) | `test_every_static_template_class_is_defined_in_the_stylesheet` | LOCALLY VERIFIED |
| UI-04 | a11y scan | `closure_a11y.py`: employer team page overflowed at 320 px (324 px) — "Make Sweep Hiring manager the owner" | Buttons do not wrap; long names arrived with the whole-site sweep personas | `h-auto max-w-full whitespace-normal text-left` on the ownership disclosure buttons | `test_the_ownership_buttons_wrap_a_long_name_on_a_phone` | LOCALLY VERIFIED |
| FILE-01 | link sweep (final) | A file row whose bytes were missing from storage (dev fixture documents) made `/staff/verification/documents/<id>/` answer **500** (FileNotFoundError); the token route shares the helper | `files.views._respond_with_bytes` opened the stream unguarded | Missing bytes → logged `attachment bytes missing from storage` + 404; fixtures now write real bytes | `apps/files/tests/test_missing_bytes.py` (2) | LOCALLY VERIFIED |
| TST-01 | D-05 | `test_a_candidate_with_several_matching_skills_appears_once` searched `q=e`, which D-05 now refuses | Test relied on one-letter substring matching | Uses a three-letter term shared by two skills | same test | LOCALLY VERIFIED |
| TST-02 | D-08 | Three billing tests assumed Pro → Free applied at once | Old rule | Updated to the scheduled downgrade | `LifecycleTests`, `InvoiceTests` | LOCALLY VERIFIED |

### D-01..D-10 evidence

| Check | Result | Evidence |
|---|---|---|
| New focused tests | D-01 5 (`files/tests/test_admin_privacy.py`), D-02/D-04/D-07 21 (`companies/tests/test_verification_decisions.py`), D-03 4 (`InviteAddressDecisionTests`), D-05 10 (`candidates/tests/test_talent_short_terms.py`), D-06 11 (`candidates/tests/test_visibility_narrowing.py`), D-08 16 (`billing/tests/test_billing_decisions.py`), D-09 12 (`candidate/tests/test_account_settings_roles.py`), UI 2, FILE-01 2 | all OK |
| Focused re-runs | companies + staff + onboarding 254 OK; candidates + employer 373 OK; billing + candidate portal + owner decisions 336 OK (3 expected old-rule failures fixed as TST-02) | — |
| Full suite pass 1 (snapshot) | Ran 3595 in 954 s — OK | `var/benchmark/full-suite-d01-d10-pass1.log` |
| Full suite pass 2 (snapshot, before FILE-01) | Ran 3597 in 1481 s — OK | `var/benchmark/full-suite-d01-d10-pass2.log` |
| **Full suite, final (snapshot after the last change)** | **Ran 3599 in 634 s — OK** (3516 after D-11..D-13 + 83 new) | `var/benchmark/full-suite-d01-d10-final.log` |
| ruff check / format `apps portals config scripts` | clean | — |
| mypy `apps` | Success, 370 files | — |
| `makemigrations --check --dry-run` | No changes (new: `companies/0004_verification_attempts`, `billing/0003_subscription_pending_plan`; both applied to the dev DB) | — |
| `site_sweep.py` (GET × 14 personas) | 1,862 requests, 0 × 5xx, 0 smells, **0 status changes** against the D-13 baseline; re-run on the final code: same | `var/benchmark/site-sweep-d01-d10.json` |
| `post_sweep.py` (hostile POST) | 5,124 requests, 0 × 5xx | — |
| `link_sweep.py` (links + forms + refusals) | first final run 2 broken (FILE-01: 500, then 404 on fixture rows without bytes) → 1,584 visited, 0 broken | — |
| `notification_link_sweep.py` | 88 opened, 0 broken | — |
| `decisions_a11y.py` (new: every D-screen in its decisive state) | 60 scans; first run 2 overflow (UI-01) → 0 serious/critical, 0 overflow | `var/benchmark/decisions-a11y.json` |
| All other axe + overflow scans after the CSS rebuild and class fixes | wholesite 72, auth 36, closure 48 (UI-04 fixed), employer 32, notifications 32, billing 12, talent 32, applications 64, cv04/ops 16, interview 18 pages — 0 serious/critical, 0 overflow | `var/benchmark/*-a11y-d01-d10.json` |
| Dev DB probe (rolled back) | D-09 before: candidate rail + profile created on 3 of 5 pages; after: employer rail with company links and "Notification settings" on all 5, no profile created | scratchpad `d09_probe.py` |
| Browser (dev server) | Plan screen of a Pro candidate with a scheduled downgrade: "Moves to Free on 18 Oct 2026. Pro keeps working until then, and its invoice is still due." + "Stay on Pro"; employer-only notification settings with the employer section strip | — |
| Dev fixtures (idempotent, `decisions_a11y.py`) | `loop.d02.*` (rejected waiting / open / second attempt), `loop.d04.owner` + open invitation to the dev superadmin (conflict), `loop.d03.invited` invitation, `loop.d08.downgrade`, `loop.d08.overdue` | — |

### Remaining locally testable items after D-01..D-10

**None.** Manual-only additions: **2.5l** (screen-reader pass over the D-screens) and a clause in **1.3f** (the "send it again" route with real mail). Real payment settlement stays under **1.3k/1.3l** (Blocked: gateways, SRS §21).

---

## 9. Implementation freeze check (2026-09-19 — superseded by §10)

Consistency check only: no feature, no redesign, no new decision. Its freeze
state and its "Final full suite" no longer describe the tree: §10 changed
source after it.

| Check | Result |
|---|---|
| Tree vs the §8 final run | Last source change before it 2026-09-18 14:36 (`apps/files/views.py`, FILE-01); ruff 14:49, mypy 14:38; suite snapshot ran about 14:49–15:00 and passed; afterwards only this ledger changed (15:01). §8's numbers matched the tree |
| Decision register | SRS §0.4 and this ledger (§4, §7, §8) carry D-01..D-13; none open |
| D-10 | Deferred, not implemented: SRS §0.4 "Deferred", §3.12, §13.2 (event struck), §21; no `Announcement` model, screen or `system.announcement` registration in `apps`, `portals`, `templates`, `config` |
| Manual checklist | Each row checked for an automated counterpart. One row was locally testable (FRZ-01) and has been removed. Three manual/external items were in the ledger but not in the checklist and have been added: 1.3y (NOT-01 bounce ingestion), 1.6 (counsel review of terms and privacy, DEPLOYMENT.md), 3.6 (INT-03 second rater) |

| ID | Source | Reproduction (before) | Root cause | Fix | Regression test | Status |
|---|---|---|---|---|---|---|
| FRZ-01 | Manual row 3.2 ("two recruiters moving the same applicant at once") was locally testable | Two real connections, both holding the application at "New", both moving it to "Reviewing": **both succeeded**, two `application.stage_changed` timeline entries and two outbox events, so the candidate got two notices. With different target stages, the second entry recorded `from_stage` "New" when the applicant was already at the first target | `pipeline.move` checked "already at" against the instance the request had loaded; no row lock | `move` locks the application row (`select_for_update`) and reloads stage, closed, withdrawn and contact-revealed state before its checks. The second identical move now answers "This application is already at Reviewing." | `apps/applications/tests/test_concurrent_move.py` (2, `TransactionTestCase`, two threads); both fail with the lock removed and pass with it | LOCALLY VERIFIED |

### Freeze evidence

| Check | Result | Evidence |
|---|---|---|
| New tests, before and after the fix | without the lock: 2 failed; with it: 2 OK | — |
| ruff check / format `apps portals config scripts` | clean, 631 files | — |
| mypy `apps` | Success, 370 files | — |
| `makemigrations --check --dry-run` | No changes | — |
| **Full suite, final (snapshot after the last change)** | **Ran 3601 in 717 s — OK** (3599 at §8 + 2 new) | `var/benchmark/full-suite-freeze-final.log` |
| Sweeps and axe scans | Not re-run: the change is inside one service function and alters no route, template or form. The §8 results stand for everything else | — |

### Remaining locally testable items after the freeze check

**None.** The project is frozen. What remains is the manual and external verification in `Manual-Verification-Consolidated.md`, none of which has been performed.

---

## 10. CV template experience and company verification completeness (2026-09-20 — freeze superseded by §11)

Product-completeness loop, raised after the 2026-09-19 freeze and therefore **replacing** it: §9 no longer describes the tree. No feature was removed, no flow replaced, no product decision added or changed. Two gaps and two defects.

### What already existed and was preserved

| Area | Already true before this loop |
|---|---|
| Canonical CV data | `ResumeDraft.content` is one JSON document per CV; `ResumeDocument.template` is a foreign key to `ResumeTemplate`. Content and presentation were already separate, and `set_template` already "never touches what it says" |
| One flattening pass | `rendering/layout.build` produces the blocks the PDF, the DOCX and the browser preview all draw, themed by `rendering/theme.for_template` |
| Template switching | The editor's template select (`command=template`) and the gallery's "Use this template" (`command=apply`) both call `documents.set_template` on the existing document. No duplicate record per template, then or now |
| Gallery with real content | `presentation.previews` already drew every card as a complete document — the candidate's own draft when they had one, a sample when they did not — and `_template_results.html` already said which |
| Three creation modes | Editor, Guided and Assistant are three screens over one draft (`BuilderMode`), all reaching the same `ResumeDraft` |
| Verification state machine | `NOT_SUBMITTED` to `PENDING` to one of `VERIFIED` / `MORE_INFO` / `REJECTED`; attempts (D-02), claim-and-lock, reviewer conflicts (D-04), immutable `CompanyVerificationDecision` history with a document snapshot, optional domain proof (D-07), moderator-level staff permission (not `is_staff`), audited document reads |

### What was missing

| ID | Gap |
|---|---|
| CVX-01 | The gallery's sample CV covered five sections. Projects and certifications — both in `DEFAULT_SECTIONS`, both supported by every renderer — were absent, so two of the sections a candidate has to fill in were never shown filled in, and the sidebar structures had less to place than a real CV gives them |
| CVX-02 | A candidate with no CV who opened a template saw a complete example and a dead end: "Start a CV" dropped the template they had just chosen, and the CV they then started was blank. The example was visible and unusable |
| CVX-03 | Nothing on the detail page said whose CV was on it — the gallery said so, the detail page did not — and neither the detail page nor the editor offered a way back to the other templates. "Try another template" existed only as an unlabelled select in the editor sidebar |
| VER-01 | Two document kinds are required (`registration`, `pan_vat`) and the screen's own copy asks for both, but nothing computed or showed completeness. A company could not see which certificate was outstanding, and the reviewer's queue listed what had arrived and nothing about what had not — on the decision ("more information needed") that depends on exactly that |

### What was added

| ID | Change |
|---|---|
| CVX-01 | `apps/resumes/examples.py`: one worked example covering every section a blank CV starts with, plus `example_content()` — a deep copy with fresh ids and entry provenance, for a candidate who asks to start from it. `presentation.SAMPLE_CONTENT` now names that constant, so the gallery, the detail page and the comparison all draw the same document |
| CVX-02 | "Start a CV from this example" on a template's detail page posts to the existing `BuilderHubView.post` with `start=example`; the hub's own form gained the same blank-or-example choice with the template preselected. One creation path, one document, the candidate's own name and email at the top of it, and a notice on arrival saying what it is |
| CVX-03 | The detail page says whether the sheet is the example or the candidate's own CV and links back to the gallery and the comparison; the editor's template section gained the same link; the comparison screen labels the example as the gallery already did |
| VER-01 | `companies.services.company.REQUIRED_DOCUMENT_KINDS`, `missing_document_kinds`, `submission_is_complete`, `document_checklist`, and `companies.selectors.verification_for` so the employer screen reads the documents and their files in one prefetch (the queue selector already did). The employer screen lists each required certificate as attached or still needed and warns before an incomplete submission; the pending state says what a reviewer will also look for; the staff queue prints "Not sent: ..." with the reminder that asking keeps the attempt. **Advisory, not a gate**: `submit_verification` still accepts what it accepted before, because the workflow already has the state for "not enough yet" and refusing the submit would shut out a company that holds only one certificate |

### Defects found while implementing and verifying

| ID | Source | Reproduction (before) | Root cause | Fix | Regression test | Status |
|---|---|---|---|---|---|---|
| CVX-04 | New test of the three creation modes | Starting a CV in "Describe your work" from the CV hub answered **500**: `NoReverseMatch` reversing `resume:ai` with a `document_id`. Prompt-based creation was the one mode that could not be entered from the hub, and the hub is its entry point | `BuilderHubView.post` mapped `BuilderMode.ASSISTANT` to `resume:ai`, the document-less route; only `resume:assistant` takes a document id | The map names `resume:assistant` | `test_every_builder_mode_can_be_started_from_the_hub` (3 subtests; each follows the redirect and asserts 200) | LOCALLY VERIFIED |
| CVX-05 | axe/overflow scan of the gallery (never scanned before) | The template gallery scrolled sideways at 320 px: 342 px against a 320 px viewport, the overshoot equal to the page gutters | `.sheet-thumb` paired `content-visibility: auto` with `contain-intrinsic-size: auto 320px auto 452px`. At 320 px the grid column is narrower than 320 px, and a deferred card reserved 320 px anyway | `contain-intrinsic-block-size: auto 452px` — the sheet's width comes from its column, so only the block axis is worth reserving | `scripts/cv_templates_verification_a11y.py` (overflow 1 before, 0 after) | LOCALLY VERIFIED |

### Evidence

| Check | Result | Evidence |
|---|---|---|
| Baseline before any change | `apps.companies` 137 OK; `apps.resumes portals.candidate` 679 OK | — |
| New CV tests | `portals/candidate/tests/test_template_experience.py` — 26 OK | — |
| New verification tests | `apps/companies/tests/test_verification_workflow.py` — 29 OK | — |
| Affected suites | `apps.resumes apps.companies portals.candidate portals.employer portals.staff` — Ran 1115 — OK; re-run after the final query change: `apps.companies portals.employer portals.staff` — Ran 414 — OK | — |
| **Full suite** | **Ran 3656 in 1075 s — OK** (3601 at §9 plus 55 new) | `var/benchmark/full-suite-cv-verification.log` |
| ruff check / format `apps portals config scripts` | clean, 635 files | — |
| mypy `apps` | Success, 371 source files | — |
| `makemigrations --check --dry-run` | No changes (this loop adds no model field) | — |
| `manage.py check` | no issues (1 silenced) | — |
| Link sweep | visited 1096, broken 0 | `scripts/link_sweep.py`. Fifteen fewer pages than the 2026-09-18 run: the demo company owners' two-factor grace period has now elapsed, so the employer personas are sent to Security before their pages open (an existing rule, SRS §2.3, manual row 1.3t). A redirect is not a broken link, and the owner pages are still covered by `site_sweep` and by the portal suites |
| Hostile POST sweep | requests 5124, failures 0 | `scripts/post_sweep.py` |
| Notification link sweep | opened 88, broken 0 | `scripts/notification_link_sweep.py` |
| Route/site sweep | rows 1862, failures 0 | `scripts/site_sweep.py` |
| axe + overflow, changed screens | 28 scans, 0 serious/critical, 0 overflow (after CVX-05) | `var/benchmark/cv-templates-verification-a11y.json` |
| axe + overflow, CV builder screens re-run | 16 scans, 0 serious/critical, 0 overflow | `var/benchmark/cv04-ops01-a11y-rerun.json` |
| axe + overflow, employer and staff screens re-run | 32 scans, 0 serious/critical, 0 overflow | `var/benchmark/employer-a11y-rerun.json` |

### Remaining locally testable items after this loop

**None.** What remains is the manual and external verification in `Manual-Verification-Consolidated.md`, including the three rows this loop adds (1.7, 1.8 and 2.5m), none of which has been performed.

---

## 11. Manual-testing follow-up loop (2026-09-21 — freeze withdrawn by §12)

Raised from manual testing and **replacing the 2026-09-20 freeze** (§10). Additive: nothing removed, no permission or subscription check relaxed, no product decision changed. One new model (`CompanyFollow`, migration `companies/0005`).

### Defects reproduced and fixed

| ID | Reproduction (before) | Root cause | Fix | Regression test | Status |
|---|---|---|---|---|---|
| CVP-01 | Upload a CV, follow "watch progress", browse jobs: no way back. `/cv/upload/` was absent from the candidate rail (asserted: rail URLs listed without it) | The progress page was reachable only from the one-off upload-success alert; the uploads screen was not in the navigation, did not refresh, and the progress page had no way out | "Uploads" in the rail (`core.navigation`); uploads list and progress page refresh themselves only while something is in flight (`selectors.uploads_in_flight`) and say the work continues without them; progress page has "Back to your uploads" and no destructive control; a parsed upload links to its CV and its analysis; home shows "Still reading …" while a file is in flight | `portals/candidate/tests/test_cv_progress_lifecycle.py` (15) | LOCALLY VERIFIED |
| CVP-02 | Found in the demo walk-through: with no worker running, the analysis report re-rendered every 3 s for ever — every button on it replaced under the pointer (the browser's clicks failed as "stale element") and keyboard focus reset | The review poll's only stop condition was the AI review arriving | Poll only for 3 minutes after the analysis (`REVIEW_POLL_MINUTES`); after that the report says the review is late and stops | `ReviewPollTests` (2) | LOCALLY VERIFIED |
| CVP-03 | axe on the reading-progress page: `list`, `listitem` serious | `<ul>` wrapping a `<div>` wrapping the `<li>`s | Poll on a wrapper `<div>`, swap the `<ul>` itself | `scripts/loop_2026_09_21_a11y.py` (0 after) | LOCALLY VERIFIED |
| RES-01 | A free candidate's second analysis of the month (htmx): 402 as `text/plain`, htmx does not swap errors, `app.js` painted "The connection dropped or the server didn't answer" | No distinction between an expected refusal and a failure on the htmx path | `common.restrictions.describe` classifies refusals (plan, permission, state, limit, input) and returns the next step (View plans / Go to billing / who can); `DomainErrorMiddleware` renders `components/restriction.html` with the real status and `X-Restriction`; `app.js` swaps those; a page request gets `restriction.html` in the site shell instead of plain text. `ProviderError` and non-domain exceptions are untouched and still reach the error handlers | `apps/common/tests/test_restriction_ux.py` (9) | LOCALLY VERIFIED |
| RES-02 | The same refusal on a CV screen whose view caught it: red "That didn't work" | The shared flash partial printed every `DomainError` as a failure | `restrictions.flash_context`; resume and letter screens pass the exception, and the flash partials render restrictions as restrictions | `ScreenTests` in the same file | LOCALLY VERIFIED |
| VIS-01 | A line drawn above "Salary", "Location", every metadata `dt` and status eyebrow (DOM: `h2.overline` computed `text-decoration-line: overline`) | `.overline` is the design system's eyebrow class **and** Tailwind's `text-decoration-line: overline` utility; the word in 33 templates made Tailwind generate the utility too | `@source not inline("overline")` — only that utility is excluded; the eyebrow class and every focus indicator are unchanged | `apps/core/tests/test_css_classes.py::UnwantedDecorationTests` (4) | LOCALLY VERIFIED |
| VIS-02 | Dashed-outline chips reading as annotations on the analysis report ("Asked for, not mentioned") | The design system's "missing" chip (`.chip-gap`, dashed by design) was used there without its "+" — a dashed box around a bare word; next to an over-lined heading (VIS-01) | Gap chips carry the "+" like every other use. The dashed "missing" treatment itself is the design system's and was kept | template; decoration test above | LOCALLY VERIFIED |
| INT-04 | A Backend Developer and a UI/UX Designer practice interview asked identical questions | With no AI kit yet (every first session; every session with no model configured) the engine used the four general competencies and ignored the named job. Separately, the kit prompt never received `Job.responsibilities`, `Job.requirements`, or the required/preferred split | `interviews.services.job_context`: one structured reading of a posting; the AI kit's inputs are built from it (prompt extended with required/preferred skills, requirements, category, industry); with no kit, a job-aware fallback builds questions from the posting's own skills, responsibilities, requirements and level — a competency only where the posting gives evidence for it | `apps/interviews/tests/test_job_context.py` (11) | LOCALLY VERIFIED |
| FOL-02 | Found by the demo seed: following a company, then the outbox draining, delivered notices for jobs published **before** the follow (the demo candidate got 4 for 1 new job) | The fan-out resolves its audience when the outbox drains, not when the job was published | `company_followers` counts only follows created at or before the event | `test_following_after_a_job_was_published_says_nothing_about_it` | LOCALLY VERIFIED |
| MAT-01 | Score 88, labelled "Strong match", with the posting's required skill missing | The band was read off the score alone | A missing required skill caps the label at "Good match"; half or more missing caps it at "Partial match". The number is unchanged. `ALGORITHM_VERSION` 1 → 2 (cache keys and snapshots) | `TruthfulBandTests` (5) | LOCALLY VERIFIED |
| CVR-01 | Guided builder "6 of 6 done" on a CV with failing ATS checks and missing target-job skills | Two completeness calculations; the generous one read as the verdict | `resumes.readiness.assess`: Complete only when required sections are filled, the latest analysis **of this exact content** has no failing check (none or stale = unmet), and — when targeted — every skill the advert asks for is on the CV. Warnings are named but do not fail it. Guided builder and analysis report render the same object | `apps/resumes/tests/test_readiness.py` (8) | LOCALLY VERIFIED |
| SEED-01 | `seed_demo` could not provide a known candidate/employer/reviewer set | — | See DEMO-01 below | — | — |
| A11Y-01 | axe: `badge-caution` fails 4.5:1 in light mode at 12 px | Design-system variant used nowhere but the design-system page until this loop | Readiness uses `badge-neutral` with a warning icon. The variant itself is left for a design-system decision (reported, not changed: it is a global token) | cv04 scan (0 after) | OPEN (design system) |

### Features added

| ID | What |
|---|---|
| FOL-01 | Follow a company. `CompanyFollow` (user → company, unique, indexed by company), `companies.services.follows` (idempotent follow/unfollow; a suspended company cannot be followed; a company the candidate blocked cannot be followed), a Follow/Following control on the public company page (`aria-pressed`, sign-in link when signed out). `job.published` now names the `company_followers` audience under the existing **Job matches** preference, deduplicated per posting, so unpublish/republish is one notice; blocked pairs are excluded; notices already delivered stay after unfollowing |
| ASC-01 | Evidence before drafting (`resumes.services.evidence`): a bare skill claim is followed up — where, then what, then what came of it — only the missing pieces, once each, stoppable ("skip"); no model is asked until there is an account to draft from, and the model then receives the whole account. Nothing is written to the CV by a question; bullets are still suggestions the candidate accepts (INV-3) |
| ENH-01 | Target-job enhancement: the analysis form offers CareerAI postings (applied, saved, newest); the report's "Asked for, not mentioned" hands the gaps to the assistant, which opens with the first gap and asks about each in turn; the target and its gaps reach the model's prompt with "never assume they have it"; location is asked once, only in a targeted conversation, never inferred |
| MAT-02 | The analysis page shows the top jobs from the matching feed and names CV skills that are not on the profile (matching reads the profile, ADR-A46) |
| DEMO-01 | `python manage.py seed_demo --personas-only` (also run by the full `seed_demo`): five fixed accounts through the real services, converging in place on every run. `--personas-only --reset` is refused: finalized CV versions are immutable at the database and verification evidence is protected, so nothing is deleted |

### Evidence

| Check | Result |
|---|---|
| New tests | 102 new test methods: 15 progress, 9 restriction, 4 decoration, 18 follow, 11 interview context, 14 demo personas, 16 evidence/enhancement, 10 readiness (incl. 2 jobs-that-fit), 5 match truth |
| **Full suite** | see `var/benchmark/full-suite-loop-2026-09-21.log` and the handoff §A |
| ruff check / format | clean, 649 files |
| mypy `apps` | Success, 377 source files |
| `makemigrations --check` | No changes (`companies/0005_company_follow` generated this loop) |
| Link sweep | visited 1103, broken 0 |
| Notification-link sweep | opened 88, broken 0; plus a rolled-back publish of the demo draft on the dev DB: follower 1 notice, non-follower 0, link 200 |
| Hostile POST sweep | 5180 requests, 0 failures (includes the new follow and target endpoints) |
| Site sweep | 1890 rows, 0 failures |
| axe + overflow | this loop's screens 28 scans; CV templates/verification 28; CV builders 16; employer/staff 32; whole site 72 — all 0 serious/critical, 0 overflow after CVP-03 and A11Y-01 |
| Demo walk-through (dev server, persona sessions) | candidate: Uploads in rail; analysis against the Backend posting → "CV readiness: Incomplete" with the gap; "Answer questions about these" → assistant opens on the gap; "I have Docker skills" → where → what → result asked; CV unchanged, no suggestion; practice interview questions name the posting's skills and responsibilities. Employer (real views): overview, jobs, applicants (Maya listed), interviews (booked live interview listed), verification all 200. Staff: queue 200 with Gandaki Traders and "Not sent"; candidate on the staff queue 404 |

### Remaining locally testable items

None open except A11Y-01 (a design-system colour decision). Everything else is manual or external — rows 1.9–1.14 and 2.5n added to `Manual-Verification-Consolidated.md`.


## 12. Adversarial verification of §11 (2026-09-21, second pass)

§11 was treated as a candidate solution and checked against the product's behaviour rather than its tests: the real application on the dev server, a Celery worker, Celery Beat, `manage.py outbox_worker` and the local Ollama model, driven through the in-app browser pane as the demo personas. **§11's freeze is withdrawn**: its report called several rows LOCALLY VERIFIED that were partial or wrong, and one claim was untrue ("nothing in the product uses `badge-caution`" — the "Verification in review" pill on every job page did, through `status_pill`).

What "browser" means below: pages opened, buttons clicked and text typed in the pane, one persona per origin (`localhost` / `127.0.0.1`). The documented password was **not** typed into the browser — sign-in by password is a step this agent does not perform — so each browser session was minted server-side after `authenticate()` confirmed the password for all five accounts; the login form itself remains a manual row. A few checks used an in-page `fetch` (file download headers, 404s for the wrong persona) and are labelled as such.

### Requirement status

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| CV upload → progress → leave → return → same CV (CVP-01) | PASS after UPL-01, REF-01 | Browser: uploaded a PDF, "watch progress", Jobs/Applications/Interviews (rail and typed URLs), back via **Uploads**; the list refreshed itself to "Read 1 role, 4 skills", "Found in your CV" appeared without reload, polling stopped; "Edit as a CV" → analysis; rail "Latest analysis" and a browser refresh both reopened the same report | — |
| Analysis continues past the poll window; backend is the source of truth | PASS | Browser: report pending at 16:30 with no AI worker; at 16:34 polling had stopped and the page said the review was late, with **Check again**; AI worker started; rail → same report, review present (1 AI finding) | — |
| No duplicate upload / analysis / result / notification (CVP) | PASS after ENR-01 | DB after the whole walk: 1 upload, 1 CV, 1 analysis for that run, 1 AI finding; resume events have no audience (no notifications); `LatestAnalysisTests.test_navigation_and_refresh_never_start_another_analysis`, `EnrichmentRaceTests` | — |
| Readiness vs match (CVR-01, cases 1–5) | PASS after TGT-01 and the required/preferred split | `test_readiness_cases.py` (7 cases from real content, ATS failure from a missing email); browser: Backend report "requires Django, PostgreSQL; would also like Docker"; after accepting a Docker bullet and re-analysing, Docker left the list and readiness stayed Incomplete for the required two | — |
| ATS status single source | PASS | `readiness._ats` reads the analysis' own check rows for the exact content hash; real failing contact check → Incomplete (test case 2) | — |
| Assisted CV question loop (cases A–E) | PASS after EVD-01..06 | `ProbeCaseTests`; browser: target Backend → "asks for Django" → "No…" → "Django stays off your CV" + Docker question → Yes → where → what → result → local model drafted one bullet from the candidate's own words → suggestion only → next gap PostgreSQL | — |
| "Not satisfactory" never becomes Complete | PASS | Readiness is computed from CV content + current analysis only; the assistant writes transcript and suggestions, never the draft; a declined gap stays in `missing` (`test_case_e…`) | — |
| No fabrication | PASS after FAB-01 | Existing grounding tests (employer, dates, figures, actions, taken-back claims) + new `test_a_skill_the_candidate_never_named_is_not_proposed`; browser: bullet contained only what was typed | — |
| Target-job enhancement, two jobs | PASS after gap order fix | Browser: Backend gaps Django/PostgreSQL (+Docker preferred); UI/UX gaps Figma/Prototyping/UX research (+Design systems preferred); UI/UX assistant opens on Figma (required) | — |
| Job-aware interview context | PASS after INT-04b | `as_prompt_inputs` carries title, level, company, category, industry, description, responsibilities, requirements, required and preferred skills (Job has no separate qualifications/tools fields: those are requirements and skills); the kit digest now includes responsibilities, requirements and the required split, so an edit rewrites the kit | — |
| Fallback questions differ in kind | PASS after INT-04b | Discipline practice pools (engineering / design / marketing / data / people); browser: UI/UX and Marketing practice sessions started and discarded — planned sets share no role question; banner now "Built from the … posting" | Hosted-model kit quality: manual row 1.11 |
| Follow event boundary, timing, refollow | PASS | `FollowBoundaryTests` (A vs B counts, unfollow then Job 2, follow after T by timestamp, follow/unfollow/follow = 1 row); browser: Lakeside follow → unfollow → follow → one row → unfollow; employer published the Data Analyst draft in the UI → follower 1 notice, non-followers 0, notice link opened the job | — |
| Notification preferences / block | PASS | `test_the_job_matches_preference_is_respected`, `test_a_block_made_after_the_follow_silences_it` (existing, re-run) | — |
| Restriction UX | PASS after RES-03, RES-04 and the plan/permission next steps | Browser: free candidate's spent analysis → warning, "View plans" → `/dashboard/plan/`, **402** (was 200 on that path); Viewer opening New job → warning with role=status (was red `role=alert`); `ProviderError` and non-domain exceptions untouched (tests) | Genuine outage look: manual row 1.13 |
| Plan path by role | PASS | Owner → Go to billing; Company Admin → See the plan + "only the owner…" (D-11); others → who to ask; candidate → View plans (`EmployerNextStepTests`) | — |
| Visual (VIS-01/02) | PASS | Rendered DOM: 0 elements with `overline` decoration on a job page; Salary/Location labels `none`; focus ring 1.6 px solid + halo on keyboard Tab; gap chip "+" icon `aria-hidden`, list labelled "Asked for, not mentioned" | Screenshots not captured: the pane would not draw while the app window was in the background |
| A11Y-01 badge-caution | PASS | `caution-700` 0.555→0.52 L (same hue); measured in the browser by canvas: 4.44 → **5.13:1** light, all badges 4.84–9.11; `StatusContrastTests` fails on the old value (mutation-checked) | — |
| Demo accounts usable | PASS after DEMO-02, DEMO-03 | `authenticate()` OK for all five; browser as candidate, employer (overview, jobs, applicants: Maya, interviews: Thu 24 Sep 16:45, publish draft) and staff (queue with Gandaki Traders and "Not sent", certificate opens — fetch 200 `application/pdf`); candidate gets 404 on staff/employer pages | Password typed into the login form: manual |
| Demo data clean, seed idempotent | PASS | 0 duplicate applications, follows or notification dedupe keys; booked slots all have sessions; this pass's upload removed, CV archived, 2 discarded practice sessions deleted; `seed_demo --personas-only` twice: first run added one fresh draft, second nothing | — |
| Security regression | PASS | Full suite (isolation suites for candidate, company, notification recipients, jobs, applications, interviews, CV, staff); sweeps below | — |

### Defects found and fixed

| ID | Reproduction | Root cause | Fix | Test |
|---|---|---|---|---|
| UPL-01 | Browser: after Upload, two "Your uploads" lists — the new one never refreshed, the old one said "Nothing uploaded yet" | The upload answer rendered a second `#upload-history` into the alert region, without `waiting` | Out-of-band swap of the one list; `waiting` passed by the upload and delete answers; the poll also refreshes "Found in your CV" | `UploadScreenTests` |
| UPL-02 | Progress page kept polling after everything finished | Poll on a wrapper that was never replaced | The wrapper replaces itself | `test_the_progress_page_stops_asking_once_nothing_is_moving` |
| REF-01 | Browser: refresh after analysing a second CV, and rail "Latest analysis", showed the main CV's report | Default document was the primary CV; the address bar never named the report | Latest analysis across current CVs; `HX-Replace-Url` to `?report=` after a run | `LatestAnalysisTests` |
| POLL-01 | Browser: "Answer questions about these" was replaced between finding and clicking it | The whole report re-rendered every 3 s while the review was pending | A hidden watcher polls; the report is swapped once when the review lands or is late | `ReviewWatcherTests` |
| ENR-01 | Two enrich tasks for one report (redelivery, retry during a run) both appended findings; a late failure could flip DONE to FAILED | State checked before a minutes-long model call, written without a lock | Row lock and re-check before writing; failure only from PENDING | `EnrichmentRaceTests` |
| TGT-01 | Browser: readiness said the CV lacked "REST APIs" while the skills score counted it | Target coverage compared against the set of single words; multi-word skills could never match; UI/UX and marketing readiness could never be Complete | Phrase match with taxonomy aliases and the advert reader's boundary rule | `MultiWordSkillTests` |
| CVR-02 | A missing *preferred* skill made the CV Incomplete | `from_job` flattened required and preferred | `Target.required`, `missing_required`; readiness judges required, names preferred as advice; old reports stay strict | cases 4, 5 |
| SCR-01 | "Missing: docker" beside readiness that had found Docker in a bullet | The score reads the skills list, the note said "appear on the CV" | Note says "in your skills list… Not listed" (score unchanged) | — |
| EVD-01..06 | "I used Python at Phewa Stays" asked nothing; "Python and Django from my job at Phewa S" read as a skill; "for a while" was a place; "3 years" was a result; a "no" to a gap went to the model (stalled with no model); "I containerised…" ended the probe without asking for a result | Claim regex; case-insensitive capital; any digit; no gap-answer handling; unrecognised verb | Taxonomy skills inside a claim; bounded capture; proper-noun capital; years excluded; decline handled without a model and next gap asked; an answered question counts | `ProbeCaseTests`, `test_an_answer_in_words…` |
| GAP-01 | UI/UX assistant opened on "Design systems" (preferred) | Gaps passed in alphabetical order | Required first | `GapOrderTests` |
| FAB-01 | A bullet adding Docker and Kubernetes to the candidate's Python tool was proposed | Word overlap passed it; skills were not checked | Every taxonomy skill in a bullet must be one the candidate named | `test_a_skill_the_candidate_never_named_is_not_proposed` |
| INT-04b | Fallbacks were one template set with nouns swapped; a designer was asked when Figma "did not behave"; a company introduction was quoted as a duty; banner said "General practice questions" above posting questions; the kit digest ignored responsibilities | — | Discipline practice pools; judgement question outside engineering/data; duties from the structured list, company sentences dropped; banner "Built from the … posting"; digest includes responsibilities, requirements, required split | `DisciplineFallbackTests`, `test_screens` |
| RES-03 | Browser: a spent analysis on the view-caught path answered **200** | `_fragment` rendered restrictions with 200 | Real status + `X-Restriction` | `test_a_second_analysis…` |
| RES-04 | Browser: a Viewer opening New job got a red `role="alert"` box | 32 views flashed every `DomainError` with `messages.error` | `restrictions.flash`: warning for restrictions, error for failures | `test_a_refusal_a_view_redirects_with…` |
| EMB-01 | Worker log: every `embed_job` raised "events.emit(...) was called outside a transaction" | `runner.embed` wrote the run and its event outside `atomic()`; `TestCase` hid it | Row and event in one transaction; verified in the real worker (run `ok`) | `test_embedding_outside_transaction.py` (`TransactionTestCase`) |
| A11Y-01 | `badge-caution` 4.44:1 on its tint | `caution-700` below the design system's own ≥ 4.5 rule | 0.52 L | `StatusContrastTests` |
| DEMO-02 | After a tester publishes the demo draft, re-seeding left no draft — the follower workflow could not be shown again | Convergence kept the published row | A fresh draft beside it when none exists | `test_a_published_demo_draft_is_replaced…` |
| DEMO-03 | Staff: demo certificate "cannot be opened: pending" | Scan task lost when Redis restarted before a worker ran; nothing re-queues it | Seed re-queues pending demo scans | — (dev-environment convergence) |
| STALL-01 | Same root as DEMO-03, for everyone: a lost scan, read or review task left a CV at "Waiting to be parsed" or a report at "taking longer than usual" for ever | Queued work had no recovery | Ten-minute sweeps: re-queue scans and reads idle 15 minutes; a review pending 30 minutes becomes failed, which offers the free retry | `test_stalled_work.py` |

### Tests added or changed

New files: `apps/resumes/tests/test_readiness_cases.py`, `apps/resumes/tests/test_stalled_work.py`, `apps/matching/tests/test_embedding_outside_transaction.py`. New classes/methods in `test_evidence.py` (`ProbeCaseTests`, `GapOrderTests`, EVD-06), `test_assistant.py` (FAB-01), `test_cv_progress_lifecycle.py` (`ReviewWatcherTests`, `UploadScreenTests`, `LatestAnalysisTests`, late-review "Check again"), `test_job_context.py` (`DisciplineFallbackTests`), `test_company_follow.py` (`FollowBoundaryTests`), `test_restriction_ux.py` (`EmployerNextStepTests`, RES-04), `test_css_classes.py` (`StatusContrastTests`), `test_demo_personas.py` (DEMO-02). Changed on purpose: `test_readiness.py` (resets the process-wide skill vocabulary — an order-dependent failure found in this pass), `test_screens.py` (banner wording), `test_resume_screens.py` and `test_restriction_ux.py` (refusals now answer 409/402 with `X-Restriction` instead of 200).

### Evidence

| Check | Result |
|---|---|
| **Full suite** | **Ran 3825 tests in 1462 s — OK** (serial, `--settings=config.settings.test`, 2026-09-21, final tree; 67 more than the first 2026-09-21 run, none removed). Log: `var/benchmark/full-suite-verification-2026-09-21.log` |
| ruff check / format | clean, 684 files |
| mypy `apps` | Success, 377 source files |
| `makemigrations --check` / `manage.py check` | No changes / no issues |
| Link sweep | visited 1102, broken 0 |
| Notification-link sweep | opened 96, broken 0 (plus a real publish in the browser: follower 1 notice, non-followers 0, link opens the job) |
| Hostile POST sweep | 5180 requests, 0 failures |
| Site sweep | 1890 rows, 0 failures |
| axe + overflow | loop screens 28, CV builders/ops 16, whole site 72, employer/staff 32, CV templates/verification 28 — 176 scans, 0 serious/critical, 0 overflow (`var/benchmark/*-2026-09-21b.json`) |
| Contrast | browser canvas measurement, light: caution 5.13, positive 4.84, critical 5.59, accent 9.11, neutral 7.10 |
| Real worker | `embed_job` run `ok` after EMB-01; review enrichment written once; seed re-queued scan → `clean` |

### Remaining

No locally testable requirement is open. Manual rows 1.9–1.14 and 2.5n stay Pending for a human tester; the password form, screen readers, real devices and real mail stay manual. Observed but out of scope: a production scanner (ClamAV) is still row 1.3h.


## 13. Manual-testing gap loop (2026-09-22 → 2026-09-24)

Raised from manual testing, sections A–I. Each item was reproduced in the in-app browser (or its absence confirmed in code), classified, built or improved, tested, and walked again in the browser. D-01..D-13 unchanged. Two migrations: `companies/0006_company_updates`, `notifications/0004_company_updates`.

### Classification before this loop, and what was done

| Section | Before | After | What changed | Tests |
|---|---|---|---|---|
| A. CV upload progress | Partial: progress page existed, but no close, no way to follow a file from other pages, no notice when done | Done | **CVP-04**: a top-bar chip on every candidate page for a CV this session uploaded — "Checking your CV · step 1/3 · Resume progress" → "Your CV has been read · View results" (steps, not a made-up percentage); it polls every 8 s only while running and not on Uploads/progress (one poller at a time); a real visit to Uploads retires it, the list's own poll does not. Progress page: Close (X), "Minimize to the top bar", "Continue browsing jobs", step per file, "View results". `resume.parsed` now notifies the owner once per upload (dedupe key; a re-read is not news twice). Dashboard "Latest analysis" card and the rail entry were already there | `test_cv_progress_chip.py` (13) |
| B. Error experience | Partial: restrictions were warnings with one action; readiness said "Incomplete" with no way on; verification card was a status word | Done | **ERR-01**: plan refusals carry "Upgrade plan" (or "Go to billing" / "See the plan" by role, D-11) **and** "View plans" (`/pricing/`). **ERR-02**: employer overview explains what verification gives and offers "Start verification". **CVP-05**: readiness says "ATS: Passing/Incomplete", a next step per unmet item, and "Improve CV" / "Go step by step" | `test_restriction_ux.py` (updated), readiness screen tests |
| C. UI polish | Partial: overline fixed in §11; dashed "missing" chips kept | Done | **UI-01**: the gap chip is solid, warning-toned, with a warning icon (5.13:1), not dashed; design-system doc updated. New `scripts/ui_polish_audit.py`: 40 page renders (candidate, employer, staff; 1280 and 375 px) — 0 stray decorations, 0 clipped text, 0 sideways scroll, only intended borders; a screenshot of each page in `var/benchmark/screens/` | audit JSON + `StatusContrastTests` |
| D. Company follow | Partial: follow + new-job notices only | Done | **FOL-03**: `CompanyUpdate` (announcement / event with time and place), employer screen "Updates for followers" (`company.edit` to post, any company-section role to read), public "News and events" on the company page, `company.update_published` and `company.verified` to followers, a new preference group "Companies you follow" (new jobs moved there from "Job matches"), withdraw keeps delivered notices, block and publication-time rules inherited from the audience | `test_company_updates.py` (11) + follow tests |
| E. Interview questions | Partial: job-aware fallback with discipline practice pools (§12) | Done | **INT-05**: skill-specific questions for common skills (Figma usability testing, API design for tens of thousands of users, PostgreSQL migrations on live data, campaign ROI, SEO keywords…); the plan prompt now carries worked right/wrong examples and asks for the level and industry. Phrased as experience, as the prompt's existing no-hypotheticals rule requires | `test_job_context.py` |
| F. Demo accounts | Partial: employer company not verified; candidate profile 44% | Done | **DEMO-04**: Summit Digital Labs verified through the real claim-and-decide path by the demo moderator (both certificates); Maya's profile 100% complete (history, education, availability, salary, link); one announcement and one upcoming event. Idempotent: second run prints nothing | `test_demo_personas.py` (19) |
| G. Assisted CV | Partial: skill probe and target gaps only | Done | **ASC-02**: after each answer, the assistant asks what the CV itself still lacks — experience (only title and dates when the employer was already named), dates, a measurable result, target role (not when aimed at a job), projects, skills, location — once each, until the CV is sufficient; nothing is filled in | `CvGapTests` (6) |
| H. CV → job match | Partial: jobs listed with label and score | Done | **MAT-03**: each recommended job lists ✓ skills found on this CV and ⚠ required / preferred skills missing — the same comparison readiness uses — and the section states "This CV: ATS …, readiness …" | `CvJobReasonsTests` |
| I. Browser verification | — | Done for every section above | See evidence | — |

### Not done, and why

- **"Verify your company to post jobs / access candidates / schedule interviews."** Verification gates none of these in this product, and no decision (D-01..D-13) says it should. The overview lists what verification actually gives. Making verification a prerequisite is a product decision, not a UI change.
- **"CV Analysis: 45%".** Neither the scan nor the read reports progress; the chip shows the real step instead of an invented percentage.
- **Years of use in the skill probe** ("How many years?"). §12's brief required the probe to stop once where/what/result are known; dates are asked by the CV-gap question instead ("When did you work at …?").

### Evidence

| Check | Result |
|---|---|
| Browser (in-app pane, demo personas) | Follower notices for verification, announcement and event in the candidate's notifications (screenshot); company page with Verified employer, Following and News and events (screenshot); upload → progress page with X / Minimize / Continue browsing / View results (screenshot) → Jobs with "View results" chip (screenshot); worker paused → Applications with "Checking your CV · step 1/3 · Resume progress" (screenshot) → worker resumed → chip changed itself to "View results" without navigation; analysis readiness with ATS line and Improve CV (screenshot); jobs that fit with ✓/⚠ (screenshot); gap chips computed `border-style: solid`; UI/UX practice question from the AI kit about researching the guest-facing booking app; blank CV in assistant mode: where → what → result → drafted bullets → "What was your title at Phewa Stays…"; free candidate's refusal with Upgrade plan → `/dashboard/plan/` and View plans → `/pricing/` (DOM; the pane would not draw that tab); Lakeside overview verification card with Start verification (DOM); demo employer posted an announcement in the Updates screen → exactly one recipient, the follower |
| Screenshots | `var/benchmark/screens/*.png` (40, from `ui_polish_audit.py`) plus the pane screenshots above |
| Full suite | **Ran 3857 tests in 874 s — OK** (serial, `--settings=config.settings.test`, 2026-09-24, final tree; 32 more than 2026-09-21, none removed). Log: `var/benchmark/full-suite-gap-loop-2026-09-24.log` |
| Static | ruff / format clean (689 files), mypy 379 files OK, migrations and check clean |
| Sweeps | link 1105 visited / 0 broken; notification links 104 / 0 broken; hostile POST 5264 / 0 failures; site 1918 rows / 0 failures |
| axe + overflow | loop screens 40, employer/staff 36, whole site 72, CV builders/ops 16, CV templates/verification 28 — 192 scans, 0 serious/critical, 0 overflow; UI polish audit 40 renders, 0 stray decorations / clipping / sideways scroll |

### Defect found while walking it

| ID | Reproduction | Root cause | Fix | Test |
|---|---|---|---|---|
| CVP-06 | Browser: uploaded, went to Jobs — no chip | The uploads list's 5-second poll hit the Uploads view, which retired finished uploads before anyone had looked | Only a real visit (not an htmx request) retires the chip | `PollDoesNotRetireTests` |
| ASC-03 | Browser: "At Phewa Stays, my current job…" then "Tell me about your most recent job: where it was…" | The CV-gap question ignored what the conversation had already said | Asks only for title and dates at the named place | `test_a_place_already_named_is_not_asked_again` |
| DEMO-05 | Notification: "Open office afternoon … 20:45" | Seed set 15:00 in UTC | Local time | — |

## 14. Final product quality gate (2026-09-24)

Scope frozen: no features, defects only. Every area from §13 re-checked, the
product used end to end as the demo personas (inputs, results and times in
`Final-Quality-Gate.md`), and the tree compared with
`freeze-manifest-2026-09-24.sha256` — 14 files changed, all listed below, none
added or removed before the gate's own documents.

| ID | Found by | Defect | Fix | Evidence |
|---|---|---|---|---|
| MIS-01 | browser (guided builder after an edit) | Readiness said "ATS: Incomplete — fix what the checks point at" when nothing had failed, only gone out of date | `Requirement.unchecked` in `apps/resumes/readiness.py`; `_readiness.html` and `analysis.html` say "Not checked since your last edit … nothing has failed" | `UncheckedAtsTests` |
| SKIP-01 | browser (assistant) | "skip" at a target-gap question was sent to the model | `apps/resumes/services/evidence.py` declines a pending gap on a stop word | `SkipGapTests`; browser 218 ms |
| MIS-02 | browser (practice result) | Practice for a job titled "General practice interview" | `interviews/result.html`, `hub.html` show `plan.job_title` | `PracticeTitleTests` |
| DEMO-06 | browser (employer interviews) | Seeded live interview stayed "Scheduled" after its time passed | `demo_personas._live_interview` moves a past / under-a-day booking via `scheduling.reschedule` | `test_a_live_interview_that_has_passed_is_moved_to_an_upcoming_day`; seed run twice on dev |
| CAL-01 | browser (employer interviews) | Past unbooked times listed as "Open" for ever; 40-row cap, oldest first, could hide upcoming times | `selectors.slots_for` filters `ends_at >= now` | `test_a_time_that_has_passed_leaves_the_published_list`; browser |
| NOTIF-07 | notification check after DEMO-06 | A reschedule sent "Interview booked" and "Interview moved" to both sides | `scheduling.book(announce=False)` from `reschedule` | `test_a_move_reaches_both_sides` asserts one "booked" only |
| MIS-03 | browser (candidate hub) | "Interviews you have done" listed booked and moved interviews | Heading "Interviews for your applications" | wording |

Company follow audited, not extended (permissions, own-company withdraw,
followed-before-event audience, blocks, one notice per update): no defect.

Final checks: **Ran 3863 tests in 1376 s — OK** (serial, `--settings=config.settings.test`, 2026-09-24 quality gate, final tree; 6 more than the earlier 2026-09-24 freeze, none removed). Log: `var/benchmark/full-suite-quality-gate-2026-09-24.log`; ruff check / format clean (689 files); mypy `apps`
Success (379 files); `makemigrations --check` no changes; `manage.py check` no
issues; link 1105 visited / 0 broken; notification links 107 / 0 broken; hostile POST 5264 / 0 failures; site 1918 rows / 0 failures; loop screens 40, employer/staff 36, whole site 72, CV builders/ops 16, CV templates/verification 28 — 192 scans, 0 serious/critical, 0 overflow; UI polish audit 40 renders, 0 stray decorations / clipping / sideways scroll.

## 15. Measured system audit (2026-09-24)

Real inputs through the running system, timed (`System-Audit-2026-09-24.md`):
DOCX and image-only PDF uploads, the Backend Developer interview kit, a practice
interview with evaluation, the speech benchmark, three templates × PDF/DOCX
with a parser round trip, and four raw notes typed to the assistant.

| ID | Found by | Defect | Fix | Evidence |
|---|---|---|---|---|
| PARSE-01 | DOCX upload | A Projects section written only as bullets was dropped | A bullet with no project name above it is a project of its own (`parsing/extract._projects`) | `test_a_bulleted_list_of_projects_keeps_every_project` |
| PARSE-02 | DOCX export round trip | Word's blank line between list items let "• Python" pass the heading test; bulleted skills vanished (own DOCX exports read back 0 skills) | A bulleted line is never an unknown heading (`_split_sections`) | `BulletedListTests`; round trip 10/10 skills |
| PARSE-03 | follow-on | Output changed without an engine bump, so `reparse_resume_uploads` would skip every old parse | `ENGINE_VERSION` 1.2 → 1.3 | 34 dev uploads now listed for re-parse |
| ASST-01 | assistant | A reply ending with its own question had it appended again | `_mentions` check in `assistant.respond` | `test_a_question_already_in_the_reply_is_not_asked_twice` |

Not changed (decisions / external): no OCR; interview pools are per job, not per
CV (by design); the leading-verb grounding rule drops paraphrased result
bullets; evaluation leniency on the local 7B model; no unique constraint on AI
scores per (session, competency) — only reachable with concurrent evaluation
workers. Resume extraction benchmark after the fixes: 36/36, macro-F1 1.000, 0
fabrications.

Final checks: **Ran 3866 tests in 998 s — OK** (serial, `--settings=config.settings.test`, 2026-09-24 system audit, final tree; 3 more than the quality-gate freeze, none removed). Log: `var/benchmark/full-suite-system-audit-2026-09-24.log`; ruff / format clean (690 files); mypy `apps` Success (379
files); `makemigrations --check` no changes; `manage.py check` no issues. Sweeps
and axe scans were not re-run: no template, view or URL changed since §14.

## 16. AI quality and grounding hardening gate (2026-09-25)

Report: `AI-Grounding-Gate.md` (findings with reproduction, root cause, fix and
test; live and browser results). Scope held: no OCR, shared interview pools
stay job-only, no new architecture or provider.

Fixed: GEN-01 employer's own product presumed as the candidate's history
(rewrite "the <posting thing>" → "a <thing>", on write and read); GEN-02
unbounded kit re-attempts (30-minute cool-down per posting); GEN-03 fallback
duty filter broken by a literal backspace; SCO-01 per-answer judgements with
deterministic bounds and rationale-claim removal; SCO-02 hedged non-answers;
SCO-03 AI score uniqueness (`nulls_distinct=False`, migration
`interviews/0004`); SCO-04 length-cut quotations; SCO-05 dangling connective;
SCO-06 output budget; CVA-01 rewording of the same claim allowed; CVA-02
ownership needs "I/we"; CVA-03 outcomes need the candidate's result about the
same thing; CVA-04 job titles must be the candidate's words; CVA-05 no figure
question after "I don't remember".

Not reproduced: GEN-04 (the audit's ~17% malformed kits were prompts v3/v6/v7;
v8 25/25 OK). Accepted (model, local 7B): SCO-01 remainder (an off-topic answer
judged "yes" keeps its score), SCO-07 (wrong claim not listed), FUP-01
(follow-up on an off-topic answer), CVA-06 (result left out of a bullet).
Operational: OPS-01 — prompts are loaded by `manage.py load_prompts`; development
had not loaded plan prompt v9.

Final checks: **Ran 3910 tests in 1769 s — OK** (serial, `--settings=config.settings.test`, 2026-09-25 AI grounding gate, final tree; 44 more than the system-audit freeze, none removed). Log: `var/benchmark/full-suite-ai-gate-2026-09-25.log`; ruff / format clean (694 files); mypy `apps` Success (379
files); `makemigrations --check` no changes after `interviews/0004`;
`manage.py check` no issues; link 1103 visited / 0 broken; notification links 109 / 0 broken; hostile POST 5264 / 0 failures; site 1918 rows / 0 failures. Live scoring: full corpus 50 cases, 48
in band (2 model misses); 8 of 9 in band, every row grounded, 0 unsupported claims in rationales, median call 42.1 s (the miss is the audit case, 3 — model limitation). axe/overflow not re-run: no template, CSS, URL
or view structure changed — the result page renders the same markup with
different rationale text, checked in the browser.

## 17. Local decision model ("Jev-like") benchmark (2026-09-26)

Report: `Local-Decision-Model-Benchmark.md`. Official Jev (TypeSafe) is hosted
and paid with no downloadable weights, so it fails the zero-cost/local
requirement. Benchmarked locally on CPU: Flow-Judge-v0.1 Q4_K_M (Apache 2.0),
phi4-mini 3.8B (MIT), qwen2.5:1.5b (Apache 2.0) and nomic-embed-text against the
current qwen2.5:7b-instruct and the deterministic logic, on answer relevance (39
assistant-labelled cases, categories A–M), interview scoring (50-case corpus)
and CV requirement classification (24 cases). Scoring in band: current 48/50,
phi4-mini 42, qwen2.5:1.5b 39, Flow-Judge 37. Relevance: no candidate better
than current on the costly error (crediting a non-answer: current 1 of 12,
candidates 2–3). Requirements: candidates 0.50 / 0.38 against the deterministic
0.54. **Nothing integrated; production code unchanged.** Added
`scripts/decision_benchmark.py` and its corpus. Full suite 3910 OK; ruff, mypy,
migrations and `check` clean.

## 18. Final autonomous production-readiness gate (2026-09-26)

Report: `Autonomous-Release-Gate.md` (sections A–O, remaining work in five
categories). Scope held: no new model, no architecture change, no redesign.

Fixed: REL-01 production could run with a non-delivering email backend (deploy
checks `common.E003/E004`); REL-02 an interview whose scoring never ran said
"being read" for ever (beat sweep `recover_unscored_sessions`, 3 attempts,
then "could not be read"); REL-03 soft time limit swallowed as a provider
error, and no per-task limits on the `ai` queue; REL-04 open redirect in
`jobs:act`; REL-05 profile Edit toggles had no accessible name; REL-06 email
addresses in failure logs; REL-07 schema-only prompt edits ignored and
`load_prompts --check` compared nothing; REL-08 deploy procedures never ran
`load_prompts`; REL-09 contact-line location dropped (parser engine 1.4 — run
`reparse_resume_uploads`); REL-10 five undocumented environment variables.

Measured, not integrated: OCR (Tesseract 5.5.0) — clean scans 0.962 macro-F1
with 15 fabricated values, low-quality 0.420 with 67 and an unbounded page;
the recorded no-OCR decision stands. Verified with no defect: object-level
authorization (new link-swap sweep), CSRF, file serving, CSV injection,
secrets, seed idempotency, Celery and storage configuration.

Final checks: Ran 3939 tests in 1278 s — OK; ruff / format clean (703 files); mypy `apps` Success
(379 files); `makemigrations --check` no changes; `manage.py check` no issues;
`load_prompts --check` every file matches; extraction corpus 36/36, 0
fabrications; link 1103 visited / 0 broken; notification links 109 / 0 broken; hostile POST 5264 / 0 failures; site 1918 rows / 0 failures; IDOR 317 GET + 519 POST replays / 0 leaks, 0 writes; HTML integrity 997 pages / 0 findings; axe 192 scans, 0 serious/critical, 0 overflow; polish
40 renders, 0 issues.

## 19. Manual-testing environment pack (2026-09-27)

No product feature, no architecture change, no model or OCR change. The demo
seed (`apps/core/demo_personas.py`, `seed_demo --personas-only`) now builds ten
LOCAL DEMO accounts through the real services: the five from §11 plus an
interview-ready candidate (application moved to Interview, three bookable
times, job-aware practice), a Company admin and a Recruiter joined by
invitation (recruiter assigned to Backend Developer only, for D-13), a second
candidate with her own CV and applications at both companies, and a candidate
with no CV; Summit gains a Junior Data Scientist posting (required and
preferred skills). Idempotent: a second run changes nothing (users,
memberships, assignments, jobs, applications, sessions, slots, follows, CVs,
analyses, notifications — asserted by `test_running_it_again_changes_nothing`).

Verification: `scripts/manual_env_check.py` signs in to every account through
the real form (CSRF enforced) and checks what each must and must not reach —
51/51, Django test client, not a browser; browser pane sign-in for the
recruiter (applicants board "1 live, 0 interviewing") and the interview-ready
candidate ("Choose your interview time — 3 times"). New
`ManualTestingPackTests` (10). CV pack: `scripts/build_manual_cv_pack.py`, nine
synthetic files, every extraction matching its expected result. Local-only
credential and test-plan documents under git-ignored `var/manual-testing/`.

Found and fixed in this pass: the seed's new report lines used "→", which
crashed the Windows console (cp1252) while printing — replaced with ASCII
before anything else relied on it. Recorded, not changed: sign-in rate limits
(5 per account, 20 per address, 15 minutes) can block a tester switching
accounts quickly — documented with a local reset command; MFA grace ends
2026-10-05 for the demo owners and staff.
