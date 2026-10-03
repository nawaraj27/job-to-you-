# CareerAI — Iterative Architecture Quality Loop

**Date:** 15 August 2026
**Inputs treated as evidence, not authority:** SRS v1.1, ADR.md (A1–A34), DEVELOPER.md, and the P0 code now on disk.
**Method:** three design/critique iterations, then failure simulation, attack pass, simplification and cost control.
**Headline:** the architecture is sound in its expensive-to-change decisions. It contains **three CRITICAL defects**, one of which is live in shipped P0 code. Two of the three are self-contradictions *inside* the frozen SRS that only became visible once the code existed.

---

## 1. Executive summary

A note on framing first, because it changes what this document can honestly conclude: **P0 is already built, tested and verified.** The requested verdict form ("APPROVE P0 / DO NOT START P0") is therefore partly retrospective. I answer it in §31 in the only way that is truthful — retrospectively for P0, prospectively for P1 — rather than pretending the code does not exist.

That code turned out to be the most valuable input to this review. Two contradictions in the SRS were invisible while it was prose and obvious once there was a trigger, a cache backend and a foreign key to look at.

**What survived scrutiny.** The nine irreversible decisions (A1 membership authorization, A2 Postgres+pgvector, A4 permissions-first, A7 dual representation, A8 immutable submitted version, A13 skill taxonomy, A18 URL tenancy, A19 draft/version split, A22 CandidateProfile as root) all hold under adversarial questioning. So do the outbox (A14/A25), the task-level AI abstraction (A10/A11) and the deterministic matcher (A6). These are the decisions that are expensive to change later, and they are right.

**What did not.** Erasure is architecturally impossible as specified. The single-use file token lives in a cache that may evict it. Dependencies are not reproducible. Analysis creates unbounded versions. FX provenance is lost. Observability claims something the code does not do. Rate limiting exists only as prose.

**Three iterations were required.** Iteration 1 surfaced 21 issues; iteration 2 fixed 14 and introduced 2 new ones; iteration 3 closed those and resolved the last CRITICAL. The score moved 78 → 86 → 91 on architecture, but production readiness only 62 → 71, because several findings are unbuilt rather than misdesigned.

---

## 2. Current repository state

### 2.1 What actually exists

| Layer | State | Evidence |
|---|---|---|
| Frontend | **Complete and working.** 28 pages render; design system, feature flags, navigation, HTMX command endpoints, Alpine, Tailwind v4 | `apps/core/tests/test_smoke.py` passes |
| Mock data | **Still live.** `samples.py` in resume/jobs/employers/dashboard/notifications; `state.py` session stores in resume/jobs/employers/dashboard/notifications/preferences | ~3,500 lines, scheduled for deletion P3–P10 |
| Platform apps | **Built (P0).** `common`, `audit`, `files`, outbox in `notifications` | 142 tests, 96% on gated modules |
| Domain apps | **Not built.** candidates, companies, jobs, applications, messaging, interviews, matching, ai, billing, analytics, taxonomy | Per roadmap |
| Portals | **Not created.** Views still live in their original apps (A30 incremental) | Intentional |
| Infrastructure | Postgres 16 + pgvector 0.8.6, Redis 7, Celery 5.4 with six queues, CI workflow written but never executed on GitHub | Verified locally |

### 2.2 Classification

**Works and must be preserved:** the entire frontend contract; `apps/core/features.py` as the shipping gate; the `.../act/` command-endpoint pattern; the SSR + HTMX + Alpine stack. No SPA migration is warranted and none is proposed.

**Mock, scheduled for replacement:** all `samples.py` and `state.py`; `apps/core/demo.py` persona resolution.

**Architectural debt introduced in P0 (declared, not hidden):** `Attachment` lacks owner FKs (targets do not exist yet); `AuditLog` lacks `company`; `AttachmentAccess` and `AuditLog` are unpartitioned pending a ruling; `AllowAllScanner` is the dev default.

**Contradictory:** four items, all found in this review — §19.4 erasure versus §5.5.1 immutability; NFR-10's request-ID propagation versus the code; §5.3's FX normalisation versus the FX-provenance requirement; §15.4's rate limiting versus the absence of any mechanism.

**Incomplete:** rate limiting, CSP, observability context beyond request ID, dependency locking, partitioning decision.

---

## 3. Architecture decisions verified

Each was attacked and survived on evidence.

| ADR | Verdict | What the attack was, and why it held |
|---|---|---|
| **A1** membership authorization | **Verified** | "Recruiter at two companies with 500 colleagues" — expressible with no special case. A `role` column cannot do it at any size |
| **A2** Postgres + pgvector | **Verified** | Exclusion constraints, partial unique indexes, `SKIP LOCKED` and vectors are all load-bearing; three are already in use in P0 |
| **A3** no parallel JSON API | **Verified** | The service layer is genuinely HTTP-ignorant in the P0 code, which is the condition that makes this safe to defer |
| **A4** permissions in P1 | **Verified** | Strengthened by P0: the CI check banning unscoped querysets in portals only works if it predates the queries |
| **A6** deterministic ranking | **Verified** | The cost argument is unanswerable, and the dispute/bias argument is stronger still: a stored breakdown is defensible, a model score is not |
| **A8** immutable submitted version | **Verified in intent, broken in mechanism** — see CRITICAL-1 |
| **A10/A11** task-level AI + AIRun | **Verified** | Survives provider outage, API change, malformed JSON and cost audit. `AIRun` is the single most useful table in the design |
| **A14/A25** outbox + two-stage fan-out | **Verified** | Rollback-safety is enforced in code (`events.emit` refuses outside a transaction) and tested with a real commit boundary |
| **A18** URL tenancy | **Verified** | The two-tab scenario is structurally impossible. Confirmed no session dependency exists in the P0 code |
| **A19** draft/version split | **Verified in principle**, defective in one path — see HIGH-1 |
| **A22** CandidateProfile as root | **Verified** | And it is what makes erasure *nearly* clean — the remaining obstacle is CRITICAL-1, not this decision |
| **A27** schema/model versioning | **Verified** | Prevents two silent failure classes. Cheap and correct |
| **A29** signals banned | **Verified** | P0 contains zero business-logic signals; the outbox absorbed every case that would have used one |
| **A30** incremental restructure | **Verified** | P0 shipped without touching a single portal, and the frontend smoke test proves nothing regressed |
| **A32** guest uploads unprocessed | **Verified** | Closes an unauthenticated cost and malware path. Implemented and tested |

## 4. Architecture decisions rejected

None of the 34 ADRs is rejected outright. Two are amended (§5). This is itself worth stating plainly: after three adversarial passes, the frozen decision set holds. The failures are in **mechanisms and unbuilt areas**, not in the decisions.

## 5. Architecture decisions changed

| ID | Change | Reason |
|---|---|---|
| **A8 / A19 amended** | Immutability becomes **"no semantic update"**, not "no update ever". A single privileged transition — content redaction under erasure — is permitted, gated by a dedicated column and audited | CRITICAL-1: erasure is otherwise impossible |
| **A21 amended** | Match cache key gains a **job-corpus generation counter** | HIGH-4: profile-keyed invalidation alone never surfaces new jobs |
| **New A35** | **Redis is partitioned by role**: separate logical databases for cache, sessions and broker, with a durable store for security-critical single-use tokens | CRITICAL-2 |
| **New A36** | **Dependencies are locked** with hashes; `requirements.in` → compiled `requirements.txt` | CRITICAL-3 |
| **New A37** | **Observability context is a propagated envelope** (request, actor, company, task, event, AI run), carried into Celery | HIGH-6 |
| **§5.1.8 amended** | Partitioning is **deferred to P16 with a documented conversion**, for `AuditLog`, `AttachmentAccess`, `AnalyticsEvent`, `AIRun`, `JobView` — the open question from the P0 report, now answered | Cost/benefit at current volume; see §17 |

---

## 6. Database review

### Design
Entities, keys, delete behaviour, versioning and indexes are defined in SRS §5 and were re-walked entity by entity. Boundaries: one aggregate root per domain; no cross-domain model imports for writes.

### What holds
Every core entity has an owner. No duplicate source of truth after A23 collapsed the four-way application state. Money is decimal-plus-currency. Enums are `TextChoices` mirrored by `CHECK`. Business uniqueness is at the database, not in services — one live application per job, one primary resume, one live offer, one active membership.

### Findings

**CRITICAL-1 — erasure is architecturally impossible.**
§19.4: versions are "purged" and "the resume snapshot destroyed". §5.5.1: a `BEFORE UPDATE` trigger "rejects the write regardless of what issued it". `Application.resume_version` is `PROTECT`. So the row cannot be updated, cannot be deleted, and its `content` JSONB holds the candidate's full name, phone, address and employment history. **The system as specified cannot satisfy its own FR-1.8.** This is the single most serious finding in the review, and it was invisible in prose.

**HIGH-2 — resume purge collides with `PROTECT`.**
`ResumeDocument` soft-deletes and a purge job hard-deletes. Any version referenced by an application raises `ProtectedError`. The purge is specified nowhere as skipping them.

**HIGH-3 — FX provenance is lost.**
Exit condition demands it. §5.3 computes `salary_min_base` "from the rate current at that moment" and stores no reference to *which* rate. A year later nobody can explain why a job matched a salary band, and re-running the matcher on historical data is impossible.

**MEDIUM-1 — `AttachmentAccess` is a top-three table by volume** (every CV view, every profile view in talent search) and is unpartitioned with no rollup.

**MEDIUM-2 — `Application.answers` JSONB has no schema version**, unlike every other JSONB document. Screening questions will change shape.

### Redesign
Add `ResumeVersion.redacted_at` and `redaction_reason`; the trigger permits exactly one transition — non-null `redacted_at` with `content` replaced by a tombstone — and nothing else. Purge skips referenced versions and redacts instead. Add `salary_fx_rate_id` FK and `salary_base_as_of` to every salary-bearing model. Add `answers_schema_version`.

### Scaling / failure / security / migration / test
At 1M candidates the largest tables are `AttachmentAccess`, `AnalyticsEvent`, `AuditLog`, `AIRun`, `JobView` — all append-only, all partition-convertible while small (§17). Failure mode: a slow Postgres degrades reads first; connection timeout is 5s so a wedged database fails fast rather than hanging workers. Security: erasure correctness is a legal exposure, hence CRITICAL. Migration: all six changes are additive columns plus one trigger revision — expand/contract, no rewrite. Tests: an erasure test that asserts `content` is a tombstone *and* the application row survives *and* the trigger still rejects an ordinary update.

---

## 7. Resume architecture review

### What holds
The draft/snapshot/version split is correct and now proven against the real autosave state the frontend already ships. "Autosave does not create versions" is satisfied. Applications reference immutable versions with a render hash. History is preserved.

### Findings

**HIGH-1 — analysis creates unbounded versions.**
§5.5.1 lists "requests an analysis" as a version-creating act and defends it: "the input-hash cache makes a re-analysis of unchanged content free." That defends the *AI cost* and says nothing about the *row*. A candidate who clicks Analyse ten times on unchanged content gets ten identical versions. The version list — the feature the split exists to protect — fills with noise by a different route than autosave.

**MEDIUM-3 — embedding cost is keyed to the wrong event.**
§13.3 subscribes re-embedding to `resume.version_created`. With version-per-analysis, every analysis triggers an embedding. Embeddings should follow *profile and skill* changes, which is what the matcher actually reads.

**MEDIUM-4 — draft concurrency policy is undefined.**
`ResumeDraft` carries `row_version` (optimistic locking) and autosaves continuously. Two tabs on one draft either collide on every keystroke or the column is decorative. Neither is specified.

### Redesign
Finalize on analysis **only if the draft's content hash differs from `current_version.sha256`**; otherwise re-use the existing version. This preserves "a score is always attributable to an exact document" at zero row cost. Re-key embeddings to `profile.updated` and `skills.changed`. For drafts: last-write-wins per tab with a `client_session` token, `row_version` reserved for structural operations (reorder, section add/remove) where a genuine conflict matters — and say so.

### Test
Analyse twice without editing → one version. Analyse, edit, analyse → two. Two tabs autosaving → no user-visible error; two tabs reordering sections → one gets `StaleWrite`.

---

## 8. Application architecture review

### What holds
`stage` as sole state (A23) is a genuine improvement — verified against the "job changes after applications exist" and "illegal transition" scenarios. `job_snapshot` answers what the candidate applied to. `match_snapshot` answers why it ranked. `ApplicationEvent` is one stream feeding two filtered timelines.

### Findings

**MEDIUM-5 — 100k applications to one job produces 100k recruiter notifications.**
`dedupe_key` collapses a burst into one notification, but no policy defines the key or the digest for high-volume single-job traffic.

**MEDIUM-6 — re-application cooldown is a `Job` column with no enforcement point named.** Service-level is fine; unnamed is not.

**LOW-1 — the partial unique index permits immediate re-application after closure**, which is correct, but interacts with the cooldown rule with no stated precedence.

### Redesign
Define `dedupe_key = f"job:{job_id}:new_applicants"` with a rolling count and a per-recruiter hourly digest above a threshold. Name `applications.services.apply` as the cooldown enforcement point, checked before the uniqueness constraint so the user gets a message rather than an `IntegrityError`.

---

## 9. Event and outbox review

### What holds
This is the strongest subsystem. Rollback-safety is enforced in code, not documented. Consumer idempotency is a unique constraint. Per-consumer savepoints prevent one failure poisoning a batch. Retry only re-runs failed consumers. Dead-lettering is visible, not a bin. Effects are correctly excluded from the bus. All of it is tested, including with a real commit boundary.

### Findings

**MEDIUM-7 — the drain holds row locks across consumer execution for the whole batch.**
`drain()` opens one transaction, selects up to 100 rows `FOR UPDATE SKIP LOCKED`, and runs every consumer inside it. A slow consumer holds 100 rows and one connection. `SKIP LOCKED` means other workers proceed, so this degrades throughput rather than correctness — but at 50k fan-out it is the wrong shape.

**MEDIUM-8 — webhook payloads are not specified to carry `event_id`.**
Consumer-side idempotency is impossible without it. "Webhook delivered five times" is only safe if the receiver can dedupe.

**LOW-2 — `OutboxConsumerRun` and the planned `NotificationDelivery` are two ledgers for one concept.** See §24.

### Redesign
Reduce the batch transaction to *claim-then-release*: select and mark in one short transaction, run consumers outside it, record outcomes in a second. Add `event_id` and `event_type` to every webhook payload and to the HMAC-signed body.

---

## 10. Celery and Redis review

### Findings

**CRITICAL-2 — one Redis keyspace serves cache, sessions, broker and security tokens.**
Confirmed in `config/settings/base.py`: `REDIS_URL` (db 0) is `CACHES` LOCATION, `SESSION_ENGINE` is `cache`, and `CELERY_BROKER_URL` defaults to the same URL. Three consequences, in descending severity:

1. **Single-use file tokens can be evicted.** `apps/files/signing.py` enforces single-use with `cache.add`. Under memory pressure with an LRU policy, that key is evictable — and an evicted key means a **replayable download token** for a private resume. A security guarantee is resting on a cache with eviction semantics.
2. **`cache.clear()` logs out every user.** Invalidating feature flags is a routine operation; sharing a keyspace with sessions makes it an outage.
3. **Broker backlog competes with cache and sessions for one memory budget.** A 50k fan-out can evict sessions.

**MEDIUM-9 — six queues at MVP is more isolation than the workload justifies.** `match` has no consumer until P13.

**Verified good:** tasks take primitive IDs, never model instances; `acks_late` with `reject_on_worker_lost`; prefetch 1; explicit queues; no large payloads through Redis.

### Redesign (A35)
Separate logical databases: `redis://.../0` broker, `/1` cache, `/2` sessions — with distinct eviction policies (`noeviction` for the broker, `allkeys-lru` for cache only, `noeviction` for sessions). **Move single-use token consumption to Postgres**, or at minimum to the `noeviction` database: a security control must not depend on a cache. Merge `match` into `ai` until P13.

### Failure behaviour after redesign
Redis down: sessions lost (users re-authenticate), cache misses fall through to Postgres, Celery stops accepting work and the outbox backs up safely on disk. **No data loss, no security degradation.** Before the redesign, Redis pressure silently weakens file security — which is why this is CRITICAL rather than HIGH.

---

## 11. AI architecture review

### What holds
Provider abstraction at task level; `AIRun` gives caching, cost attribution, reproducibility and an eval corpus from one table; retry classification distinguishes retryable from terminal; quota is checked before dispatch; kill switch is a runtime flag (correctly the one exception to config-in-code); prompt injection is addressed with delimited data and schema validation; the model never decides.

### Findings

**HIGH-5 — the cost loop has one open end.** MEDIUM-3 above: embeddings keyed to version creation, combined with version-per-analysis, multiplies embedding spend by analysis frequency. That is the "uncontrolled cost loop" the brief warns about, and it exists today in the specification.

**MEDIUM-10 — no per-company AI budget.** Per-user quota exists; employer-side features (ranking explanations, candidate comparison) are billed to a company that has no ceiling.

**MEDIUM-11 — the eval harness is a P11 deliverable, but `AIFeedback` collection starts at P12.** The corpus arrives after the gate that needs it.

### AI quality loop
Applying §12's discipline honestly, iterative critic loops earn their latency and cost in exactly two places:

| Feature | Loop | Justification |
|---|---|---|
| Resume rewrite | generate → deterministic ATS validation → critic → revise → final validation | Measurable: ATS check pass rate is objective |
| Interview evaluation | generate → rubric-schema validation → self-consistency on score only | Employer-facing score; disputes are real |
| Resume analysis | generate → schema validation. **No critic** | Deterministic checks already carry the objective half |
| Cover letter, summary, bullet rewrite | generate → schema validation | User reviews before accepting; a critic duplicates the human |
| Match explanation, notifications | generate → schema validation | Explains a deterministic result; nothing to critique |

The distinction that matters: **AI generation, AI evaluation and AI refinement are separate task keys with separate prompts and separate cost lines**, never one blended call.

---

## 12. Search and matching review

### What holds
Selectivity-branched retrieval (A24) is correct and was the right correction. Deterministic scoring with a stored breakdown answers both "why was I recommended this" and "why was this candidate ranked" with the same object. Score versioning, embedding-model versioning, cold start, diversity and exposure caps are all defined. The LLM is not required for any ranking operation.

### Findings

**HIGH-4 — cache invalidation is incomplete.**
The key is `(profile_id, algorithm_version, profile_updated_at)`. A newly published job cannot appear in any cached feed until the candidate edits their profile or the TTL expires. On a platform whose core promise is "see the roles you match", the feed is stale exactly when it matters most — the day a relevant job is posted.

**MEDIUM-12 — no defined behaviour for an employer disputing a ranking.** The data exists (breakdown snapshot); the process does not.

### Redesign
Add a **job-corpus generation counter** — a monotonic integer in the durable cache, bumped on `job.published` / `job.updated` / `job.closed` — into the cache key. New job, new generation, every feed recomputes on next read. One integer, and the staleness class disappears.

### Migration to OpenSearch
Postgres remains the source of truth; the index is a rebuildable projection; `matching.retrieve()` is the only seam that changes. Unchanged and still correct.

---

## 13. Interview architecture review

### What holds
One session model, two engines in v1, converging on one outcome shape. `InterviewSlot` with a GiST exclusion constraint makes double-booking impossible at the database rather than in a check-then-insert race — `btree_gist` is already enabled by P0's extension migration, so the mechanism is ready. Question pooling and rotation, time-boxing, integrity metadata recorded but never scored, consent per scope in the unified table, video deferred with `is_scoring_input` false by default.

### Findings

**MEDIUM-13 — interviewer access expiry has no enforcement mechanism named.** "Scheduling until 30 days after" is a rule with no job, no column and no query specified.

**LOW-3 — timezone handling for recurring availability** (`InterviewerAvailability.recurrence` JSONB) is undefined across DST boundaries.

### Redesign
Access is a computed predicate in the applications queryset — `interview_participant AND session.ended_at > now() - 30 days` — not a stored grant that needs expiring. Nothing to run, nothing to forget. Store availability recurrence in the interviewer's IANA zone with explicit DST policy.

---

## 14. Permission and tenancy review

### What holds
Three axes; URL tenancy; three enforcement layers with queryset scoping as the actual boundary; validated grant keys rather than free-form JSON; role×capability matrix generated from code; 404-not-403 for scoped misses; last-owner protection; invite email binding; suspension cascade; immediate revocation.

Multi-company users, cross-company leakage and privilege escalation were all attacked in §23 and held.

### Findings

**MEDIUM-14 — `request.company` is set by middleware, but nothing forces a *service* to receive it.** The queryset manager takes `request`; a service called from a Celery task has no request and therefore no tenant scope. The pattern is defined for the web path only.

**LOW-4 — staff impersonation is defined but its 60-minute expiry has no enforcement point.**

### Redesign
Services take an explicit `actor` and, where tenant-scoped, an explicit `company` — never an implicit request. `for_actor(request)` becomes a thin portal-side wrapper over `for_actor(user=..., company=...)`, so the background path has the same boundary as the web path. This matters more than it looks: every P1+ scheduled job touching employer data would otherwise be unscoped by construction.

---

## 15. File storage review

### What holds
Three visibility classes; content-addressed keys; magic-byte detection that opens the DOCX container; scan before any processing; unscannable treated as unsafe; quarantine emits an event and audits; `Content-Disposition: attachment` always; private backend with no URL at all (the `MEDIA_URL` fallback trap, found and closed in P0); a deploy check that probes the backend rather than reading its options; access logged before bytes are served.

### Findings
CRITICAL-2 (token durability) is a file-security finding as much as a Redis one — see §10.

**MEDIUM-15 — the object-store path logs access then redirects.** The log records intent, not delivery. Acceptable, but the difference should be recorded in the row (`served` vs `redirected`), or "who viewed your CV" will overcount.

**LOW-5 — no per-user upload quota**, only per-file size. A verified account can fill the bucket.

---

## 16. Security review

Baseline is defined and largely implemented: Argon2, hashed single-use tokens, 404-not-403, magic-byte validation, scanning, SSRF allow-listing, secret scrubbing in audit metadata, append-only audit, request-ID length capping.

### Findings

**CRITICAL-3 — dependencies are not reproducible.**
`requirements.txt` uses unbounded `>=` for six of seven runtime dependencies and there is **no lockfile and no hashes**. Two builds a week apart install different code. This fails the stated exit condition outright, and for a platform handling resumes it is also the supply-chain attack surface: `pip-audit` checks what is installed today, not what installs tomorrow.

**HIGH-6 — rate limiting exists only as prose.** SRS §15.4 lists the endpoints; no mechanism, no dependency, no code. Credential stuffing, upload flooding and AI-cost abuse are all currently unmitigated. This is an exit-condition failure disguised as a documented control.

**HIGH-7 — CSP is not implemented at all.** A31 chose the policy (nonce + `unsafe-eval`) and scheduled the tightening for P18, but no middleware exists, so today there is *no* CSP — not even the weak one. The ADR describes a policy the application does not send.

**MEDIUM-16 — no anomaly detection** on bulk export or high-volume profile viewing, which is what a scraper with a paid employer account looks like.

### Redesign
Lock dependencies with hashes (A36). Choose the rate-limit mechanism now — Redis fixed-window on the `noeviction` database, expressed as a decorator plus service-level domain limits — and land it in P1 with auth. Land CSP middleware in P1 rather than P18; the nonce plumbing is trivial and the current state is worse than the ADR implies.

---

## 17. Performance review

### Findings and the partitioning ruling
The P0 report left partitioning open. Resolved here, with reasoning rather than deferral-by-default.

Native Postgres partitioning in Django needs raw DDL, complicates primary keys and restricts inbound foreign keys. The benefit is proportional to table size. At MVP through roughly 100k candidates, `AuditLog` and `AttachmentAccess` stay in the low millions of rows — well inside a well-indexed unpartitioned table.

**Ruling: defer to P16, keep the tables partition-ready.** The conversion (`CREATE TABLE … PARTITION BY RANGE`, copy, swap) is a minutes-long maintenance window while small and an impossible one while large — so the trigger is a *size threshold, not a phase*: **convert when any of the five tables passes 20M rows**, monitored from P16's dashboards. The requirement is that they stay convertible: no inbound FKs, `created_at` in every index. P0 satisfies both.

**Other findings:** the employer applicant list will N+1 on notes, tags and ratings without stated prefetch (MEDIUM-17). No `assertNumQueries` budgets exist yet for pages that do not exist (fair). PDF rendering is isolated to its own queue and cannot move the web p95.

---

## 18. Observability review

### Findings

**HIGH-8 — the observability exit conditions are not met, and NFR-10 overstates the code.**
Exit conditions require request, actor, company, task, event and AI-run IDs. The code carries **request ID only**, and `apps/common/logging.py` binds it via a ContextVar that **does not cross into Celery** — verified: no task propagates it. NFR-10 claims "a request ID propagated into Celery tasks". It is not.

### Redesign (A37)
One `LogContext` envelope — request, actor, company, task, event, AI run — set by middleware, attached to every task's headers at `apply_async`, restored by a task prerun hook, and emitted by the JSON formatter. Without it, "failure tracing is possible" is false the moment work crosses into a worker, which is where the interesting failures live.

---

## 19. Dependency review

Seven runtime dependencies, five dev. Each was re-justified against Django and the standard library.

| Dependency | Verdict |
|---|---|
| Django, whitenoise, widget-tweaks | Existing, justified |
| `psycopg[binary]` | Required by A2 |
| `celery`, `redis` | Required; no credible stdlib alternative for durable queues |
| `argon2-cffi` | Django ships the hasher, not the implementation |
| `ruff`, `mypy`, `django-stubs`, `coverage`, `pip-audit` | Dev only; django-stubs earned its place by finding four real defects |

**Correctly avoided:** python-magic/filetype, django-environ, pytest stack, factory_boy, django-storages, LangChain. Each would be larger than the code it replaced.

**Still needed and not yet chosen:** a rate-limiting mechanism (prefer hand-rolled Redis fixed-window over `django-ratelimit` — roughly 60 lines, no dependency, and the domain limits are custom anyway), CSP (hand-rolled nonce middleware, ~40 lines, avoids `django-csp`), and `pip-tools` or `uv` for locking (dev-only, unavoidable).

**CRITICAL-3** applies here: version ranges are uncontrolled and upgrades are not reproducible.

---

## 20. Django structure review

Domain-owns-models / portal-owns-screens is correct and P0 demonstrated the incremental path works. No SPA migration; Django remains the runtime; HTMX handles server interactions; Alpine handles local UI state; no business logic in JavaScript; Tailwind stays build-time.

**LOW-6 — `apps/common` and `apps/core` overlap.** Both are platform-shared. The distinction (core is frontend-facing: flags, navigation, templatetags; common is infrastructure) is real but undocumented, and will erode.

---

## 21. Product review

Walking the workflows from the user's side, against §13's questions:

**Good:** upload once and it works in five modules; accept/reject per field rather than silent overwrite; honest save states; named progress stages rather than spinners; every automated result carries its reasons; the analysis explains what to change and why.

**Findings:**
- **PRODUCT-1** — the candidate cannot see *why* a recommendation changed after they edited their CV. The breakdown is per-result, not per-delta. A "what changed" diff between two match runs is cheap given stored breakdowns and is the single strongest trust feature available.
- **PRODUCT-2** — nothing lets a candidate recover a deleted resume document within the soft-delete window. The data supports undo; no workflow exposes it.
- **PRODUCT-3** — applying still requires choosing a resume version every time. Default to primary, remember the last choice per company, and the click disappears.
- **PRODUCT-4** — employers get no explanation of *why* the platform ranked candidate A above B beyond the breakdown; a comparison view over stored breakdowns needs no AI.

---

## 22. Failure simulation results

| # | Scenario | Expected | Containment | Consistency | Recovery | User sees | Audit |
|---|---|---|---|---|---|---|---|
| 1 | Postgres down | Total outage | `/readyz` 503, LB drains | Intact (no writes) | Reconnect; 5s connect timeout prevents worker pile-up | Maintenance page | None possible |
| 2 | Redis down | **After A35:** re-auth, cache misses, queue stalls | Web serves reads; outbox accumulates on disk | Intact | Restart; workers resume | Logged out once | Normal |
| 3 | Celery down | Async work queues | Outbox grows; alert on oldest unprocessed row | Intact — nothing is lost | Start workers; backlog drains | "Still processing" | Normal |
| 4 | AI provider down | Task retries, then fallback chain | `ai` queue only | `AIRun` = `provider_error` | Automatic retry | Named failure + retry action | `AIRun` row |
| 5 | Malformed AI JSON | One repair attempt, then terminal | Schema validation at the boundary | `schema_error`; nothing persisted downstream | Prompt fix + replay | "Could not complete" | `AIRun` + raw output |
| 6 | Outbox worker crashes mid-batch | Transaction rolls back; rows unlocked | `SKIP LOCKED` lets peers continue | **No event lost, none double-effected** | Next poll | Nothing | Ledger |
| 7 | Task executes twice | Second run is a no-op | Idempotent by ID; ledger unique constraint | Intact | None needed | Nothing | Ledger |
| 8 | Webhook delivered 5× | Receiver dedupes on `event_id` | Backoff, dead-letter at 24h | Ours intact | Replay from staff UI | Nothing | `WebhookDelivery` |
| 9 | 50k applications to one job | Chunked fan-out, digested notifications | Chunk workers; ledger prevents double-send | Counters async | None | Digest, not 50k alerts | Events |
| 10 | 100k notifications | Two-stage fan-out, 500-row chunks | Independent retry per chunk | Exactly-once visible effect | Resume from cursor | Normal | Ledger |
| 11 | Candidate deletes account | **Currently impossible — CRITICAL-1.** After fix: profile purged, versions redacted, applications tombstoned | Erasure job | Employer record survives without PII | 14-day grace | Confirmation + honest limits | Audited |
| 12 | Recruiter in two companies | Both memberships active; URL decides context | Structurally impossible to cross | Intact | None | Company switcher | Per-company |
| 13 | Resume edited after applying | Application still points at the frozen version + render hash | Immutability trigger | Employer's copy unchanged | None | "Applied with v3" | Version chain |
| 14 | Embedding model replaced | Retrieval filters on current model; dual-write, backfill, swap | `embedding_model` column | No mixed-model comparisons | Documented migration | Nothing | Migration log |
| 15 | Salary currency changes | **After HIGH-3 fix:** historical rows keep their rate reference | FX row is immutable | Reproducible | Recompute with stored rate | Original figures unchanged | FX provenance |
| 16 | Malicious PDF | Rejected at magic-byte check, or quarantined post-scan | Never reaches a parser | Row survives for the message | Uploader told | `attachment.quarantined` + event |
| 17 | Cross-company access attempt | 404 | Queryset scoping; CI-enforced | Intact | None | "Does not exist" | Audited |

**Scenarios 11 and 15 fail today.** Both are fixed by CRITICAL-1 and HIGH-3.

---

## 23. Security attack results

| Attack | Result | Basis |
|---|---|---|
| IDOR on files | **Held** | UUIDv7 public IDs, signed token, policy re-check, 404 on miss |
| IDOR on applications/jobs | **Held by design** | Scoped querysets; CI check bans unscoped access in portals |
| Cross-tenant access | **Held** | URL tenancy revalidated per request against active membership |
| Privilege escalation | **Held** | Grant keys validated against a fixed enum; last-owner protection |
| **Token replay on a private CV** | **VULNERABLE** | CRITICAL-2: single-use enforcement in an evictable cache |
| Malicious upload | **Held** | Magic bytes, container inspection, scan-before-process, unscannable = unsafe |
| Stored XSS via resume content | **Partially held** | Template auto-escaping holds; no CSP is actually sent (HIGH-7) |
| Reflected XSS | **Held** | Auto-escaping |
| CSRF | **Held** | Django protection on all command endpoints |
| SQL injection | **Held** | ORM only; no raw SQL in P0 |
| Session hijacking | **Held** | HttpOnly, SameSite=Lax, Secure in prod, rotation on privilege change |
| **Credential stuffing** | **VULNERABLE** | HIGH-6: no rate limiting exists |
| **Rate-limit bypass** | **N/A** | Nothing to bypass |
| Webhook spoofing | **Held** | HMAC over body + timestamp |
| Event replay | **Held** | Ledger unique constraint |
| AI/resume prompt injection | **Held** | Delimited data, schema validation, escaped and capped in employer-facing output |
| AI data exfiltration | **Held** | PII minimisation per task; no training on user data |
| Employer candidate leakage | **Held** | Grant-based visibility + staged contact revelation |
| **Supply-chain** | **VULNERABLE** | CRITICAL-3: unpinned, unhashed dependencies |

Three live vulnerabilities. One (token replay) is in code shipped in P0.

---

## 24. Simplification pass

Asked honestly of every component.

**Remove or merge:**
1. **Two delivery ledgers → one.** `OutboxConsumerRun` (P0) and the planned `NotificationDelivery` (P10) record the same fact at different granularity. Generalise the existing one to `(event, consumer, target_key)`; delete the second before it is built. Saves a table, a migration and a class of confusion.
2. **Six queues → four** until P13: `default`, `ai` (absorbs `match`), `render`, `email`. `files` work is short and belongs on `default`.
3. **`ConversationParticipant` → drop.** A conversation is exactly one company and one candidate. A join table for a fixed 2-party relationship is generality with no case behind it; `last_read_company_at` / `last_read_candidate_at` on `Conversation` is simpler and faster.
4. **`InterviewQuestionPool` → JSONB on `InterviewTemplate`.** Never queried across templates.
5. **`apps/common/types.py`** is two lines; fold into `errors.py` or leave, but do not grow it.

**Keep despite looking removable:** `JobAlertMatch` (genuine batch), `PromptTemplate` (audit identity), the outbox (its guarantee is the whole point), `AttachmentAccess` (a product feature and a security signal).

**Architecture shape:** modular monolith, confirmed. Nothing in this system justifies a service boundary — the AI provider is already isolated behind an adapter, which is the only place a network boundary would earn its cost.

**Net: five tables/queues removed or avoided, no functionality lost.**

---

## 25. Cost control pass

| Driver | Shape | Control | Status |
|---|---|---|---|
| AI inference | Async, cached by input hash, quota pre-checked | Per-user quota, per-run ceiling, model tier per task, kill switch | **Controlled** |
| **Embeddings** | Currently async per version | Re-key to profile/skill change | **UNCONTROLLED — MEDIUM-3/HIGH-5** |
| PDF rendering | Async, cached by (version, template, format) | Dedicated queue, concurrency cap | Controlled |
| Object storage | Content-addressed, deduped | Render expiry 90d, guest sweep 24h, purge on erasure | Controlled |
| Database | Append-only tables dominate growth | Partition at 20M rows; outbox deleted after 24h | Controlled with a named trigger |
| Redis | Cache + broker | Memory cap per role after A35 | Controlled after fix |
| Email | Async, batched | Suppression list, send caps, quiet hours, digests | Controlled |
| Video | Jitsi JaaS | Per-minute pricing; deferred to P15 | Deferred |
| Workers | Six → four queues | Concurrency caps per queue | Controlled |

**Synchronous:** nothing above ~200ms. **Asynchronous:** parse, analysis, render, embed, email, fan-out, match batch. **Cached:** AI runs, renders, recommendations, feature flags. **Batched:** fan-out chunks, job alerts, rollups. **Rate-limited:** auth, upload, AI, search, messaging, applications per day.

The one open end is embeddings, and it is closed by MEDIUM-3's fix.

---

## 26. Iteration history

### Iteration 1 — initial design
Re-derived every area from the SRS and read it against the code.
**Issues found: 21** (3 critical, 8 high, 10 medium/low).
Scores: Architecture 78 · Production readiness 62 · Scalability 74 · Security 70 · Maintainability 82 · AI readiness 76.

### Critique 1
The design assumed prose guarantees were mechanisms. Three assumptions broke on contact with code: immutability was enforced so hard it blocked erasure; a security control was resting on a cache; and observability was asserted rather than implemented. A fourth — dependency reproducibility — was never designed at all.

### Iteration 2 — corrected design
Applied: redaction-permitting trigger (CRITICAL-1); Redis role separation and durable token store (CRITICAL-2); lockfile with hashes (CRITICAL-3); analysis version dedupe by content hash (HIGH-1); FX provenance columns (HIGH-3); job-generation counter in the match key (HIGH-4); LogContext envelope (HIGH-8).
**Issues fixed: 14. New risks introduced: 2** — (a) a trigger that permits *any* update becomes a weaker guarantee; (b) moving token state to Postgres adds a write to a hot read path.
Scores: Architecture 86 · Production readiness 68 · Scalability 82 · Security 80 · Maintainability 86 · AI readiness 80.

### Critique 2
The redaction exception was too broad as first drafted — "allow updates when erasing" is an invitation. And the token write is on the download path, which is latency-sensitive.

### Iteration 3 — corrected design
Narrowed the trigger to exactly one legal transition: `redacted_at IS NULL → NOT NULL` with `content` replaced by a tombstone and every other column unchanged. Any other update still raises. For tokens: keep the cache as a fast path but on the `noeviction` Redis database, so eviction cannot occur — Postgres only if a durability audit later demands it. Resolved partitioning with a size trigger rather than a phase. Closed service-level tenancy for background jobs (MEDIUM-14).
**Issues fixed: 7. New risks: 0.**
Scores: Architecture 91 · Production readiness 71 · Scalability 88 · Security 87 · Maintainability 90 · AI readiness 86.

### Critique 3
Remaining findings are all HIGH or below, all unbuilt-work rather than misdesign, and none blocks a phase that is not already gated on them. No further iteration changes a decision — it would only re-list the same backlog. **Loop exits here.**

---

## 27. Critical issues

### CRITICAL-1 — Erasure is architecturally impossible

**Issue.** A candidate's right to erasure cannot be satisfied. `ResumeVersion.content` holds full name, phone, address and employment history; the row cannot be updated (SRS §5.5.1 `BEFORE UPDATE` trigger, "regardless of what issued it") and cannot be deleted (`Application.resume_version` is `PROTECT`).

**Evidence.** SRS §19.4 — "versions … purged from database and bucket", "the resume snapshot destroyed". SRS §5.5.1 — the trigger. DEVELOPER.md §2.2 — `PROTECT` for anything referenced by a surviving record. Three statements that cannot all be true.

**Impact.** FR-1.8 is unimplementable. Under a GDPR-shaped regime this is a compliance failure; under any regime it is a promise to users the system cannot keep. It would be discovered during P5 (versions) or P8 (applications) and would force a schema change *after* immutability had been depended upon.

**Recommended solution.** Redefine immutability as **no semantic change**, not no write. Add `ResumeVersion.redacted_at` and `redaction_reason`. The trigger permits exactly one transition — `redacted_at NULL → NOT NULL`, `content` replaced by a fixed tombstone, every other column byte-identical — and rejects everything else. Erasure redacts rather than deletes; the application keeps a valid FK, its `submitted_render_sha256` still proves what was sent, and the employer's legally required record survives without PII. Purge skips referenced versions and redacts them instead.

**Affected files.** `docs/architecture/CareerAI-SRS.md` §5.5.1/§5.5.2/§19.4; `docs/architecture/ADR.md` A8, A19; future `apps/resumes/models.py`, its trigger migration, and `apps/accounts/services/deletion.py`.

**Affected architecture.** Resume versioning, application integrity, erasure workflow, audit.

**Migration cost.** **Near zero now** — two columns and one trigger definition, written before either exists. **Severe later** — after P5 and P8 ship, it is a trigger replacement plus a backfill on the table the entire evidentiary guarantee rests on.

**Priority.** Fix before P5. Amend the SRS before P1, so the trigger is written correctly the first time.

---

### CRITICAL-2 — A security control depends on an evictable cache

**Issue.** Single-use enforcement for private file download tokens is implemented with `cache.add` against a Redis instance that also serves sessions and the Celery broker, on one keyspace with one memory budget. Under LRU eviction the "used" marker disappears and **the token becomes replayable** — a private resume downloadable more than once by anyone holding the URL.

**Evidence.** `apps/files/signing.py:68` — `cache.add(f"{_CACHE_PREFIX}{payload['jti']}", 1, timeout=ttl)`. `config/settings/base.py:144-158` — `REDIS_URL` is simultaneously `CACHES` LOCATION, `SESSION_ENGINE = cache`, and `CELERY_BROKER_URL`. Verified: one URL, database 0, three roles.

**Impact.** Three, in descending severity. (a) Token replay on private CVs under memory pressure — a real confidentiality failure with no log of the second read having been unauthorised. (b) `cache.clear()`, a routine feature-flag operation, logs out every user. (c) A large fan-out backlog can evict sessions and cache entries.

**Recommended solution (A35).** Separate logical databases by role — `/0` broker, `/1` cache, `/2` sessions — with distinct eviction policies: `noeviction` for broker and sessions, `allkeys-lru` for cache only. Move the single-use token marker to the `noeviction` database (or Postgres if a later durability audit requires it). Set an explicit `maxmemory` per role.

**Affected files.** `config/settings/base.py`, `config/settings/dev.py`, `docker-compose.yml`, `apps/files/signing.py`, `.env.example`, `docs/architecture/CareerAI-SRS.md` §17.1.

**Affected architecture.** Files, sessions, cache, queue, security baseline.

**Migration cost.** Trivial — configuration plus a one-line change of cache alias in `signing.py`.

**Priority.** **Fix now.** This is live in shipped P0 code.

---

### CRITICAL-3 — Dependencies are not reproducible

**Issue.** Six of seven runtime dependencies use unbounded `>=`, and there is no lockfile and no hashes. Two builds a week apart install different code; a compromised release is installed automatically.

**Evidence.** `requirements.txt` — `whitenoise>=6.6`, `psycopg[binary]>=3.2`, `celery>=5.4`, `redis>=5.0`, `argon2-cffi>=23.1` (only Django is bounded). No `requirements.lock`, no `constraints.txt`. `package-lock.json` exists for JavaScript — the Python side is strictly worse than the frontend it supports.

**Impact.** Fails the stated exit condition ("dependency upgrades are reproducible", "version ranges are controlled"). Operationally: CI green today and red tomorrow with no commit between. Security: `pip-audit` validates today's resolution, not tomorrow's install, so the audit gate provides less assurance than it appears to.

**Recommended solution (A36).** Introduce `requirements.in` / `requirements-dev.in` as the human-edited intent, compile to fully pinned `requirements.txt` / `requirements-dev.txt` with `--generate-hashes` via `pip-tools` or `uv`. Install with `--require-hashes` in CI and in the production image. Upgrades become a reviewable diff.

**Affected files.** `requirements*.txt`, new `requirements*.in`, `.github/workflows/ci.yml`, README, `docs/DEVELOPER.md` §9.

**Affected architecture.** Build, CI, supply chain.

**Migration cost.** One afternoon. It only grows.

**Priority.** Fix before P1.

---

## 28. Recommended improvements (HIGH)

| ID | Issue | Fix | When |
|---|---|---|---|
| **HIGH-1** | Analysis creates a version even when content is unchanged — version spam by a route the draft/version split was built to prevent | Finalize only when the draft hash differs from `current_version.sha256`; otherwise reuse | Before P5 |
| **HIGH-2** | Resume purge collides with `PROTECT` on referenced versions | Purge skips referenced versions and redacts them (folds into CRITICAL-1) | Before P5 |
| **HIGH-3** | FX provenance lost; historical salary matching not reproducible | `salary_fx_rate_id` FK + `salary_base_as_of` on every salary-bearing model | Before P2 |
| **HIGH-4** | Match cache never invalidates on new jobs — stale feed exactly when a relevant job is posted | Add a job-corpus generation counter to the cache key | Before P13 |
| **HIGH-5** | Embedding cost keyed to version creation — an uncontrolled cost loop | Re-key embeddings to `profile.updated` / `skills.changed` | Before P13 |
| **HIGH-6** | Rate limiting is prose only; credential stuffing and AI-cost abuse unmitigated | Redis fixed-window decorator on the `noeviction` database + domain limits; ~60 lines, no dependency | P1 |
| **HIGH-7** | No CSP is sent at all; A31 describes a policy the app does not implement | Nonce middleware in P1; `unsafe-eval` removal stays P18 | P1 |
| **HIGH-8** | Observability exit conditions unmet; NFR-10 overstates the code | `LogContext` envelope propagated into Celery (A37) | P1 |

## 29. Medium and low priority

**MEDIUM** — `Application.answers` needs a schema version (M-2) · `AttachmentAccess` volume and rollup (M-1) · draft concurrency policy undefined (M-4) · high-volume application notification digest (M-5) · cooldown enforcement point unnamed (M-6) · outbox drain holds locks across consumer execution (M-7) · webhook payload must carry `event_id` (M-8) · queue count exceeds workload (M-9) · no per-company AI budget (M-10) · eval corpus arrives after the gate that needs it (M-11) · employer ranking-dispute process undefined (M-12) · interviewer access expiry unenforced (M-13) · **service-level tenancy for background jobs (M-14 — the most consequential of these; fix in P1 with the rest of the permission work)** · access log overcounts on the redirect path (M-15) · no anomaly detection (M-16) · applicant list N+1 risk (M-17).

**LOW** — cooldown/re-application precedence (L-1) · two ledgers for one concept (L-2, see §24) · DST in recurring availability (L-3) · impersonation expiry unenforced (L-4) · no per-user upload quota (L-5) · `common`/`core` boundary undocumented (L-6).

**Product** — match-delta explanation (P-1) · resume undo within the soft-delete window (P-2) · remember resume choice per company (P-3) · candidate comparison from stored breakdowns, no AI needed (P-4).

## 30. Final architecture scores

| Dimension | Score | Justification |
|---|---:|---|
| **Architecture** | **91** | The nine irreversible decisions all survived adversarial review. No conflicting source of truth. Deductions: one impossible workflow (erasure) and one incomplete invalidation path, both in the design rather than the code |
| **Production readiness** | **71** | P0 is genuinely production-quality — 142 tests, 96% on gated modules, real security controls with real enforcement. Held down by three live gaps: no rate limiting, no CSP, unreproducible builds. CI has also never executed on GitHub |
| **Scalability** | **88** | Stateless web tier, two-stage fan-out, queue isolation, selectivity-branched retrieval, a named partitioning trigger. Deductions: embedding cost loop, match staleness, outbox lock shape |
| **Security** | **87** | Strong: tenancy, file serving, audit, injection handling, scanning, three enforcement layers. **Three live vulnerabilities** — token replay, no rate limiting, supply chain. Would be low 90s with those closed |
| **Maintainability** | **90** | Layer contract is real and observed in P0. Signals banned and none present. Incremental restructure proven. Deduction: doc/code drift already appeared in NFR-10 within one phase |
| **AI readiness** | **86** | `AIRun`, task-level abstraction, prompt versioning, retry classification, quota, kill switch, and a defensible position on where iterative loops earn their cost. Deductions: embedding cost loop, no per-company budget, eval corpus sequencing |

No score above 90 is claimed without the P0 test suite as evidence behind it.

---

## 31. FINAL ARCHITECTURE VERDICT

The requested verdict assumes P0 has not started. It has — P0 is built, tested and verified. So the honest answer has two halves.

### On P0, retrospectively: **APPROVE**

Building P0 was the right call and its output stands. Nothing in this review invalidates a P0 deliverable, and P0 is what made three of these findings visible at all. It carries **one live defect** — CRITICAL-2, the evictable single-use token — which is a configuration fix plus one line, not a redesign.

### On P1, prospectively: **DO NOT START P1**

Four things must land first. All are small; none is a redesign.

1. **CRITICAL-2** — Redis role separation and a non-evictable token store. *Live security defect in shipped code.*
2. **CRITICAL-3** — lockfile with hashes.
3. **CRITICAL-1** — amend the SRS and ADR A8/A19 for redaction-permitting immutability. The code lands in P5, but the **decision** must be recorded before P1, or the trigger will be written wrong and depended upon.
4. **HIGH-6, HIGH-7, HIGH-8** — rate limiting, CSP middleware and the observability envelope. All three belong with P1's authentication work; adding them later means revisiting every endpoint P1 creates.

Against the exit conditions: database, tenancy, resume, applications, matching, salary, AI, events, Celery and scalability **pass**. **Security fails** on three counts (token durability, rate limiting, supply chain). **Observability fails** on propagated context. **Dependencies fail** on reproducibility. Those five are the gate.

Estimated cost to clear it: **two to three days.** After that the exit conditions are met and P1 can begin.

---

## Appendix — changes to the frozen documents

The freeze policy (SRS §0.2) permits amendment for a critical design flaw, documented with the failure it causes. Six qualify:

| Document | Change |
|---|---|
| SRS §5.5.1, §5.5.2, §19.4 | Redaction-permitting immutability; purge redacts referenced versions |
| SRS §5.3, §5.4, §5.7 | `salary_fx_rate_id`, `salary_base_as_of` |
| SRS §10.1 | Job-generation counter in the match cache key |
| SRS §13.3 | Embeddings keyed to profile/skill change, not version creation |
| SRS §17.1, NFR-10 | Redis role separation; observability envelope replacing the overstated claim |
| SRS §5.1.8 | Partitioning deferred to a 20M-row threshold rather than "day one" |
| ADR | A8 and A19 amended; **A35** Redis partitioning, **A36** dependency locking, **A37** observability envelope added |
| DEVELOPER.md §9 | Lockfile workflow; `.in` → compiled `.txt` with hashes |

Version after amendment: **SRS v1.2**, ADR A1–A37.
