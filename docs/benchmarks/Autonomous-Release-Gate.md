# CareerAI — Final Autonomous Production-Readiness Gate (2026-09-26)

**Status: AUTONOMOUS ENGINEERING COMPLETE.** Every safe, repository-side task
found in this pass is done or classified below. This is not a claim that
CareerAI is production-ready: that needs the manual and external checks in §M,
which no amount of automated testing replaces.

Scope held throughout: no new model (Jev and the local "Jev-like" candidates
stay rejected, `qwen2.5:7b-instruct` remains the local model), no architecture
change, no repository layer, no frontend redesign, no speculative feature.
SRS, ADR and DEVELOPER.md decisions were treated as authoritative; one of them
(no OCR) was re-measured and upheld (§D). Ledger entry: §18.

## A. Baseline

| Item | Before any change |
|---|---|
| Tree | identical to `var/benchmark/freeze-manifest-2026-09-26.sha256` (966 files, manifest SHA-256 `61c4457e…86672ef`) — verified file by file |
| Full suite | 3910 tests OK (897 s) on that exact tree |
| ruff / format / mypy | clean / 697 files / Success, 379 files |
| `makemigrations --check`, `check` | no changes / no issues |
| Sweeps (last run on the same code, 2026-09-25) | link 1103/0 broken; notification links 109/0; hostile POST 5264/0; site 1918/0 |
| Accessibility | 192 axe scans, 0 serious/critical, 0 overflow |
| `check --deploy` (production settings) | Django: no security warnings; project: `common.E001` (encryption keys unset) and `interviews.E001` (fake video provider) — both intended fail-loud checks for unset production configuration |

## B. Work performed

Every item below was reproduced first, fixed, and covered by a regression test
unless marked "docs".

| ID | Found by | Defect | Root cause | Fix | Test |
|---|---|---|---|---|---|
| REL-01 | `check --deploy` under prod settings | Production with no `DJANGO_EMAIL_BACKEND` printed every verification link, password reset and invitation to the log and sent nothing; SMTP with no host sent nothing | `EMAIL_BACKEND` defaults to the console backend; `prod.py` did not override it and no check guarded it | Deploy check `common.E003` (non-delivering backend) and `common.E004` (SMTP without `EMAIL_HOST`) | `test_deploy_checks` (6) |
| REL-02 | Tracing scoring failure paths | A finished interview whose scoring never ran (model down, worker lost, broker dropped the task) showed "your answers are being read" **for ever** | Scoring is queued once, at the end; nothing asked again (the CV pipeline has had a recovery sweep since STALL-01, interviews had none) | Beat task `recover_unscored_sessions` every 10 min: completed self-paced sessions with no score, 20 min–24 h old, at most 3 attempts; after 24 h the result page says the answers could not be read and stops polling | `test_scoring_recovery` (9) |
| REL-03 | Same trace | A scoring or kit task that hit Celery's soft time limit carried on until the hard kill (and a killed task is redelivered); scoring 4 competencies could not fit the 540-second global limit on the local model | The runner caught `SoftTimeLimitExceeded` in its broad `except Exception` as a provider error; no per-task limits on the `ai` queue | Runner re-raises it; `evaluate_session` 25 min / `plan_job_interview` 15 min limits; on timeout what was scored is concluded, not lost | same file |
| REL-04 | Redirect audit | `jobs:act` redirected to any address in the `next` form field (open redirect) | `redirect(request.POST.get("next") or …)` with no host check (three places) | `_next_or` with `url_has_allowed_host_and_scheme` | `test_jobs` (2) |
| REL-05 | HTML integrity sweep (new) | The profile page's two "Edit" toggles had no accessible name in the served HTML, and would both have read as "Edit" | Label rendered only by Alpine `x-text` | Server-rendered "Edit" plus screen-reader context ("basics", "what you're looking for") | `ProfileScreenTests` (1) |
| REL-06 | Logging audit | A failed email logged the recipient's full address (and the provider's error text repeated it) | `extra={"to": to, "error": str(exc)}` against DEVELOPER.md's "log identifiers" rule | Domain kept, local part masked (`m***@example.com`), error type logged | `test_email_logging` (2) |
| REL-07 | Deploy audit | A schema-only prompt edit (a field made required) was treated as "unchanged" and never loaded; `load_prompts --check` only listed file names | Loader compared the body hash, not the output schema; `--check` did no comparison | Unchanged only if body **and** schema match (no spurious versions for existing prompts); `--check` names each stale prompt and exits non-zero | `test_support` (3) |
| REL-08 | Deploy audit | The documented first-deploy and routine-deploy commands never ran `load_prompts`: a fresh install's AI features fail with `PromptMissing`, an upgrade keeps old prompts silently (seen in development, ledger §16 OPS-01) | Omitted from DEPLOYMENT.md | Added to both procedures (after the swap on a routine deploy) and to the checklist with `--check` | docs |
| REL-09 | Parser audit (two audits hit it) | The location on the contact line ("Pokhara, Nepal") was dropped on import, so the assistant asked for it | The reader had no location field | Contact-line location (only a line carrying the email or phone; place-shaped parts only; nothing inferred); carried into the CV on import; engine 1.3 → 1.4 | `ContactLocationTests` (5), import test (1); extraction corpus 36/36, 0 fabrications |
| REL-10 | Environment audit | Five variables read by the code were documented nowhere (`OLLAMA_KEEP_ALIVE`, `JAAS_PRIVATE_KEY`, `JAAS_BASE_URL`, `SPEECH_TO_TEXT_PROVIDER`, `CAREERAI_TEST_DB_NAME`) | — | Added to `.env.example` | docs |

New verification tooling (kept for reproducibility): `scripts/idor_sweep.py`
(link-swap object-level authorization sweep with database-write detection),
`scripts/html_integrity_sweep.py` (duplicate ids, dangling ARIA/label
references, unnamed controls, debug text on every crawled page),
`scripts/ocr_benchmark.py`. Two defects in my own tooling were found and fixed
during the pass (a pipe deadlock and a missing page timeout in the OCR
benchmark) — neither is in the product.

Verified and **not** defects (inspected, no change needed): no view is
CSRF-exempt; the only `|safe` is staff-authored legal HTML (documented); CSV
formula injection is already neutralised; private files are served as
attachments with Django-encoded filenames and content-addressed storage keys;
no committed secrets or private keys; no debug statements; every `{% url %}`
(273) and literal `reverse`/`redirect` name resolves; `seed_demo` refuses to
run with `DEBUG` off and is idempotent (second run changes nothing; no
duplicate applications, follows or notifications); a late CV analysis cannot
overwrite a newer one (each writes its own row; screens read the newest).

## C. AI status

Hardened this pass: scoring recovery and time limits (REL-02/03); prompt
loading integrity (REL-07/08). Carried from ledger §16: per-answer judgements
with deterministic bounds, rationale-claim removal, hedge/non-answer handling,
injection guard, AI-score uniqueness, employer-product rewrite, CV-assistant
grounding.

Remaining genuine model limitations (local 7B; unchanged, documented): an
off-topic answer judged relevant keeps its score (the false praise is
removed); a confidently wrong claim can get partial credit; a follow-up can
chase an off-topic answer; the CV assistant can leave a stated result out of a
bullet; kit questions are formulaic. Each should be re-measured with a hosted
model (§M.2).

**Jev was not integrated. No model was added or replaced.**

## D. CV / parser status

Extraction corpus 36/36, macro-F1 1.000, 0 fabrications (engine 1.4). Fixed
this pass: contact-line location (REL-09); earlier this week: bulleted
projects and bulleted skill lists (§15).

**OCR decision — measured, upheld (not integrated).** Tesseract 5.5.0 (Apache
2.0, local) against every PDF case: text PDFs 1.000 (OCR never triggered, 0 of
28); clean 200-dpi scans 0.962 with **15 fabricated values**; 3° skew 0.947
with 33; low-quality scans 0.420 with 67 and one page past 120 s (unbounded,
400+ CPU seconds). Nepali needs the `nep` data (0.945 vs 0.158 with `eng`).
OCR would introduce invented facts where the reader's standard is zero, on top
of the recorded product reasoning (CV-Extraction.md). Details there.

Known parser limitations: a location on its own line (not the contact line)
is not read; an image-only header inside a text PDF is lost; row-major reading
of multi-column tables remains a recorded decision.

## E. Security status

Confirmed and fixed: REL-04 (open redirect), REL-06 (PII in logs). Checked
with no defect: object-level authorization — link-swap sweep across 6 persona
pairs (candidate↔candidate, company owner↔other owner in both directions,
Summit↔Fonepay, candidate→employer), **317 object GETs and 519 object POSTs**
replayed, 0 leaks, 0 accepted, 0 database writes (positive control: the same
forms replayed by their owner were accepted and wrote rows); hostile POST sweep
5264/0; site sweep 1918 rows/0; CSRF, file serving, CSV injection, secrets,
redirects (all other `redirect` targets are server-built). Remaining security
checks that need a person or a deployment: §M.1–2 (real MFA device, TLS/HSTS
behind the real proxy, ClamAV in the deployed stack).

## F. Privacy / data status

No defect found in this pass beyond REL-06. Privacy narrowing (D-06), erasure,
exports and file access were covered by their existing suites and by the IDOR
and site sweeps; they were not re-derived from scratch here. Status: DONE +
AUTOMATED ONLY.

## G. Notification status

Notification links 109/0 broken; follower audience, preference groups, dedupe
keys and the delivery ledger's unique constraint unchanged and covered by the
suite; demo data shows no duplicate notifications. Real email delivery: §M.2.

## H. Billing status

D-08 and D-11 behaviour covered by the existing billing suite; nothing
changed. Gateway callbacks: NOT IMPLEMENTED by SRS §21 (adapters deferred), so
duplicate/stale callback handling can only be tested once an adapter exists.

## I. Frontend static status

HTML integrity sweep across 997 pages as six personas: 0 duplicate ids, 0
dangling label/ARIA references, 0 unnamed controls (after REL-05), 0 debug
text. axe: 192 scans, 0 serious/critical, 0 overflow. UI polish audit: 40
renders, 0 stray decorations, clipping or sideways scroll. Link sweep 1103/0.
No redesign.

## J. Deployment-configuration status

Django `check --deploy` under production settings: no security warnings
(secure cookies, HSTS with preload, SSL redirect behind the proxy header,
`X-Frame-Options: DENY`, nosniff, strict referrer policy, required secret key,
hosts, CSRF origins and database password). New: `common.E003/E004` (email).
Celery: late acks, rejection on worker loss, prefetch 1, bounded time limits
(now per task on the `ai` queue). Storage: private backend with no URL,
manifest static files. Health: `/readyz` (database, cache, storage).
Remaining: an actual deployment (§M.2).

## K. Documentation status

Updated: DEPLOYMENT.md (email backend, `load_prompts` in both procedures and
the checklist), DEVELOPER.md (prompt loading and `--check`), `.env.example`
(five variables), CV-Extraction.md (OCR measurement, parser 1.4), this report,
ledger §18, handoff. SRS and ADR unchanged — no new architectural decision was
needed.

## L. Test results

| Check | Result |
|---|---|
| Full suite (serial, final tree) | Ran 3939 tests in 1278 s — OK. Log: `var/benchmark/full-suite-release-2026-09-26.log` |
| New tests this gate | 29 (deploy checks 6, scoring recovery 9, jobs redirect 2, profile toggles 1, email logging 2, prompt loading 3, contact location 5, import location 1) |
| ruff check / format | clean, 703 files |
| mypy `apps` | Success, 379 source files |
| `makemigrations --check --dry-run` | No changes |
| `manage.py check` | No issues |
| `load_prompts --check` | every file matches its loaded version |
| Extraction corpus (`resume_benchmark`) | 36/36, macro-F1 1.000, 0 fabrications |
| Sweeps (dev database, final code) | link 1103 visited / 0 broken; notification links 109 / 0 broken; hostile POST 5264 / 0 failures; site 1918 rows / 0 failures; IDOR 317 GET + 519 POST replays / 0 leaks, 0 writes; HTML integrity 997 pages / 0 findings |
| axe + overflow | 40 + 36 + 72 + 16 + 28 = 192 scans, 0 serious/critical, 0 overflow |
| UI polish audit | 40 renders, 0 stray decorations / clipping / sideways scroll |
| Demo seed | run twice; second run changed nothing |

## M. Remaining work

### M.1 NEEDS HUMAN MANUAL TEST
- Sign in by typing each demo password; set up MFA with a real authenticator app and use a recovery code.
- Upload real CVs (DOCX, text PDF, a scan) and judge the parse and the "picture of a CV" message.
- Judge answer quality: interview questions, follow-ups, scores and rationales; CV assistant bullets.
- Spoken practice on a real microphone (and camera on/off).
- Screen reader (NVDA/VoiceOver), keyboard-only, Firefox and Safari, a real phone.
- Two people in a live interview (join window, room, scorecard).
- An employer posts a company update; a follower reads it.
- Leave a finished practice unscored (stop the AI worker) and confirm the result page switches from "being read" to "could not be read" after the retry window.

### M.2 NEEDS EXTERNAL SERVICE TEST
- Real email provider and inboxes (verification, reset, invitation, digests); bounce/complaint feed.
- 8x8 JaaS live video.
- Hosted AI key — re-measure every model limitation in §C.
- ClamAV daemon in the deployed stack.
- Staging/production deployment: TLS, HSTS and the proxy header, backups, alerting, worker and beat processes, `check --deploy` with real configuration.
- DNS/domain verification for company domains.

### M.3 KNOWN MODEL LIMITATION
- Off-topic answer judged relevant keeps its score (false praise removed).
- Confidently wrong claim can get partial credit (e.g. the Parquet case).
- Follow-up can chase an off-topic answer.
- CV assistant can omit a stated result from a bullet.
- Formulaic kit questions; local latency (kit ~3 min, scoring ~40 s per competency, assistant ~40–50 s).

### M.4 NOT IMPLEMENTED
- Payment gateway adapters (deferred by SRS §21).
- Email bounce-ingestion adapter.
- Career roadmap (`career_tools` flag, off).
- CV-aware questions for private practice (a possible future hook; shared pools stay job-only).
- Location read from a line other than the contact line.

### M.5 OUT OF SCOPE
- OCR for scanned CVs (recorded product decision, re-measured and upheld, §D).
- Official Jev or any other decision model (benchmarked and rejected, ledger §17).
- Announcements (D-10) and the message-moderation queue (deferred by SRS §21).

## N. Final file change list

Relative to `freeze-manifest-2026-09-26.sha256`.

Product code: `apps/common/checks.py`, `apps/ai/services/runner.py`,
`apps/ai/services/prompts.py`, `apps/ai/management/commands/load_prompts.py`,
`apps/interviews/tasks.py`, `config/celery.py`,
`portals/candidate/interview_views.py`, `portals/candidate/job_views.py`,
`templates/interviews/result.html`, `templates/dashboard/_profile_body.html`,
`apps/notifications/email.py`, `apps/resumes/parsing/base.py`,
`apps/resumes/parsing/extract.py`, `apps/resumes/services/documents.py`.

Tests: new `apps/common/tests/test_deploy_checks.py`,
`apps/interviews/tests/test_scoring_recovery.py`,
`apps/notifications/tests/test_email_logging.py`; changed
`apps/ai/tests/test_support.py`, `apps/resumes/tests/test_parsing.py`,
`apps/resumes/tests/test_documents.py`, `portals/candidate/tests/test_jobs.py`,
`portals/candidate/tests/test_screens.py`.

Scripts (verification only): new `scripts/idor_sweep.py`,
`scripts/html_integrity_sweep.py`, `scripts/ocr_benchmark.py`.

Docs and configuration: `docs/DEPLOYMENT.md`, `docs/DEVELOPER.md`,
`docs/benchmarks/CV-Extraction.md`, `docs/benchmarks/Loop-Closure-Ledger.md`
(§18), `docs/benchmarks/Manual-Testing-Handoff.md`, this report,
`.env.example`.

No migration. No file removed: the dead-code review found nothing proven
obsolete (`career_tools` is a documented, flag-off placeholder).

## O. Final freeze

| Item | Value |
|---|---|
| Manifest | `var/benchmark/freeze-manifest-2026-09-26b.sha256` — 973 files (previous scope plus `.env.example`) |
| Manifest SHA-256 | `4fa04a9ab763ca7b4e55bf3ba3a3ab17321f581d736b511e63a7e6832b6cf9e4` |
| Metadata | `var/benchmark/freeze-manifest-2026-09-26b.json` |
| Created | 2026-09-26T17:41:18+00:00 |
| Tests | 3939 (Ran 3939 tests in 1278 s — OK) |
| Models | `qwen2.5:7b-instruct` (generation), `nomic-embed-text` (embeddings), local Ollama — unchanged |
| Prompts | interview plan v9, interview evaluate v6, assistant section draft (latest); `load_prompts --check` clean |
| Parser engine | 1.4 |
| Not in manifest | this report and the handoff (written after it) |

**Final state: AUTONOMOUS ENGINEERING COMPLETE.** Production readiness now
depends on §M.1 and §M.2.
