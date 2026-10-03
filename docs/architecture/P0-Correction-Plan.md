# CareerAI — P0 Correction Plan & P1 Gate

**Phases A–C. No code modified.**
**Date:** 15 August 2026
**Baseline verified this session:** 142 tests passing, containers healthy, Postgres 16 + pgvector 0.8.6.

---

## 0. The finding that reframes the gate

Ten corrections were carried into this task. Reconnaissance shows **six of them touch models that do not exist yet.**

Concrete models in the repository today: `User`, `IdempotencyKey`, `AuditLog`, `FeatureFlag`, `Attachment`, `AttachmentAccess`, `OutboxEvent`, `OutboxConsumerRun` — eight, plus three abstract bases (`apps/*/models.py`, verified by inspection).

**Not present:** `ResumeVersion`, `ResumeDraft`, `Application`, `FxRate`, any matching cache, `Conversation`, `ConversationParticipant`, `InterviewQuestionPool`.

So the correction scope splits cleanly:

| | Corrections | Nature |
|---|---|---|
| **Code + docs** | Redis separation, token replay, dependency locking, observability, rate limiting, CSP | Real changes to shipped P0 code |
| **Docs only** | Erasure/redaction, resume version dedupe, FX provenance, match cache invalidation, partitioning threshold, simplifications | Amend SRS/ADR now; the code lands in P5/P2/P13 when those models are built |

This is the good outcome. The three CRITICALs were caught before the models they concern exist, so **CRITICAL-1 costs two column definitions in a document rather than a trigger replacement on a live evidentiary table.** The expensive mistake was avoided by finding it now.

Code-touching work is bounded: roughly six files changed, four small modules added, one migration.

---

# PHASE A — CURRENT VERIFIED STATE

Every row inspected in the repository this session. No claim is carried over from a previous report unverified.

## A.1 The three CRITICAL findings

| # | Finding | Status | Evidence |
|---|---|---|---|
| **C-1** | Erasure impossible: immutable `ResumeVersion` holds PII, cannot be updated or deleted | **VERIFIED as a document contradiction — NOT YET IN CODE** | SRS §19.4 vs §5.5.1 vs DEVELOPER §2.2. No `resumes` app exists; `ls apps/` shows no such module. **Correction is free today** |
| **C-2** | Single-use token state in evictable, non-persistent cache | **VERIFIED — LIVE DEFECT** | `apps/files/signing.py:68` `cache.add(...)`; `config/settings/base.py:144,149,155,158` one `REDIS_URL` for cache + sessions + broker; `SESSION_ENGINE = cache` with no `SESSION_CACHE_ALIAS` |
| **C-3** | Dependencies not reproducible | **VERIFIED** | `requirements.txt` — six of seven runtime deps unbounded `>=`; no lockfile; `.github/workflows/ci.yml:34,67` plain `pip install -r`, no `--require-hashes` |

**C-2 is worse than previously assessed.** `docker-compose.yml` runs Redis with `--save "" --appendonly no` — **persistence is explicitly disabled**. So the consumed-token marker is lost not only on eviction but on *any Redis restart*. A deploy, an OOM kill or a host reboot silently re-arms every outstanding download token. Moving the marker to a `noeviction` Redis database, as the previous review proposed, **would not have fixed this.** That recommendation is withdrawn (§B.2).

## A.2 The additional findings

| # | Finding | Status | Evidence |
|---|---|---|---|
| **O-1** | Request ID does not reach Celery; NFR-10 claims it does | **VERIFIED** | No `request_id` reference in `apps/files/tasks.py`, `apps/notifications/tasks.py`, `config/celery.py`. `apps/common/logging.py` uses a ContextVar, which does not cross a process boundary |
| **O-2** | Only one of six required identifiers exists | **VERIFIED** | `request_id` only. No actor, company, task, event or AI-run ID in the log envelope |
| **O-3** | `RequestIDMiddleware` is **last** in the chain | **VERIFIED — new finding** | `config/settings/base.py` MIDDLEWARE, index 8 of 9. Session, CSRF and auth all run *before* the ID is bound, so any log or exception they emit carries `request_id: "-"` — including authentication failures, which is exactly where correlation matters |
| **R-1** | Rate limiting absent | **VERIFIED** | Zero matches for `ratelimit\|throttle` across `apps/` and `config/` |
| **CSP-1** | No CSP sent at all | **VERIFIED** | Zero matches for `Content-Security-Policy` anywhere. A31 describes a policy the application does not implement |
| **RV-1** | Resume version spam on unchanged analysis | **VERIFIED as a document defect only** | SRS §5.5.1 line 496–498. No resume code exists |
| **FX-1** | FX provenance lost | **VERIFIED as a document defect only** | SRS §5.3 line 460 — "computed on save from the rate current at that moment", no rate reference stored. No `taxonomy` app exists |
| **MC-1** | Match cache never invalidates on new jobs | **VERIFIED as a document defect only** | SRS §10.1 line 1054 — key is `(profile_id, algorithm_version, profile_updated_at)`. No matching code exists |
| **P-1** | `AuditLog` / `AttachmentAccess` unpartitioned | **VERIFIED — accepted** | Both plain tables; no inbound FKs; `created_at` in every index. Partition-ready as intended |

## A.3 What was verified as correct and needs no change

| Area | Status | Evidence |
|---|---|---|
| Outbox transactional guarantee | **VERIFIED CORRECT** | `events.emit` raises outside a transaction; tested with `TransactionTestCase` real commit boundary |
| Consumer idempotency | **VERIFIED CORRECT** | `OutboxConsumerRun` unique `(event, consumer)`; retry re-runs only failures |
| Private storage has no URL | **VERIFIED CORRECT** | `PrivateFileSystemStorage.url()` raises; deploy check probes the backend |
| Scan-before-process, unscannable = unsafe | **VERIFIED CORRECT** | `apps/files/services.py`; tested |
| Append-only audit | **VERIFIED CORRECT** | `AppendOnlyModel` rejects update and delete; tested |
| Magic-byte detection incl. DOCX container | **VERIFIED CORRECT** | `apps/files/sniff.py`; plain ZIP rejected |
| Celery task hygiene | **VERIFIED CORRECT** | Primitive args, explicit queues, `acks_late`, max_retries |
| Frontend intact | **VERIFIED CORRECT** | 28 pages render; mock layer untouched in 11 files |
| Six Celery queues | **VERIFIED — but `match` and `ai` both unused** | `config/celery.py:30` |

---

# PHASE B — ARCHITECTURE CORRECTIONS

Ten corrections. For each: current design, why it fails, corrected design, affected files, database impact, migration risk, tests, rollback.

---

## B.1 — CRITICAL-1: Privacy redaction of immutable resume versions *(documentation only)*

**Current design.** `ResumeVersion` is append-only, enforced three ways including a `BEFORE UPDATE` trigger; `Application.resume_version` is `PROTECT`; §19.4 promises purge and destruction of the snapshot.

**Why it fails.** The row holds contact details, employment history and free text. It can neither be updated nor deleted. FR-1.8 is unimplementable, and the failure would be discovered in P5 or P8 *after* the guarantee is depended on.

**Corrected design — the complete erasure model.**

Redaction is a **single legal transition**, enforced by the trigger:

```
redacted_at IS NULL  →  redacted_at IS NOT NULL
```

Permitted to change in that transition, and nothing else:
`content` → fixed tombstone document · `content_schema_version` → the tombstone schema · `redacted_at`, `redaction_reason`, `redacted_by`.

Every other column — `document_id`, `version_no`, `sha256`, `template_snapshot`, `created_by`, `created_reason`, `finalized_at`, `U(document, version_no)` — is byte-identical afterwards. The trigger rejects any update where an unlisted column differs, and rejects any update where `redacted_at` is already non-null. **Redaction is itself single-use.**

`sha256` is deliberately retained: it is the evidentiary anchor proving what was submitted, and a hash of destroyed content is not personal data.

**Every store that must be redacted or destroyed** — the question the brief asks explicitly:

| Store | Holds PII? | Action on erasure |
|---|---|---|
| `ResumeVersion.content` | Yes | Redact to tombstone; keep row, hash, lineage |
| `ResumeDraft.content` + snapshots | Yes | **Hard delete** — nothing references them |
| `ResumeParse.raw_text` / `structured` | Yes, heavily | **Hard delete** — derived, no evidentiary role |
| `ResumeUpload` + `Attachment` bytes | Yes | Purge from bucket; verify absence, not just row removal |
| `ResumeRender` files | Yes | Purge; regenerable only from redacted content, so gone permanently |
| `ResumeAnalysis` / `ResumeFinding` | **Yes — quotes CV lines as evidence** | Hard delete. Easy to overlook: findings embed verbatim resume text |
| `CandidateProfile` + children | Yes | Anonymise to tombstone |
| **Embeddings** (`profile.embedding`) | **Yes — a vector is derived personal data and is partially invertible** | **Null the column.** Not "leave it, it's just numbers" |
| **`AIRun.input_snapshot` / `output`** | **Yes — resume text was sent to a provider** | **Delete rows for the erased subject.** The most commonly missed store |
| `Application.answers`, `job_snapshot` | Answers may contain PII | Redact `answers`; `job_snapshot` is employer data, keep |
| `Application.match_snapshot` | Skill names only | Keep |
| `Message.body` | Yes | Hard delete; conversation shell survives |
| Interview transcripts | Yes | Hard delete |
| `AttachmentAccess`, `AuditLog` | Actor IDs | **Keep.** Pseudonymised by the tombstone; required for the audit of the erasure itself |
| `AnalyticsEvent` | `actor_id` | Anonymise `actor_id`, keep aggregates |

**Provider-side residue is a stated limit, not a solved problem:** text sent to an AI provider cannot be recalled. `AIRun` rows are deleted on our side and the privacy policy says so plainly — as it already does for documents delivered to employers.

**Affected files.** SRS §5.5.1, §5.5.2, §5.11, §19.4; ADR A8, A19; new ADR A38.
**Database.** Three columns on a table that does not exist yet + one trigger definition. **Zero migration.**
**Migration risk.** **None now. Severe after P5/P8.**
**Tests (written with the model in P5).** Ordinary update still raises · redaction succeeds once · second redaction raises · redaction that alters `sha256` raises · application FK survives · `content` unrecoverable · analysis/parse/embedding/AIRun rows gone · audit entry written.
**Rollback.** Documentation revert; no data affected.

---

## B.2 — CRITICAL-2: Security token state *(code — live defect)*

**Current design.** `apps/files/signing.py:68` marks a token consumed with `cache.add(jti, 1, ttl)` against the default cache — Redis database 0, shared with sessions and the Celery broker, **with persistence disabled** in compose.

**Why it fails.** Three independent ways to lose the marker: LRU eviction under memory pressure; `cache.clear()` during a routine feature-flag operation; **and any Redis restart, because `--save "" --appendonly no` means there is no persistence at all.** Losing the marker re-arms a signed download token for a private CV. A confidentiality guarantee is resting on a store explicitly configured to lose data.

**Corrected design — evaluated against the brief's criteria.**

| Option | Correctness | Replay protection | Failure behaviour | Concurrency | Performance | Simplicity |
|---|---|---|---|---|---|---|
| Redis `SET NX`, `noeviction` db | Good until restart | Lost on restart | **Silently fails open** | Atomic | ~0.2 ms | Simple |
| Redis + AOF persistence | Good | Survives restart | fsync window still loses recent markers | Atomic | ~0.3 ms | Adds an ops obligation |
| **Postgres nonce ledger** | **Exact** | **Durable** | **Fails closed** — no database, no download | Unique constraint | ~1 ms on a path already doing several queries | **Simplest to reason about** |

**Selected: a Postgres-backed single-use token ledger,** `apps/common.ConsumedToken(jti UUID unique, purpose, expires_at, consumed_at)`.

Reasoning: consumption is a *security decision*, and security decisions belong where the transaction is. The download path already performs several queries and writes an `AttachmentAccess` row, so one indexed insert is not a meaningful cost. Concurrency is handled by the unique constraint — the loser gets `IntegrityError`, mapped to `NotAllowed`. Failure mode is the right one: if Postgres is down nothing is served at all, so a token cannot be replayed during an outage.

Generic rather than file-specific because the SRS already mandates single-use tokens for password reset, invites and interview join. One ledger, one purge job.

**Redis is separated regardless** (A35): `/0` broker, `/1` cache, `/2` sessions, with `SESSION_CACHE_ALIAS` bound explicitly. Distinct memory and eviction policy per role; `cache.clear()` can no longer log anyone out. Persistence stays off for cache, which is now correct because nothing security-critical lives there.

**Affected files.** `apps/common/models.py` (+ledger), `apps/common/tokens.py` (new), `apps/files/signing.py`, `config/settings/{base,dev,test}.py`, `docker-compose.yml`, `.env.example`, `apps/common/management/commands/purge_consumed_tokens.py`.
**Database.** One small table; one migration.
**Migration risk.** Low — additive, no existing data.
**Tests.** Replay rejected after restart-equivalent (cache flushed) · concurrent consumption: exactly one winner · cache eviction cannot re-arm a token · `cache.clear()` does not log out · sessions and broker on separate databases.
**Rollback.** Revert `signing.py` to the cache path; the table becomes inert.

---

## B.3 — CRITICAL-3: Dependency reproducibility *(code)*

**Current design.** Unbounded `>=` on six runtime deps; no lock; CI `pip install -r requirements-dev.txt`.

**Why it fails.** Two installs a week apart differ. CI can go red with no commit between. `pip-audit` validates the resolution it happens to find.

**Corrected design — evaluated on the brief's constraints (Windows dev, Linux deploy, CI, Django, 3.12).**

| Tool | Verdict |
|---|---|
| Poetry / PDM | Rejected — replaces the whole workflow and adds a runtime for one problem |
| **uv** | Strong: fast, universal lock, single binary, Windows-native. Rejected *for now* only because it changes the install command everywhere for a 12-dependency project |
| **pip-tools** | **Selected** — stays `pip`, `.in` → pinned `.txt` with `--generate-hashes`, install with `--require-hashes`. Smallest possible change to an already-working workflow |
| Constraints file | Rejected — pins without hashes; solves half the problem |

`requirements.in` / `requirements-dev.in` hold intent; compiled files hold the graph with hashes. Compilation runs on Linux (matching deployment); Windows developers install the same pinned file. Upgrades become a reviewable diff, which is the actual goal.

**Affected files.** New `requirements*.in`; regenerated `requirements*.txt`; `.github/workflows/ci.yml`; README; DEVELOPER.md §9.
**Database.** None.
**Migration risk.** Low, but real: `--require-hashes` fails hard on any platform-specific wheel divergence. Mitigation: verified locally on Windows before committing.
**Tests.** A CI job that installs from the lock with `--require-hashes` on a clean runner. That *is* the test.
**Rollback.** Keep the previous `requirements.txt` for one release.

---

## B.4 — Observability envelope *(code)*

**Current design.** `RequestIDMiddleware` sets a ContextVar — and sits **last** in MIDDLEWARE. Nothing propagates to Celery.

**Why it fails.** A ContextVar cannot cross a process boundary, so every worker log is uncorrelated — and workers are where the interesting failures live. Being last means session, CSRF and auth all log with `request_id: "-"`, including authentication failures. NFR-10 asserts behaviour the code does not have.

**Corrected design (A37).** One `LogContext` — `request_id`, `actor_id`, `company_id`, `task_id`, `event_id`, `ai_run_id` — held in a single ContextVar, emitted by the JSON formatter, and propagated explicitly:

- **Middleware moves to index 0**, so everything downstream is covered.
- **`before_task_publish`** copies the current context into task headers.
- **`task_prerun`** restores it and adds `task_id`; **`task_postrun`** clears it.
- **Outbox dispatch** binds `event_id` per event; **`ai.run`** binds `ai_run_id`.
- `actor_id` / `company_id` are bound by the auth and tenancy layers in P1.

Explicit propagation, no thread-local reliance, exactly as the brief requires.

**Affected files.** `apps/common/logging.py`, `apps/common/middleware.py`, `config/settings/base.py` (order), `config/celery.py` (signals), `apps/notifications/services/outbox.py`.
**Database.** None.
**Migration risk.** None.
**Tests.** Context survives HTTP → service → outbox → Celery · `task_id` present in worker logs · context cleared between tasks (no bleed) · unset fields omitted rather than `"-"`.
**Rollback.** Revert; logging degrades, nothing breaks.

---

## B.5 — Rate limiting *(code)*

**Current design.** None.

**Why it fails.** Credential stuffing, upload flooding and AI-cost abuse are unmitigated, and the SRS claims otherwise.

**Corrected design.** A ~80-line fixed-window limiter in `apps/common/ratelimit.py` over the Redis cache — no framework. A sliding window is not worth the complexity for these limits.

| Scope | Key | Window | Limit | On exceed | Failure policy |
|---|---|---|---|---|---|
| Login | `ip` + `email` | 15 min | 5 / identity, 20 / IP | 429 + progressive delay | **Fail closed** |
| Signup | `ip` | 1 h | 5 | 429 | Fail closed |
| Password reset | `email`, `ip` | 1 h | 3, 10 | 429, generic message | Fail closed |
| Upload | `user` | 1 h | 20 | 429 | Fail closed |
| AI request | `user` | 1 h | tier-dependent | `QuotaExceeded` | Fail closed |
| Job search | `user`/`ip` | 1 min | 60 | 429 | **Fail open** |
| Candidate search | `membership` | 1 min | 30 | 429 | Fail closed |
| Webhook receipt | `endpoint` | 1 min | 120 | 429 | Fail open |

Two dimensions everywhere (identity and IP) so one attacker cannot lock out a victim by targeting their email alone. Staff bypass is explicit and audited. **The fail-open/fail-closed split is the important decision:** if Redis is unavailable, authentication limits must refuse rather than admit, while search limits must not take the product down.

Domain limits (applications per day, messages per company per candidate) are service-level, not middleware — they are business rules.

**Affected files.** New `apps/common/ratelimit.py`; `config/settings/base.py`; applied to real endpoints in P1 when auth views exist.
**Database.** None.
**Migration risk.** None.
**Tests.** Limit enforced · window resets · two dimensions independent · fail-closed on Redis outage for auth · fail-open for search · staff bypass audited.
**Rollback.** Feature flag disables enforcement.

---

## B.6 — Content Security Policy *(code)*

**Current design.** None sent.

**Why it fails.** A31 documents a policy the application does not implement; the review's assumption that a weak CSP existed was wrong. Stored-XSS defence currently rests on template auto-escaping alone.

**Corrected design.** ~40-line nonce middleware, no `django-csp`.

```
default-src 'self';
script-src 'self' 'nonce-{nonce}' 'unsafe-eval';
style-src 'self' 'unsafe-inline';
img-src 'self' data:;
object-src 'none'; base-uri 'self'; frame-ancestors 'none';
form-action 'self'; connect-src 'self'
```

Compatibility, verified against what the frontend actually does: the inline theme script takes the nonce; **Alpine needs `unsafe-eval`** (`new Function`) until the `@alpinejs/csp` build is adopted in P18 — this is A31's accepted, scheduled debt; **HTMX needs nothing special**; **Tailwind is build-time**, so `style-src` needs `unsafe-inline` only for Alpine's `x-bind:style`; **inline SVG is markup, not script**, so it is unaffected. `frame-ancestors 'none'` and `object-src 'none'` close clickjacking and plugin vectors today.

Report-only mode first, then enforce — so a missed inline script surfaces as a report rather than a broken page.

**Affected files.** New `apps/common/csp.py`; `config/settings/base.py`; `templates/partials/_theme_script.html`; `templates/base.html`.
**Database.** None.
**Tests.** Header present · nonce unique per request · inline theme script carries it · smoke suite still renders · no console errors.
**Rollback.** Report-only, or remove the middleware.

---

## B.7 — Resume version semantics *(documentation only)*

**Current.** SRS §5.5.1 lists "requests an analysis" as version-creating and defends it on AI cost, not row count.
**Fails because.** Ten clicks on unchanged content produce ten identical versions — the exact noise the draft/version split exists to prevent.
**Corrected.** Finalize on analysis **only if** the draft content hash differs from `current_version.sha256`; otherwise reuse. Analysis still always references an immutable version, so FR-4.1 and score history are unaffected.
**Files.** SRS §5.5.1, FR-3.8. **Database:** none. **Risk:** none. **Tests (P5):** analyse twice unchanged → one version; edit between → two. **Rollback:** doc revert.

---

## B.8 — FX provenance *(documentation only)*

**Current.** `salary_min_base` computed "from the rate current at that moment"; no rate reference stored.
**Fails because.** A historical match cannot be reproduced or explained, and the exit condition demands provenance.
**Corrected.** Every salary-bearing model stores: source currency, period, normalised annual amount, conversion currency, **`fx_rate_id` FK, `fx_rate_value` (denormalised, so provenance survives an FX row change), `fx_as_of`, `fx_source`**. `FxRate` gains `source` and `provider_ref` and is append-only — historical rates are never mutated, only superseded.
**Files.** SRS §5.3, §5.4, §5.7; ADR A39. **Database:** columns on models not yet built. **Risk:** none now. **Tests (P2):** re-running a historical conversion reproduces the stored figure exactly, even after the rate table is refreshed. **Rollback:** doc revert.

---

## B.9 — Match cache invalidation *(documentation only)*

**Current.** Key `(profile_id, algorithm_version, profile_updated_at)`.
**Fails because.** A newly published job cannot appear until the candidate edits their profile or the TTL expires — stale exactly when it matters.
**Corrected.** Key becomes `(profile_id, algorithm_version, embedding_model, profile_updated_at, job_generation)` where `job_generation` is a monotonic counter in the durable cache, incremented on `job.published` / `job.updated` / `job.closed`. TTL 15 minutes as a backstop, not the mechanism. Adding `embedding_model` closes a second staleness class the previous review missed: a model change would otherwise serve vectors from the old model out of cache.
**Files.** SRS §10.1; ADR A21 amended. **Database:** none. **Tests (P13):** publishing a job invalidates every feed; changing the embedding model invalidates; scoring version bump invalidates. **Rollback:** doc revert.

---

## B.10 — Partitioning threshold *(documentation only)*

**Decision, recorded as an amendment:** do **not** partition now. Keep `AuditLog`, `AttachmentAccess`, `AnalyticsEvent`, `AIRun`, `JobView` partition-ready — no inbound FKs, `created_at` in every index, both verified present today.

Convert when **any one table passes 20M rows**, on this checklist: review indexes, confirm retention, review query patterns, rehearse on a restored dump, convert in a controlled window. Monitoring lands with P16's dashboards; a row-count alert at 15M gives lead time.

**Files.** SRS §5.1.8; ADR A40. **Database:** none. **Risk:** none — the conversion is cheap while small and the threshold is set well below where it stops being cheap.

---

# PHASE C — BUILT-IN CRITIC

Each correction attacked. Three problems found in my own corrections; all three are fixed above before any code is written.

## C.1 Attacking the erasure design (B.1)

**"What if the same resume version is referenced by 1,000 applications?"** Redaction is one row update; all 1,000 FKs stay valid and every `submitted_render_sha256` still proves what was sent. This is precisely why redaction beats deletion — deletion would need 1,000 `PROTECT` violations resolved.

**"What if AI output contains personal data?"** It does — `AIRun.input_snapshot` holds resume text and `output` holds findings quoting it. **This was missing from my first draft of the erasure table and is now included.** It is the single most commonly missed store in a Django erasure implementation.

**"Do embeddings retain deleted candidate information?"** Yes. A 768-dimension vector derived from a résumé is derived personal data and is partially invertible. Nulling the column is in the table. "It's just numbers" is not a defence.

**"What if erasure runs while an analysis task is in flight?"** *Found by this question.* The task would write `ResumeAnalysis` rows containing CV quotes **after** erasure completed. **Fix:** erasure sets `CandidateProfile.erasure_started_at` first; every AI and analysis task checks it on entry and aborts; the erasure job runs after a drain window. Added to the design.

**"Does redaction break the immutability guarantee it was carved out of?"** Only if the trigger is loose. It permits exactly one transition, only from null, with every other column compared byte-for-byte. Redaction is itself single-use. A second attempt raises.

**"What if a regulator asks us to prove what was submitted, after erasure?"** `sha256`, `version_no`, `finalized_at` and the application event chain all survive. We can prove *that* a specific document was submitted and when, without holding its content. This is the correct trade.

## C.2 Attacking the token ledger (B.2)

**"What if Postgres is unavailable?"** No downloads at all — the read is already gated on a database query for the attachment. Fails closed, which is right for a confidentiality control.

**"Concurrent replay — two requests, same token, same millisecond?"** The unique constraint decides. One inserts, one gets `IntegrityError` → `NotAllowed`. No lock, no race.

**"Does this table grow without bound?"** Yes without a purge. `expires_at` plus a daily command, matching the pattern already used for idempotency keys.

**"Is a database write on the download path too slow?"** The path already runs several queries and writes `AttachmentAccess`. One indexed insert is noise beside streaming a file.

**"What if a token is consumed but the response then fails?"** The token is burned and the user re-requests. Correct: a token that survives a failed delivery is a token that can be replayed.

**"Could you have kept Redis with AOF instead?"** Considered and rejected in the table. AOF still has an fsync window, and it would make a security guarantee depend on an ops setting nobody re-checks after a container image bump.

## C.3 Attacking dependency locking (B.3)

**"What if a Windows wheel differs from the Linux one under `--require-hashes`?"** The real risk with pip-tools. `psycopg[binary]` and `argon2-cffi` ship platform wheels. Mitigation: verify the compiled file installs on Windows before committing, and document `uv` as the escape hatch if divergence appears — it produces a genuinely universal lock.

**"Does this slow developers down?"** Adding a dependency becomes edit-`.in`, recompile, commit. That friction is the feature.

## C.4 Attacking the observability envelope (B.4)

**"What if context leaks between tasks in a pooled worker?"** *Found by this question.* A ContextVar set in `task_prerun` and never cleared persists into the next task on the same worker thread, attributing one user's work to another in the logs. **Fix:** `task_postrun` resets the token unconditionally, and a test asserts no bleed across two tasks. Added.

**"Does moving middleware to index 0 break anything?"** It runs before `SecurityMiddleware`, so it cannot short-circuit an SSL redirect — it only reads a header and sets a ContextVar. Safe, and it is the only position where auth failures get an ID.

**"What if a header is spoofed?"** Already length-capped and charset-restricted; `actor_id` and `company_id` are bound server-side from the session, never from headers.

## C.5 Attacking rate limiting (B.5)

**"What if Redis is down?"** The fail-open/fail-closed split is the whole answer: auth refuses, search proceeds. A single global policy would be wrong in one direction or the other.

**"Can an attacker lock out a victim by hammering their email?"** Limits are per-identity *and* per-IP; the identity limit uses progressive delay rather than a hard lock, so a victim is slowed, never locked out by a third party.

**"Fixed window allows a 2× burst at the boundary."** True and accepted. For 5 logins per 15 minutes, 10 in a boundary-straddling instant is not a meaningful attack, and a sliding window costs complexity these limits do not justify.

## C.6 Attacking CSP (B.6)

**"Does `unsafe-eval` make this theatre?"** No. Nonces still block injected inline scripts, which is the stored-XSS vector that matters for resume content. `unsafe-eval` only helps an attacker who already achieved execution.

**"What if a template has an inline script nobody remembers?"** Report-only first, then enforce — that is exactly what the report phase is for. Verified: only `_theme_script.html` and `base.html` contain `<script>`.

## C.7 Attacking the simplifications

Reassessed against the repository rather than accepted from the previous review.

| Proposal | Decision | Reasoning from the code |
|---|---|---|
| Six queues → four | **MODIFY — defer** | `ai` and `match` are both unused today. Merging is a one-line change with zero benefit until P11/P13, and splitting later is equally cheap. Revisit at P11 |
| Drop `ConversationParticipant` | **REMOVE — confirmed** | SRS §5.9 fixes a conversation to one company and one candidate. A join table for a permanently 2-party relation is generality with no case behind it. Doc-only; the table does not exist |
| Interview question pool → JSONB | **REMOVE the table — confirmed** | Never queried across templates. Doc-only |
| One delivery ledger instead of two | **MODIFY — do not generalise yet** | `OutboxConsumerRun` exists and works. `NotificationDelivery` (P10) needs a *recipient* dimension the current table lacks. Generalising now is speculative; the P10 decision is to extend rather than add. Recorded, not implemented |
| Fold `apps/common/types.py` | **KEEP** | Two lines, one clear purpose, avoids a circular import. Merging saves nothing |

**Net: two tables avoided, one deferral recorded, nothing removed from working code.** The previous review's "five removed" was optimistic — two of those were premature.

## C.8 Cross-cutting scenarios

**"Two browser tabs autosave simultaneously?"** Unresolved and out of P0 scope — no draft model exists. The policy (last-write-wins per tab token, `row_version` for structural operations) is recorded for P5.

**"Same recruiter in two companies?"** Structurally safe via URL tenancy. **But** §14 of the quality loop found the gap that matters: services take `request`, so a Celery task touching employer data has no tenant scope. Fix belongs in P1 with the permission layer — services take explicit `actor` and `company`. Recorded as a P1 entry condition.

**"Malicious user accesses another company's object?"** Held. Queryset scoping plus the CI check, both verified present.

---

# PROPOSED PHASE D SCOPE

Awaiting approval before any code changes.

## Will change (code)

| # | Change | Files | Est. tests |
|---|---|---|---|
| 1 | Postgres `ConsumedToken` ledger; `signing.py` uses it | +2 files, 1 migration, 1 command | 5 |
| 2 | Redis separation `/0` `/1` `/2` + `SESSION_CACHE_ALIAS` | 4 settings/compose files | 3 |
| 3 | `requirements*.in` + compiled locks with hashes; CI `--require-hashes` | 5 files | CI job |
| 4 | `LogContext` envelope + Celery signal propagation + middleware to index 0 | 5 files | 6 |
| 5 | `apps/common/ratelimit.py` | +1 file, settings | 7 |
| 6 | `apps/common/csp.py` + nonce in templates | +1 file, 3 edits | 5 |

Roughly **6 new modules, ~12 edited files, 1 migration, ~26 new tests.** No product surface, no domain models, nothing from P1+.

## Will change (documents)

SRS → **v1.2**: privacy redaction semantics and the full erasure store table · Redis separation · dependency reproducibility · observability propagation · rate limiting · CSP · FX provenance · match cache invalidation · partitioning threshold · resume version dedupe.

ADR → **A38** redaction-permitting immutability · **A39** FX provenance · **A40** partitioning deferral · **A35/A36/A37** Redis separation, dependency locking, observability envelope. A8, A19, A21 amended with history preserved, never overwritten.

DEVELOPER.md → lockfile workflow, rate-limit and CSP conventions, logging context rules.

## Will not change

No candidate or employer portal, no matching engine, no resume models, no interviews, no billing, no AI provider implementation. The erasure, FX, match-cache and version-dedupe corrections are **documented now and implemented in P5/P2/P13** with the models they describe.

## Two questions for you

1. **pip-tools or uv?** I recommend pip-tools as least disruptive; uv is faster with a genuinely universal lock and would remove the Windows/Linux wheel risk entirely. Either is defensible.
2. **CSP report-only first, or enforce immediately?** I recommend report-only for one deploy. Only two templates contain inline scripts, so enforcing straight away is low-risk — but report-only costs nothing.

---

# P1 GATE — CURRENT STATUS

| Requirement | Status |
|---|---|
| Zero unresolved CRITICAL issues | **BLOCKED** — three open, all with agreed designs |
| No cross-tenant vulnerability | **PASS** |
| Token replay protection correct | **BLOCKED** — B.2 |
| Privacy erasure implementable | **BLOCKED** — B.1 (documents) |
| Immutability/erasure compatible | **BLOCKED** — B.1 |
| Redis architecture safe | **BLOCKED** — B.2 |
| Dependency install reproducible | **BLOCKED** — B.3 |
| Observability connected | **BLOCKED** — B.4 |
| Rate limiting baseline | **BLOCKED** — B.5 |
| CSP baseline | **BLOCKED** — B.6 |
| Resume version semantics correct | **BLOCKED** — B.7 (documents) |
| FX provenance preserved | **BLOCKED** — B.8 (documents) |
| Match cache invalidation defined | **BLOCKED** — B.9 (documents) |
| Outbox semantics correct | **PASS** — verified in code and tests |
| Celery idempotency defined | **PASS** |
| Failure recovery defined | **PASS** |
| SRS/ADR consistent with implementation | **BLOCKED** — NFR-10 and A31 currently overstate the code |
| Tests verify critical corrections | **BLOCKED** — pending Phase D |
| CI passes | **PASS** — 142 tests, all gates green this session |

# P1 BLOCKED

Four of eighteen conditions pass. The remaining fourteen have agreed designs and no unknowns; ten are closed by documentation and six by roughly a day of code.

Phase D on your approval.
