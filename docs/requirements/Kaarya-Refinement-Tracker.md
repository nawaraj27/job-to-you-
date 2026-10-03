# Kaarya refinement: requirement tracker

Mission: "Career platform — complete product refinement, UX, subscription,
performance and final acceptance" (2026-10-01 to 2026-10-02). One row per
requirement. Status moves NOT STARTED → IN PROGRESS → IMPLEMENTED → AUTOMATED
TESTED → BROWSER VERIFIED, or ends at EXTERNAL VERIFICATION REQUIRED / BLOCKED
with the reason. Evidence names tests, scripts or screenshots, never
intentions. "Browser" means Chromium driven against the dev server with the
real Celery worker, Redis and Ollama.

| ID | Requirement | Code | Status | Test evidence | Browser evidence | External dependency |
|---|---|---|---|---|---|---|
| BR-1 | Public name "Kaarya", set centrally | `SITE_NAME`/`SITE_TAGLINE` in `config/settings/base.py`; `apps/core/branding.py`; `site` context | BROWSER VERIFIED | `apps/core/tests/test_branding.py` | Journey A: verification email "From: Kaarya"; every journey screenshot | |
| BR-2 | Supplied logo integrated: mark extracted unaltered, wordmark typeset as KAARYA; header, footer, auth, portals, dark theme, alt text, favicon | `templates/components/brand_mark.html`, `static/img/brand/`, `.brand-*` CSS | BROWSER VERIFIED | CSS class test; a11y scan (alt, contrast) | Header/footer/auth/staff screenshots, light and dark | Optional: artwork spelling KAARYA |
| BR-3 | No public-facing "CareerAI" (internal ids, webhook headers, migrations kept) | templates, emails, choice labels (`applications/0004`, `jobs/0004`), billing User-Agent | BROWSER VERIFIED, except the two items in the last column | `test_branding.py`; `makemigrations --check` | Public pages fetched: only the support address remains | Support domain (`hello@careerai.app`); privacy-policy contact in legal text v0.1 (`hello@careerai.test`) |
| CP-1 | Natural, human copy; no AI-like phrasing or excess em dashes | 120 template and 63 Python string rewrites, by hand | BROWSER VERIFIED | Copy-pinned tests updated to the new wording (11) | Journeys A–F | |
| CP-2 | Realistic, varied, fictional CV examples; no fabricated claims | `apps/resumes/example_personas.py` (6 personas), `examples.example_content` | BROWSER VERIFIED | `test_example_personas.py` | Template preview in the a11y scan | |
| QU-1 | Plan-aware per-feature allowances in plan entitlements | `apps/billing/limits.py`, `seeds.py`, `entitlements.py` | AUTOMATED TESTED | `test_checkout_and_quotas.py` (29) | Plan page lists all five | |
| QU-2 | Periods; allowance, used, left and reset date shown truthfully | `selectors.usage_summary`, `dashboard/plan.html`, interview hub | BROWSER VERIFIED | `test_the_plan_page_shows_used_left_and_reset` | Plan page; interview hub "X of 2 left … resetting on …" | |
| QU-3 | Server-side, concurrency-safe; tabs, refresh, duplicates, retries | `usage.take` (conditional UPDATE), request tokens | AUTOMATED TESTED | `test_claim.py`, `test_checkout_and_quotas.py` concurrency and repeat-form tests; post sweep 5,404 requests 0 failures | | |
| QU-4 | No charge for work that never starts or fails internally | `entitlements.release` in assistant, improvements, analysis, interviews, letters | AUTOMATED TESTED | refund tests per feature; `AssistedQuotaTests` | | |
| QU-5 | Limit reached is a normal state with an upgrade path | `restrictions.describe` ("You've reached your plan's limit"), 402, flash on interview hub, letter fragment | BROWSER VERIFIED | `test_restriction_ux.py` | Journey C: third practice interview refused with reset date and Pro link | |
| QU-6 | Free, paid, expired, cancelled, overdue accounts | `entitlements.plan_for`, `LIVE_STATES` | AUTOMATED TESTED | `test_ended_and_overdue_accounts`, `test_subscriptions.py` | | |
| CV-1 | Saved CV vs unsaved draft vs archived | `ResumeDocument.saved_at`, `versions._mark_saved` | BROWSER VERIFIED | `test_cv_lifecycle.py` | Journeys A and B | |
| CV-2 | Saved CVs listed only once saved; drafts listed separately | `selectors.saved_documents_for` / `drafts_for`; CV hub; dashboard (`dashboard/_cvs.html`, both dashboard states) | BROWSER VERIFIED | `test_cv_lifecycle.py` incl. `OverviewForANewProfileTests` | Dashboard and hub screenshots | |
| CV-3 | Draft limit; abandoned drafts expire via beat; saved/archived untouched | `MAX_DRAFTS`, `purge_abandoned_drafts`, beat 03:20 | AUTOMATED TESTED | expiry, idempotency, primary hand-over tests | Beat running with the entry loaded (not yet due during the session) | |
| CV-4 | Delete with confirmation; stale URLs dead | `documents.delete`, `hx-confirm`/`data-confirm` | BROWSER VERIFIED | lifecycle + `test_documents.py` | Journey A: confirm dialog, old URL redirects to hub | |
| CV-5 | Users told when a draft expires | drafts list, editor status | BROWSER VERIFIED | lifecycle tests | Drafts list and editor | |
| AS-1 | Assisted CV is the visual and functional centre | start screen, nav first item, hub card, dashboard CTA | BROWSER VERIFIED | `test_assisted_cv.py` | Journey B | |
| AS-2 | Intro, examples, progress, history, review | `assistant_start.html`, `assistant.html` | BROWSER VERIFIED | page-order test | Journey B | |
| AS-3 | Targeted questions; reuse with consent; no fabrication; stops asking for numbers | `assistant.py` (`_mentions`, `_FIGURE_QUESTION`), opt-in reuse | BROWSER VERIFIED | `FigureQuestionTests`, `ReplyQualityTests`, `ReuseIsOptInTests`, evidence tests | Journey B (defects found live and fixed: pre-ticked reuse, repeated number question) | |
| AS-4 | Review, edit, explicit save | suggestions, Save version | BROWSER VERIFIED | assistant tests | Journey B: accepted line, saved, listed as saved | |
| PY-1 | Plan → NPR price → invoice → provider → return → server verification | `start_checkout`, eSewa gateway (ADR-A58) | EXTERNAL VERIFICATION REQUIRED (final paid step) | `test_checkout_and_quotas.py`, `test_gateways.py` | Journey D to the eSewa sandbox page showing NPR 1,700; cancel returns, account stays Free | eSewa login reCAPTCHA must be completed by a person |
| PY-2 | Paid access only after verified payment | `activate_paid` in `payments._record` | BROWSER VERIFIED | `test_billing_decisions.py` (18) | Journey D: "You stay on Free until the payment is confirmed" | |
| PY-3 | Renew, cancel, period end, expire, overdue, failed renewal, reactivation, refund, double payment | `subscriptions.py`, `payments.py`, tasks | AUTOMATED TESTED | `test_subscriptions.py`, `test_renewal_concurrency.py`, `test_tasks.py`, `test_billing_loop_regressions.py` | | |
| PY-4 | NPR prices in data, central formatting, consistent everywhere | `PRO_PRICE_NPR`, `money.format_money`, `money` filter | BROWSER VERIFIED | `test_catalogue.py` | Pricing, plan, checkout, eSewa page all NPR 1,700 | |
| PY-5 | Khalti verified in its merchant sandbox | `gateways/khalti.py` | EXTERNAL VERIFICATION REQUIRED | `test_gateways.py` (Khalti cases) | | Merchant sandbox secret key |
| PY-6 | Database is the source of truth across refresh, re-login, device, restart, late callback | DB state; `reconcile_payments` every 5 min | AUTOMATED TESTED | reconcile and late-payment tests | Checkout panel survives reload | |
| JB-1 | Job filter all / verified / not verified, authoritative, combined, paginated | `apps/jobs/search.py`, `job_views.py`, `_filters.html`, `matched.html`, `job_row.html` | BROWSER VERIFIED | `test_verification_filter.py` (10) | Journeys A and E; a11y at 4 widths × 2 themes | |
| VS-1 | Visual hierarchy and section distinction | assist composer, brand lockup, data tables, allowance cards, hidden-section style, dashboard CVs | BROWSER VERIFIED | CSS class test | Screenshots home, pricing, plan, dashboard, jobs, employer, staff | |
| VS-2 | Restrained animation honouring prefers-reduced-motion | `kaarya-settle`, `.reveal`, `details[open]`, button press | BROWSER VERIFIED | | Chromium with `reduced_motion=reduce`: durations 0.00001 s | |
| PF-1 | Stage timings incl. queue and worker; avoidable delays removed | progress poll 5 s → 2 s; parse timer `perf_counter` | BROWSER VERIFIED | progress-lifecycle tests | `docs/benchmarks/CV-Analysis-Timing.md` end-to-end table | Model speed is the CPU (documented) |
| PF-2 | Progress reflects backend state; survives leave and return | processing page states | BROWSER VERIFIED | `test_cv_progress_lifecycle.py`, `test_cv_progress_chip.py` | Journey A; timing runs | |
| PF-3 | No duplicate analysis jobs | `enrich` idempotent by state; request tokens | AUTOMATED TESTED | analysis tests | One scan, parse and review per upload in each timing run | |
| AD-1 | Django admin regression (D-01 respected, secrets hidden) | admin registrations | BROWSER VERIFIED | `test_admin_inventory.py` | Journey F: admin pages 200 for superadmin, redirect for everyone else; TOTP secret and password hash not shown | |
| AD-2 | Staff portal: each level, permitted/forbidden, sensitive data, separation, IDOR | `portals/staff` | BROWSER VERIFIED | `portals/staff/tests`; IDOR sweep | Journey F matrix (6 personas × 13 URLs), suspend/reinstate, self-approval refused, staff impersonation refused | |
| OP-1 | Redis, Celery worker, beat and outbox running; tasks execute | docker, `celery worker`, `celery beat`, `outbox_worker` | BROWSER VERIFIED (observed live) | | Worker log: scan, parse, review, beat-dispatched `expire_due_jobs` | |
| JR-A..F | Journeys A–F in a real browser | — | BROWSER VERIFIED; D EXTERNAL VERIFICATION REQUIRED at the payment step | | Journeys A, B, C, E, F pass; D passes up to the provider | eSewa reCAPTCHA |
| DOC-1 | Documentation matches the implementation | `docs/CV-Lifecycle.md`, `docs/Assisted-CV.md`, `docs/Job-Search.md`, `docs/billing/Pricing-and-Quotas.md`, `docs/BRANDING.md`, timing doc, ADR-A59, technical report §45 | IMPLEMENTED | | | |
