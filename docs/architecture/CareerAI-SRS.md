# CareerAI — Software Requirements Specification & System Architecture

**Version:** 1.23 — **Architecture Freeze** (amended during P1-P19; owner decisions D-01..D-13)
**Date:** 5 September 2026
**Status:** P0–P19 implemented. Two P19 exit criteria — the rehearsed restore and the 10x load test — need infrastructure rather than code and are outstanding; see §21 and docs/DEPLOYMENT.md.
**Supersedes:** SRS v1.0 and v1.1. This document is the single source of truth for the architecture.

**v1.23 changes (owner decisions D-01..D-10, 2026-09-18).** Ten product decisions recorded in §0.4 and applied where they bite: `is_staff` never reads private files (§15.2); a rejected company may apply again after thirty days, with every decision kept (§5.6, §7.5, §19.2, §19.5); invitations never bind to another account (§2.6); a reviewer with a conflict of interest cannot claim or decide (§19.5); talent-search terms under three characters match skills exactly (§3.9, §10.6); narrowing visibility revokes public, pool and invitation grants (§2.7); domain proof is optional (§19.2); Pro starts at once, Pro → Free waits for the period end and an overdue invoice blocks upgrades (§5.14, §21); notification preferences stay account-level with a role-aware screen (§3.11); announcements are deferred (§3.12, §13.2, §21).

**v1.22 changes.** No architectural amendment: P19 built §15.3, §19.4 and §19.5 as written, and closed ADR-A31 by swapping Alpine's standard build for `@alpinejs/csp` — `script-src` is now `'self'` plus a per-request nonce, with **no `unsafe-eval`**. One clarification and six defects, five of them found by the launch audit rather than by a test.

The clarification: **§15.3's retention schedule is now one module** (`apps/common/retention.py`) with a scheduled job named against every rule, and a test that fails if a rule has no job — a promise on a privacy page that nothing enforces is the failure mode this replaces.

The defects, in the order they would have hurt:

1. **The rollups and the billing lifecycle were never scheduled.** P17 wrote four analytics tasks and P18 wrote five billing tasks; neither phase added them to `beat_schedule`. Dashboards read rollups nothing computed, and no subscription would ever have renewed, been chased, or ended. Both are scheduled now, and a test asserts every entry resolves to a real task and every retention rule is scheduled.
2. **The Content Security Policy blocked the site's own assets.** Google Fonts was blocked by `style-src 'self'`, so every page silently fell back to system fonts; every marketing photograph was blocked by `img-src 'self' data:`. Fonts and photographs are now self-hosted, which is what `apps/core/content.py` had said to do "before launch" since P0.
3. **The persona stub was still wired into every request.** `apps/core/demo.py` resolved a fictional person from `?as=` and `?state=` query parameters — writing to an anonymous visitor's session — and the candidate dashboard greeted signed-in users with its hard-coded headline. Deleted; the shell now renders from `request.user`.
4. **The account-closure screen overstated what it did.** It was headed "Close this account" and called the service that empties the *profile*. There are now two controls that each say what they do, and the account one runs the §19.4 pipeline.
5. **Job search ran one query per posting** for its skill chips. One `prefetch_related`; a query-budget test now fails if any list screen's query count grows with its rows.
6. **Three multi-line `{# … #}` template comments rendered as visible text**, because Django's `{# #}` is single-line only. Two were on live screens.

Also corrected: **ADR-A12 and §5.1.9 list `Tag` as soft-deleted and omit `CompanyCandidateNote`** — the reverse of what the models do. §5.1.9 now names the eight tables that actually carry `deleted_at`, and `apps/common/tasks.py` purges them.

**v1.21 changes.** Two amendments, one correction of record, and two defects found and fixed. The amendments are both recorded as ADRs: **A54 — a plan's limits are a versioned JSONB document, read by one service and enforced nowhere else**, because a boolean column per feature means a migration every time commercial terms move and commercial terms move more often than schemas should; and **A55 — payment providers sit behind a two-method interface, and settlement by bank transfer is a first-class provider rather than a gap**, which is what ADR-A15's "Nepal-first" actually requires of the code. Two rules follow and both are tested: **a plan limit is not a rate limit** — `apps/common/ratelimit.py` refuses abuse and returns 429, `entitlements.require` refuses unpaid use and returns 402, and conflating them would tell a free user to slow down when what they need is a plan; and **every account has a plan**, resolved to the default for its audience when there is no subscription, so no enforcement path contains an "unsubscribed" branch. The correction: **§5.14's `Subscription` is confirmed as a two-column owner with a `CHECK`**, not a generic key — a candidate and a company are the two owners this product has, and a polymorphic key would buy nothing but unindexable joins. The defects, both on the pricing page and both the same class as INV-6 read backwards — *advertised, but not enforced*: **the marketing page listed limits nothing in the code applied** (one analysis a month, Pro-only templates, Pro-only export), and **it advertised twelve CV templates, eight of them free, where six exist and four are free**. The page is now generated from the `Plan` rows that govern the product, so a limit can no longer be advertised without being enforced, or enforced without being advertised.

**v1.20 changes.** No architectural amendment. One clarification of substance, one correction of record, and one defect found and fixed. The clarification: **rollups are computed from the domain tables, not from `AnalyticsEvent`.** §16.1 says domain events are the primary *source*, which reads as "count the events" — but the event table is lossy by design (§5.15) and applications are not, so a funnel counted from events would drift from the funnel counted from applications and the second one is the true one. `AnalyticsEvent` answers questions nobody built a table for; the rollups answer the ones that must be right. The correction: **§16.3 refers to a response rate "already computed in the existing `hiring_summary()`"** — that was a function in the frontend mock this codebase replaced, and no such function exists; the measurement is computed in `CompanyDailyStat` and read from there by both the nudge and the steering snapshot, so there is one implementation rather than two. The defect: **a hire was being identified by stage *kind*, and `hired` and `declined` share the `terminal` kind** — a hire count filtered on kind would have counted every rejection as a hire, and filtering on a kind named `hired` (which does not exist) would have counted none. Every hire count now filters on the stage **key**, from one named constant.

**v1.19 changes.** No architectural amendment: P16 built §5.10 and §11 as written, including the exclusion constraint that makes double-booking impossible, sampled and rotated question pools, bounded follow-ups, and `integrity_meta` as employer-visible context that never reaches a scorer. Four clarifications the implementation forced, recorded in §5.10 and §11: **interview events map to the `application_updates` preference group** rather than a group of their own, because an interview is a step in an application and §13.1 requires every delivered event to map to a group somebody can toggle; **a scorecard is written and submitted in one act**, so `submitted_at` guards visibility rather than an editing workflow; **`InterviewOutcome.recommendation` is four positions rather than a score**, because a number invites averaging across interviewers and that is how a panel's disagreement disappears into a mean nobody argued for; and **the JaaS JWT is not minted in v1** — `join_url_for` returns the room URL and the operator configures JaaS to accept it, because a self-signed token the vendor rejects at the door would be worse than an honest absence. One defect found and fixed within the phase: **a candidate whose employer chose not to share feedback saw "your answers are being read" indefinitely**, because "scoring has not finished" and "they chose not to share" were the same branch.

**v1.18 changes.** One amendment, one correction of record, two defects. The amendment: **a profile view is its own record, not an `AttachmentAccess`.** §10.6 said an employer opening a candidate's profile writes an `AttachmentAccess` row; a profile is not an attachment, and a candidate with no CV file uploaded would have had no record written at all — which is the case where "who viewed me" matters most, because somebody found in a search is exactly somebody nobody has a file for yet. `ProfileView` answers "who read my profile"; `AttachmentAccess` continues to answer "who downloaded my CV"; the candidate sees both. The correction: **`CandidateProfile` gains `experience_months`**, a derived column maintained beside `completeness` — talent search filters and orders on months of work, and deriving it per query would have been a third implementation of arithmetic `apps.common.periods` already owns, so `matching.extract` now reads the same function rather than building its own list of periods. The defects, both fixed: **the `job_matching` flag was still `False` with the comment "P13, not built"**, so the feed P13 finished was unreachable in production for two phases — INV-6 inverted, the same class of defect P14 found on the job page; and **two new talent views caught `NotAllowed` inline**, rendering a 200 with an apology instead of a 403, which would have made a missing grant look like a bad query.

**v1.17 changes.** One correction of record that matters more than it looks, one amendment, two defects and one deferral. **The phase table now records the delivered order** — from v1.15 this document used "P14" to mean both cover letters (§21, the P12 changelog) and talent search (§20's table), which is a contradiction inside one document rather than a difference of opinion with one. Cover letters are P14 as delivered; everything from talent search onward moves up by one, and §20 explains why phase *names* are now the stable reference. The amendment: **INV-3's mechanism is stated as two shapes, not one.** A resume suggestion exists because a draft is already there and must not be overwritten; a cover letter *is* the new thing being made, so the letter itself is the proposal and nothing sends it. Both reach the same guarantee — no AI-written text becomes a candidate's submission without an explicit act — and §5.8 now says so, because a reader who knew only the `ResumeSuggestion` shape would conclude cover letters had no protection. The defects, both fixed: **the job page told candidates that applying "is not switched on yet"** from P8 until now, which is INV-6 inverted — hiding a feature that works; and **`ResumeSuggestion`'s AI attribution was patched on after the row was created**, leaving it briefly unattributed and guaranteeing that every future AI caller copied the patch, so `suggestions.offer` now takes `ai_run` and `finding` directly. Deferred: **streaming for the assistant builder** (§9.5) — the assistant now answers, in one response rather than token by token, and the ASGI exception §9.5 describes stays open for the phase that wants the polish.

**v1.16 changes.** One amendment, three corrections of record and three deferrals. The amendment: **ADR-A10's adapter protocol is split in two** — `Adapter` for completions and `EmbeddingAdapter` for vectors — because not every provider offers embeddings, and a required method that half the implementations raise `NotImplementedError` from is a protocol that has stopped describing anything. `runner.embed` routes through the same kill switch, budget, chain, classification and `AIRun` as `runner.run`; a provider that cannot embed is skipped at dispatch rather than producing a 404 dressed up as an outage. The corrections: **ADR-A21's own solution line named a three-part cache key** (`profile_id`, `algorithm_version`, `profile_updated_at`) while §10.1 named five — the ADR now agrees with §10.1, which is the source of truth, and `embedding_model` and `job_generation` are both in the key the implementation builds; **§10.2's domain component scores industry only**, because a candidate carries no job category and the two ways to invent one both make the feed a function of itself; and **`JobAlertMatch.score` remains search relevance**, not a match score, since alerts run against a saved search rather than a profile. Deferred with reasons in §21: `Skill.embedding` (no consumer designed), `CandidateProfile.search_vector` (P14's talent search is its only reader), and FR-4.6's market-demand view of missing skills (needs application-outcome data that arrives with P16).

**v1.15 changes.** One amendment of scope and three corrections of record. The amendment: **ADR-A6 now governs the resume score as well as the match score** — the CV score is deterministic, carries an `algorithm_version`, and the model writes prose and judgement findings that cannot move the number. Two consequences follow and both are tested: a report is reproducible, so score history (FR-4.8) compares like with like; and analysis **degrades to its deterministic parts** rather than to an error when AI is off, out of budget, refused or failing, which is what §9.3.6 asks of every AI feature. The corrections: §5.5.2 said `ResumeParse`'s `AIRun` link "arrives with P9" — P9 was messaging, P4's reader is deterministic, and no AI parse is planned while the deterministic reader suffices; §9.4's task keys (`resume.analyze`, `resume.rewrite_bullet`, `cover_letter.generate`) never matched the keys P11 shipped, and the table now records the implemented names; and `ResumeSuggestion` gains the `FK→AIRun` and `FK→ResumeFinding` this section always specified, deferred in P5 for want of the tables. Three P12 scope items are deferred with reasons in §21: FR-4.6 (missing skills as market demand) needs matching data and moves to P13; cover letters move to P14; the assistant builder's streaming endpoint needs an ASGI serving path §17 does not run.

**v1.14 changes.** No architectural amendment: P11 built §5.11 as written, including A11 (every AI output is an `AIRun`), A20 (prompts are files in version control) and A28 (provider routing is code). One addition of record three clarifications the implementation forced, and one defect found and fixed within the phase. The addition: **every AI entry point goes through `ai.services.runner.run`** — one function performing the kill switch, cache lookup, quota check, provider fallback, schema validation and record, because orchestration duplicated per feature is orchestration that drifts per feature, and the first feature to skip the quota check would not announce itself. Providers are adapters behind one Protocol; a feature never names one. The clarifications, recorded in §5.11: **`AIRun` monthly partitioning is deferred** until the table is large enough for the partition to earn its migration complexity — the indices the cache and the cost queries need exist now, and a partitioned table with four months of rows is a maintenance burden with no reader; **a cache hit is a new `AIRun` row with `cached_from` set**, not a returned old row, because attribution is per request and a shared row would credit one person's question to another; and **spend is recorded whatever the outcome**, since a failed provider call still cost money and a budget that counts only successes is not a budget. The defect: **a run refused by the kill switch was recorded as `provider_error`**, which would have fired the provider error-rate alert during a deliberate shutdown and hidden real provider failures beneath it; `disabled` is now its own status, terminal and not retryable. `CoverLetter.ai_run`, deferred from P8 for want of the table, remains deferred to P14 with the cover-letter feature that fills it.

**v1.13 changes.** One amendment (A53: a webhook signing secret is encrypted, not hashed — a key that is *used* on every outbound request cannot be stored as a digest, and a signature computed from one would be decoration) and one addition of record: **the event registry now carries `audience`, `title`, `body` and `dedupe`**, so §13.3's recipient table and §13.5's wording live beside the event that causes them rather than inside whichever consumer builds the row. `register()` refuses an audience with no title. One defect found in an earlier phase and fixed: **the daily application budget was consumed inside the transaction that could still refuse the application** (FR-6.13), so a rejected submission cost a slot; `apply_to_job` now validates, then consumes, then writes — the shape `messaging.send` already used.

**v1.12 changes.** One amendment (A52: an extra reader of a private file is a grant row, not a branch in `files.policies`) and two corrections of record. **ADR-A26's solution line named a `ConversationParticipant` table that §5.9 had already removed**; the ADR now agrees with the SRS, which is the source of truth. **Blocking a company now has one implementation** — `candidates.services.blocking` — because §5.6 and §12 each describe it and two implementations would eventually disagree about whether a block also withdraws profile access. It does. The event registry gains `preference_group`, so §13.1's rule that every delivered event maps to a preference group is enforced at registration rather than documented.

**v1.11 changes.** No architectural amendment: P8 built §5.8 as written, including A23 (stage is the only state), A34 (staged contact revelation) and INV-4 (`CandidateVisibilityGrant` authorises every employer read of a candidate). Three clarifications the implementation forced, recorded in §5.8 — `JobRecruiter`/`JobHiringManager` were already one table (P7); **reopening a closed application is an explicit move requiring `application.decide`**, the same grant closing needed; and **`CoverLetter.ai_run` is deferred to P11**, because a nullable foreign key to a table that does not exist is not something a migration can express. `Application.match_snapshot` ships empty and is filled by P13.

**v1.10 changes.** One amendment (A51: `search_vector` is a generated column, and P7 ships no `JobRevision` table) and one addition of record: **Company gains the public-profile columns §7.1 already specified** — `industry`, `location`, `about`, `founded`, `logo` — which P1 had not yet built and the company page P7 delivers needs. Two candidate surfaces named in the navigation are now behind their own flags rather than the `jobs` flag: matched roles (`job_matching`, P13) and applications (`applications`, P8). Neither existed; both had been reachable links into mock screens.

**v1.9 changes.** One amendment (A50: the rendering engines) and one correction of record: **FR-3.11, tagged PDFs, is not delivered by P6** and remains open at priority S. The renderer produces extractable text in reading order with document metadata and a language, which is what the chosen engine can honestly do; structure tags need a different engine and are scheduled with the accessibility work.

**v1.8 changes.** No architectural amendment: P5 built §5.5.1–§5.5.3 as written. Two clarifications the implementation forced, both recorded in §5.5.2 — the four version-creating acts are a `TextChoices` whose membership is the enforcement, and `ResumeSuggestion`'s target paths are a closed vocabulary rather than a general expression language.

**v1.7 changes.** One amendment, forced by P4 producing the platform's first extracted data: extraction proposes and never writes the profile (A49), with `ResumeImportProposal` as the table that makes it structural. §5.5.2 gains the ingestion columns P4 actually built.

**v1.6 changes.** Three foundational taxonomy additions, requested before P4 because they become expensive once parsing, extraction and matching start writing to the taxonomy: the unresolved-term ledger (A46), provenance with reversible batches on every alias and relation (A47), and `Skill.kind` (A48).

**v1.5 changes.** One amendment, forced by DEVELOPER.md §6.11 meeting its first encrypted column: field-level encryption is a shared primitive with key rotation and a deploy check (A45). §5.4's model list is updated to match what P3 built — the candidate profile, its sections and `CandidateSkill` — and `UserPreferences` / `NotificationPreference` are recorded as the tables the settings screens write.

**v1.4 changes.** Two amendments, both extensions of A13 rather than reversals of it, and both requested with P2's scope: the skill lifecycle — deprecation and merge as distinct, non-destructive facts (A43) — and the pluggable importer interface with a resumable two-phase runner (A44). §5.3's model list is updated to match what P2 built, including `SkillTranslation`, `SkillExternalId` and `ImportRun`.

**v1.3 changes.** MFA is provided by `django-otp` rather than an in-tree TOTP implementation (A42).

**v1.2 changes.** Seven amendments, each forced by a defect found either by the architecture quality loop or by P0's own tests: privacy redaction of immutable resume versions (A38), Redis role separation with security state moved to PostgreSQL (A35), reproducible dependency locking (A36), the observability envelope (A37), FX provenance (A39), the partitioning threshold (A40), and the rate-limiting baseline (A41). CSP (A31) is now implemented rather than described.

---

## 0. Document control

### 0.1 What this document is

The complete backend architecture for CareerAI: an AI-powered recruitment platform and applicant tracking system. The frontend (Django templates, Tailwind v4, Alpine, HTMX) is built and is treated here as a **fixed contract the backend must satisfy** — the shapes in `apps/*/samples.py` and the function names in `apps/*/state.py` are requirements, not suggestions (Appendix A).

### 0.2 Freeze policy

From approval of this document, architectural change requires one of:

1. A production bug that the current design cannot fix.
2. A critical design flaw discovered during implementation, documented with the failure it causes.
3. A product decision by the owner that invalidates a stated assumption.

Anything else is implementation detail and does not amend this document. Amendments increment the minor version, are recorded in the decision register (§0.4), and **replace** the affected sections rather than appending to them. This document never carries two designs for one problem.

### 0.3 How to read it

Sections 1–4 define the product and its requirements. Sections 5–18 define the system. Section 19 walks the workflows end to end and is the fastest way to check that the parts fit. Section 20 is the build order. Section 21 states what is deliberately absent.

### 0.4 Decision register

Every architectural decision, with its status. A decision marked **superseded** has no surviving trace anywhere in this document; its replacement is the only design present.

| ID | Decision | Status |
|---|---|---|
| **A1** | Authorization derives from `CompanyMembership`, never from a role field on `User` | Adopted |
| **A2** | PostgreSQL 16 + pgvector from Phase 0; SQLite is not used in any environment | Adopted |
| **A3** | Server-rendered views call services directly; no parallel JSON API for the product surface | Adopted |
| **A4** | Permissions and tenancy are built in Phase 1, before any tenant data exists | Adopted |
| **A5** | Platform administration uses Django admin plus three purpose-built staff queues; no third SaaS application | Adopted |
| **A6** | Matching ranks with a deterministic scorer; the LLM explains, never ranks | Adopted |
| **A7** | Candidate data has two representations: a normalised profile (machine-readable canon) and immutable resume documents (wording canon), reconciled by an explicit protocol | Adopted, extended by A22 |
| **A8** | An application references an immutable `ResumeVersion` and the SHA-256 of the rendered file submitted | Adopted |
| **A9** | Video interview scoring evaluates answer content only; no eye-contact, confidence, engagement or sentiment scoring | Adopted |
| **A10** | AI is abstracted at the task level (`ai.run(task_key, …)`), not the provider API level | Adopted |
| **A11** | Every AI output is persisted as an `AIRun` row | Adopted |
| **A12** | Soft delete is selective, with a stated purge schedule per model | Adopted |
| **A13** | A canonical skill taxonomy with aliases and relations is infrastructure, built before matching | Adopted |
| **A14** | State changes and their side effects are decoupled by a transactional outbox | Adopted, extended by A25 |
| **A15** | Nepal-first, global-ready: local payment providers behind an interface, GDPR as design standard | Adopted |
| **A16** | AI video interview deferred to the final phase behind a legal review | Adopted |
| **A17** | Webhooks in v1; the public JSON API is specified but deferred | Adopted |
| **A18** | **Tenancy is scoped by URL** (`/employer/<company_slug>/…`), never by session | Adopted — **replaces** v1.0's session-scoped `request.company` |
| **A19** | **A mutable `ResumeDraft` is the builder's write target; `ResumeVersion` is immutable and created only by an explicit act** | Adopted — **replaces** v1.0's "the builder writes versions" |
| **A20** | **Prompts live in version control as files; a deploy step records their hash into `PromptTemplate`** | Adopted — **replaces** v1.0's database-authored prompts |
| **A21** | **The recommendation feed is computed on read and cached in Redis; no candidate×job score table exists** | Adopted — **replaces** v1.0's persisted `MatchScore` table |
| **A22** | **Every candidate-domain relation points at `CandidateProfile`; `User` is the authentication identity only** | Adopted — **replaces** v1.0's mixed `User`/`CandidateProfile` targets |
| **A23** | **`Application.stage` is the sole state; `status`, the `Rejection` table and `JobRevision` are removed** | Adopted — **replaces** v1.0's four-way duplication |
| **A24** | **Vector retrieval branches on filter selectivity; no unconditional ANN-after-filter** | Adopted — **replaces** v1.0's filter-then-retrieve ordering |
| **A25** | **Event fan-out is two-stage with a delivery ledger; no single-transaction fan-out** | Adopted — extends A14 |
| **A26** | **Messaging is a first-class domain** | Adopted — new; the frontend already advertises it |
| **A27** | **Embeddings, resume documents and event payloads all carry an explicit schema/model version** | Adopted — new |
| **A28** | **AI provider and model configuration lives in code, not in database tables** | Adopted — **replaces** v1.0's `AIProvider` / `AIModel` / `AITaskConfig` tables |
| **A29** | **`post_save`/`pre_save` signals are banned for business logic** | Adopted — new |
| **A30** | **Domain apps are introduced alongside the existing apps and portals move one per phase; no big-bang restructure** | Adopted — **replaces** v1.0's Phase 0 restructure |
| **A31** | **Content Security Policy uses nonces; `unsafe-eval` removed in P19 with the Alpine CSP build** | Adopted — **replaces** v1.0's unachievable "no `unsafe-inline`" requirement; the exception it granted is closed |
| **A32** | **Guest uploads are stored and scanned but never parsed, analysed or embedded before an account claims them** | Adopted — new |
| **A33** | **Consent is one table with purposes and policy versions** | Adopted — **replaces** three separate consent mechanisms |
| **A34** | **Contact details are revealed to an employer by stage, not at application** | Adopted — new |
| **A35** | **Redis is separated by role — /0 broker, /1 cache, /2 session cache — and holds no state whose loss changes a security or durability outcome** | Adopted (v1.2) |
| **A36** | **Dependencies are locked with hashes; `.in` files hold intent, compiled `.txt` files hold the graph, CI installs with `--require-hashes`** | Adopted (v1.2) |
| **A37** | **One correlation envelope — request, actor, company, task, event, AI run — propagated explicitly into Celery and cleared at task exit** | Adopted (v1.2) |
| **A38** | **`ResumeVersion` immutability permits exactly one transition: privacy redaction. Single-use token state lives in PostgreSQL, never in a cache** | Adopted (v1.2) |
| **A39** | **Salary normalisation stores full FX provenance: rate, rate id, as-of date and source** | Adopted (v1.2) |
| **A40** | **High-volume tables stay partition-ready and convert at a 20M-row threshold, not on a phase schedule** | Adopted (v1.2) — **replaces** v1.1's "partitioning from day one" |
| **A41** | **Rate limiting is a fixed-window mechanism with a per-rule fail-open/fail-closed policy** | Adopted (v1.2) |
| **A42** | **Multi-factor authentication uses `django-otp` rather than an in-tree TOTP implementation** | Adopted (v1.3) |
| **A43** | **Skills retire by status and forward through `replaced_by`; merge and deprecation are distinct facts, and nothing is deleted** | Adopted (v1.4) — extends A13 |
| **A44** | **External taxonomies arrive through a source interface with a two-phase resumable runner; imports fill blanks and never overwrite curation** | Adopted (v1.4) — extends A13 |
| **A45** | **Field-level encryption is one shared primitive over `cryptography`, keyed by a rotating list, with a deploy check; encrypted columns are deliberately unsearchable** | Adopted (v1.5) |
| **A46** | **Terms the taxonomy does not recognise are recorded and queued for curation, never discarded** | Adopted (v1.6) — extends A13 |
| **A47** | **Every alias and relation records source, confidence, author and batch; machine-written batches are reversible** | Adopted (v1.6) — extends A13 |
| **A48** | **`Skill.kind` classifies what sort of skill something is, independently of its category** | Adopted (v1.6) — extends A13 |
| **A49** | **Resume extraction writes proposals, never the profile; accepting calls the same service the profile screen calls** | Adopted (v1.7) — makes §5.5.3 structural |
| **A50** | **Rendering uses ReportLab (PDF) and python-docx (DOCX), both pure Python and deterministic; tagged PDF is deferred rather than claimed** | Adopted (v1.9) |
| **D-01** | **Django `is_staff` opens the admin site and nothing else.** It never reads a private CV, message attachment, verification document or any other private file; staff read those only through StaffRole-gated, audited staff screens. The admin shows file metadata only — no file field, download link or delete (§15.2) | Adopted — owner decision 2026-09-18 |
| **D-02** | **A rejected company may submit a new verification attempt thirty days after the decision.** Earlier is refused. The rejection's reason, reviewer, time and audit history are never overwritten (`CompanyVerificationDecision`), and each submission after a rejection is a new numbered attempt. Suspension still stops it (§5.6, §7.5, §19.2) | Adopted — owner decision 2026-09-18; **replaces** "rejected is terminal" |
| **D-03** | **An invitation is never bound to a different account.** It is accepted only by an account whose main address or an already verified secondary address is the invited one; anybody else signs in as that address or asks for the invitation to be sent again (§2.6) | Adopted — owner decision 2026-09-18; **replaces** §2.6's "bind the invite to the current account" option |
| **D-04** | **A staff reviewer may not claim or decide a company's verification** while they are an active member of it, hold a pending invitation to it, or submitted it themselves. Both the claim and the decision are refused and audited; there is no override, at any staff level (§19.5) | Adopted — owner decision 2026-09-18 |
| **D-05** | **Talent-search terms under three characters never match by substring.** They match a skill's whole name or alias (R, C, Go, UI); an unknown one-character term is refused with a message; three or more characters keep substring matching. Filters-only searches, rate limits and caps unchanged (§3.9, §10.6) | Adopted — owner decision 2026-09-18 |
| **D-06** | **Narrowing visibility revokes PUBLIC, POOL and INVITED grants** and removes the candidate from talent pools; APPLICATION grants and application records remain. Each revocation is audited per company with the reason (§2.7) | Adopted — owner decision 2026-09-18; **replaces** "narrowing revokes only public grants" |
| **D-07** | **Domain proof is optional additional evidence**, never a prerequisite for submitting documents; free-mail domains never count; reviewers see whether it exists (§19.2) | Adopted — owner decision 2026-09-18 |
| **D-08** | **Billing: Pro starts at once on a valid Free → Pro; Free → Pro starts a new period that day with a full-period invoice; Pro → Free takes effect at the end of the current period (Pro until then, invoice still owed); an overdue invoice blocks any new subscription or upgrade.** No proration, no refunds (§5.14, §21) | Adopted — owner decision 2026-09-18; **replaces** "a change takes effect at once and the period is untouched" |
| **D-09** | **Notification preferences stay account-level** — one set per person, never per company. The settings screens are role-aware: an employer without a candidate profile gets the employer rail and a way back to their company, and opening settings never creates a candidate profile; notification settings are linked from the employer rail and the account menu (§3.11) | Adopted — owner decision 2026-09-18 |
| **D-10** | **Announcements are deferred** — no model, management screen, publishing flow or `system.announcement` event in v1 (§3.12, §13.2, §21) | Deferred — owner decision 2026-09-18 |
| **D-11** | **Company Admin sees the plan, its status, usage and invoices but cannot change, cancel, resume or otherwise alter it; `billing.manage` stays with the Owner and cannot be handed out by anyone who lacks it** (§2.3, §2.4) | Adopted — owner decision 2026-09-17; **replaces** §2.3's "Company Admin: View, manage plan" |
| **D-12** | **Sign-up does not disclose whether an address is registered: every valid submission gets the same page, nothing is signed in, and the address's owner is told by email which case it was** (FR-1.1, FR-1.3) | Adopted — owner decision 2026-09-17 |
| **D-13** | **Recruiters read and act on applicants only for jobs they are explicitly assigned to; Owner and Company Admin read company-wide; `application.read_any` given explicitly widens one member** (§2.3) | Adopted — owner decision 2026-09-17; **replaces** §2.3's "Recruiters get company-wide read" default |

---

## 1. Product definition

### 1.1 What this is

A two-sided AI recruitment platform. Candidates build, analyse and version a CV, are matched to roles, apply, message employers, and interview. Employers post roles, run a hiring pipeline, search consenting talent, rank applicants, and interview. Platform staff operate, verify, moderate and bill.

### 1.2 What it is not

Not a job-board aggregator (nothing is scraped). Not a freelance marketplace. Not payroll or HRIS. Not a background-check provider. Not a video infrastructure vendor — live video runs on Jitsi.

### 1.3 Product invariants

Enforced architecturally, not by discipline. Each names the mechanism that enforces it.

| # | Invariant | Enforced by |
|---|---|---|
| **INV-1** | A CV is supplied once and is available everywhere | `ResumeDocument` is owned by `CandidateProfile` and referenced by analysis, builder, matching and applications |
| **INV-2** | Modules reuse data rather than copying it | Domain apps own data; portals own only presentation (§6) |
| **INV-3** | The system never silently rewrites a candidate's words, and never sends words they did not choose | Two shapes, one guarantee (§5.5.2, §5.8): where a draft already exists, AI writes a `ResumeSuggestion` and only an accept mutates it; where the text *is* the new thing — a cover letter — the draft lands in the candidate's own library and applying is a separate act with no path from drafting to submission |
| **INV-4** | An employer sees only what a candidate granted, when the stage warrants it | `CandidateVisibilityGrant` + queryset scoping + staged contact revelation (A34) |
| **INV-5** | Every automated result is explainable and reproducible | Deterministic scorer breakdown; `AIRun` with prompt version and input hash |
| **INV-6** | Nothing is advertised before it works | `apps/core/features.py` remains the shipping gate |
| **INV-7** | Two representations of a candidate never silently disagree | Canon per field class + `sync_state` + reconcile service (§5.5.3) |

### 1.4 Market and commercial context (A15)

Nepal-first, global-ready. Payments through eSewa / Khalti / Fonepay behind a `PaymentProvider` interface; Stripe is added on market entry, not swapped in. Hosting region Singapore or Mumbai. NPR is the default currency and every monetary column carries its own currency plus a normalised annual base (§5.6). GDPR is the design standard — the rights in §15.5 are built — but not yet a compliance obligation, so no DPO and no formal audit in v1. `TIME_ZONE` remains `Asia/Kathmandu` for display defaults; all storage is UTC.

---

## 2. Roles, tenancy and permissions

### 2.1 Three axes of authority

Authorization is not one enum. It is three independent facts:

1. **Account identity** — `User`, authenticated or not.
2. **Company membership** — `CompanyMembership`, a role *within one company*. A user may hold several simultaneously.
3. **Platform staff role** — `StaffRole`, global operator authority. Rare, MFA-mandatory, fully audited.

`User.signup_intent` (`candidate` | `employer`) exists only to choose an onboarding path and a default landing page. **No code branches on it for authorization or capability.** A user with a `CandidateProfile` is a candidate; a user with an active membership is an employer-side user; both can be true.

### 2.2 Tenancy is in the URL (A18)

Every employer surface is namespaced by company:

```
/employer/<company_slug>/overview/
/employer/<company_slug>/jobs/
/employer/<company_slug>/jobs/<job_slug>/applicants/
/employer/<company_slug>/company/verification/
```

`CompanyContextMiddleware` resolves the slug, loads the active `CompanyMembership` for `request.user`, and sets `request.company` and `request.membership`. A missing or inactive membership is a 404, never a 403 — a 403 confirms the company exists to someone with no right to know.

The session holds only `last_company_slug`, used to redirect a bare `/employer/` to somewhere sensible. It is a hint, never an authority.

**Why this matters:** a recruiter working at two companies in two browser tabs has one session. Session-scoped tenancy sends the losing tab's writes — a note, a rejection, a job edit — into the wrong company. URL scoping makes that structurally impossible and makes every employer URL shareable inside a team.

### 2.3 Role catalogue

**Global / account level**

| Role | Definition | Can | Cannot |
|---|---|---|---|
| **Guest** | Unauthenticated | Browse public jobs, company pages, marketing, template gallery, pricing; upload a CV that is stored and scanned but not processed (A32) | Apply, save, message, see match results or contact details, reach any dashboard |
| **Candidate** | Verified account with a `CandidateProfile` | The whole candidate module (§3.2–3.4); apply; message employers who hold a grant; control own privacy | See other candidates; see any employer-internal data, including notes, ratings and internal stage names |
| **Platform Support** | `StaffRole=support` | Read user and company records, answer tickets, resend verification, impersonate with a reason and a 60-minute expiry | Delete data, change billing, alter verification state |
| **Platform Moderator** | `StaffRole=moderator` | Act on abuse reports, unpublish jobs, suspend accounts and companies, review company verification | Billing, feature flags, AI configuration |
| **Platform Admin** | `StaffRole=admin` | The above, plus feature flags, email templates, plans and pricing, system settings | Alter the audit log, grant `superadmin` |
| **Super Admin** | `StaffRole=superadmin` | Everything, including staff role grants | Nothing — but every action is audited, and granting a staff role requires approval by a second superadmin |

**Company-scoped, via `CompanyMembership.role`**

| Role | Jobs | Applications | Candidates | Team | Company | Billing |
|---|---|---|---|---|---|---|
| **Owner** | Full | Full | Full | Invite, remove, change roles, transfer ownership | Edit, verify, delete | Full — the only preset holding `billing.manage` (D-11) |
| **Company Admin** | Full | Full | Full | Invite and remove non-owners | Edit, submit verification | View plan, status, usage and invoices; cannot change, cancel, resume or alter it (D-11) |
| **Recruiter** | Create and edit own jobs; publish if granted | Full on assigned jobs; **no access to applicants of unassigned jobs** unless `application.read_any` is granted explicitly (D-13) | Search, save to pools, message; applicants, their threads and their offer letters only on assigned jobs (D-13) | View | View | None |
| **Hiring Manager** | View assigned jobs, comment | Rate and comment on assigned jobs; stage moves and rejections only if granted | Only applicants to assigned jobs | View | View | None |
| **Interviewer** | None | Read-only on applications they are scheduled against, from scheduling until 30 days after the interview | Only assigned candidates | None | None | None |
| **Viewer** | Read | Read lists and aggregates; no PII export | None | View | View | None |

**Two defaults chosen deliberately:**

- **Recruiters are assignment-scoped for reads and writes (D-13).** A recruiter sees applicants — the list, the detail, notes, interviews, message threads started from an application, offer letters, talent-search results that exist only because somebody applied, and notifications — only for jobs they are assigned to. Authors are assigned to the jobs they create (and to the copies they duplicate); only members with `job.write_any` — Owner and Company Admin by default — change who else is assigned, because an assignment is a grant of applicant access. Company-wide applicant access belongs to Owner and Company Admin, or to a member given `application.read_any` explicitly by an Owner or Company Admin; it is never a Recruiter default. `strict_assignment_scoping` remains and now narrows only Viewers' list reads.
- **Plan management is the Owner's (D-11).** Company Admin reads the billing screen and is refused every write, in the template and on the server; no member can hand out a grant they do not hold, so `billing.manage` cannot reach an admin through an invitation or a role change.
- **Job assignment is real for writes.** `Job.recruiters` and `Job.hiring_managers` are memberships, and write access flows through them unless a grant key overrides it.

### 2.4 Permission grants

`CompanyMembership.permissions` is a set of **validated grant keys**, not free-form JSON. The full key set lives in `apps/companies/permissions.py` as an enum; writes validate against it; unknown keys are rejected. Current keys:

`job.publish` · `job.write_any` · `application.decide` (stage moves to terminal, rejections) · `application.read_any` · `offer.send` · `candidate.search` · `candidate.export` · `team.invite` · `billing.manage`

The role×capability matrix in §2.3 is **generated from that module**, so documentation cannot drift from enforcement. A capability with no test in the permission matrix suite fails the build.

### 2.5 Enforcement — three layers, all mandatory

```
1. Route guard        @require_permission("application.decide")   cheap rejection, UX only
2. Queryset scoping   Application.objects.for_actor(request)      the security boundary
3. Object policy      policies.can_reject(actor, application)     decisions and mutations
```

Rules that make this real:

- **No view may obtain an unscoped queryset for tenant-owned data.** Managers expose `for_actor()` / `for_company()` only. A CI check fails the build on `objects.all()` or `objects.filter(` inside portal packages.
- Object policies live in `policies.py` per domain app, and **every rule has a passing negative test**. A permission without a failing-case test does not count as implemented.
- `request.company` is derived from the URL and re-validated against an active membership on every request (§2.2). It is never read from a form field, a query parameter, or the session.

### 2.6 Membership lifecycle

- **Last-owner protection.** A company always has at least one active owner. Removing or demoting the last owner is rejected at the service layer and by a database check on the membership count.
- **Ownership transfer** is an explicit two-party flow: the current owner nominates, the nominee accepts, both are notified, and the transfer is audited.
- **Invites bind to an email address (D-03).** An invitation is accepted only by an account whose main address, or a secondary address it has already verified, is the invited one. Somebody signed in as a different address is shown the mismatch and can sign out and sign in as the invited address, or ask for the invitation to be sent again to the address they use. There is no option to bind the invitation to the current account: a forwarded link would otherwise put anyone who opened it on the team.
- **Suspension cascades.** Suspending a company pauses its published jobs, blocks new applications, cancels scheduled interviews with notice to candidates, and freezes messaging. Existing applications remain readable to the candidate.
- **Revocation is immediate.** Setting `deleted_at` on a membership removes access in the same request cycle, because every queryset derives from membership. Historical notes and events remain attributed to that person — the audit trail must not develop holes.

### 2.7 Candidate visibility and consent

`UserPreferences.profile_visibility` — `everyone` | `employers` | `nobody`, already present in the frontend — is backed by:

- **`nobody`** — no company can read the profile; matching still runs, so the candidate sees jobs while remaining invisible.
- **`employers`** — readable only where a `CandidateVisibilityGrant` exists, created on application and on explicit talent-search opt-in, revoked on withdrawal plus a retention window.
- **`everyone`** — a public profile page; search-engine indexing is a separate opt-in.

**Narrowing is a withdrawal (D-06).** Moving to `employers` or to `nobody` revokes every `public`, `pool` and `invited` grant the candidate's companies hold and removes them from those companies' talent pools, with one audit row per company recording the bases revoked and why. `application` grants stay — they are the candidate's own act towards that company — and so do the applications and what was sent with them. Under `nobody` no company can find or open the profile, the applied-to company included; that company keeps the application itself. Widening revokes nothing, and a block still revokes every grant, applications included.

`BlockedCompany` hides the candidate from named companies and hides those companies' jobs from the candidate — the "my current employer" case, which every serious platform needs.

**Staged contact revelation (A34).** An application does not expose the candidate's phone number and email. Contact details unlock when the application reaches a stage whose `reveals_contact` flag is set (default: the first interview stage) or when a recruiter explicitly unlocks with a reason. Every reveal writes `Application.contact_revealed_at`, an `ApplicationEvent`, and an audit row. This is cheap at the serializer boundary now and near-impossible to retrofit once employers expect the data.

---

## 3. Functional requirements

Priority: **M** must (v1), **S** should (v1.1), **C** could (later).

### 3.1 Identity and accounts

| ID | Requirement | Pri |
|---|---|---|
| FR-1.1 | Email + password registration; email is the login identifier. Sign-up never discloses whether an address is registered: the same page for a new and an existing address, no session created, a "you already have an account" email (with sign-in, reset and — if unconfirmed — confirmation links) to an existing address, rate-limited silently (D-12) | M |
| FR-1.2 | Email verification required before applying, posting a job, messaging, or any AI processing of uploaded content | M |
| FR-1.3 | Password reset via a single-use, time-limited, hashed token | M |
| FR-1.4 | Session and device list with individual and global revocation | S |
| FR-1.5 | TOTP 2FA; mandatory for company owners, company admins and all platform staff, with a 14-day grace period from role grant and a recovery-code path | S |
| FR-1.6 | OAuth (Google, LinkedIn) as an additional identity on an existing account; never an implicit merge by email | S |
| FR-1.7 | Magic-link login | C |
| FR-1.8 | Account deletion request, 14-day cancellable grace, then anonymisation (§19.4) | M |
| FR-1.9 | Data export as JSON plus original files | M |
| FR-1.10 | Staff impersonation with a recorded reason, a 60-minute expiry, no staff-on-staff use, and an `on_behalf_of` stamp on every action performed | S |
| FR-1.11 | Duplicate-account detection surfaced to employers as a soft signal when one person applies twice to a role from two accounts | S |

### 3.2 Candidate profile

| ID | Requirement | Pri |
|---|---|---|
| FR-2.1 | Structured profile: headline, summary, location, contact, notice period, salary expectation, open-to, seeking statement | M |
| FR-2.2 | Child records — experience, education, projects, certifications, languages, links, awards — independently editable and ordered | M |
| FR-2.3 | Skills linked to the canonical taxonomy, with years, self-declared level, and evidence provenance | M |
| FR-2.4 | Completeness score with named missing items, not a bare percentage | M |
| FR-2.5 | Import from a parsed resume field by field, with accept or reject per field; never a silent overwrite | M |
| FR-2.6 | Drift detection in both directions between profile and resume documents, with one-click reconcile (§5.5.3) | M |
| FR-2.7 | Link checking with reachable / broken / missing states | S |
| FR-2.8 | "Who viewed your profile and when", and separately who downloaded your CV | S |
| FR-2.9 | Career roadmap: target role, skill gap, ordered learning steps | C |
| FR-2.10 | Hosted portfolio pages | C |

### 3.3 Resume system

| ID | Requirement | Pri |
|---|---|---|
| FR-3.1 | Upload PDF or DOCX up to 10 MB, magic-byte type check, virus scan, page count, per-user checksum dedupe | M |
| FR-3.2 | Guest uploads are stored and scanned only; no parse, analysis or embedding until a verified account claims them; unclaimed uploads are swept after 24 hours (A32) | M |
| FR-3.3 | Parse to structured JSON with per-field confidence | M |
| FR-3.4 | Multiple resume documents per candidate, each with one mutable draft and an ordered chain of immutable versions | M |
| FR-3.5 | Three builder modes writing to the same draft: guided, editor, assistant — all three UIs exist | M |
| FR-3.6 | Autosave with honest save state (`dirty` / `saving` / `saved`), matching the existing interface | M |
| FR-3.7 | Undo and redo across a bounded snapshot ring, not the version chain | M |
| FR-3.8 | An immutable version is created only by an explicit act: save-a-version, request analysis, application submit, or export. Autosave never creates one | M |
| FR-3.9 | Template catalogue — layout × accent × font × tier, 12 seeded templates; applying a template never alters content | M |
| FR-3.10 | Deterministic server-side render to PDF and DOCX, cached by (version, template, format) | M |
| FR-3.11 | Rendered PDFs are tagged for accessibility | S | *Open after P6 — see ADR-A50. P6 delivers extractable text in reading order, metadata and a document language; structure tags need a different engine.* |
| FR-3.12 | Version history with diff, restore into the draft, and duplicate-to-new-document | M |
| FR-3.13 | Side-by-side comparison of two versions or two templates | S |
| FR-3.14 | Every application records the version and the SHA-256 of the file actually submitted | M |
| FR-3.15 | Re-analysis is free while content is unchanged, via input-hash cache | M |

### 3.4 Resume analysis

| ID | Requirement | Pri |
|---|---|---|
| FR-4.1 | Analysis runs against one `ResumeVersion` and an optional target role or job | M |
| FR-4.2 | Deterministic ATS checks with no LLM: extraction integrity, contact detection, tables and columns, heading recognition, date-format consistency, file naming, length, keyword coverage, measurable-outcome ratio | M |
| FR-4.3 | Scored breakdown across machine readability, evidence and impact, skills coverage, structure and length — each with a note | M |
| FR-4.4 | Findings classified critical / improve / strength, each with text, evidence and a concrete action | M |
| FR-4.5 | Skill extraction with evidence level (supported by a bullet, versus list-only) | M |
| FR-4.6 | Missing skills expressed as market demand, computed from matching rather than model opinion | M |
| FR-4.7 | Experience analysis: total, level, gaps, progression | M |
| FR-4.8 | Score history across analyses | M |
| FR-4.9 | Apply a finding's fix into the draft as a suggestion the candidate accepts | S |

### 3.5 Jobs

| ID | Requirement | Pri |
|---|---|---|
| FR-5.1 | Job lifecycle `draft → published → paused → closed → archived`, every transition audited | M |
| FR-5.2 | Fields per §5.7, including remote scope (remote-from-where) and salary with a normalised annual base | M |
| FR-5.3 | Required and preferred skills with weights and minimum years — the matcher's input | M |
| FR-5.4 | Screening questions with optional knockout rules | S |
| FR-5.5 | Search: keyword with Postgres full text plus trigram fallback, filters, and stable sorting | M |
| FR-5.6 | Saved jobs and saved searches; a saved search can become an email alert | M |
| FR-5.7 | Job alert emails on a daily or weekly cadence | M |
| FR-5.8 | Deadline urgency surfaced only inside 14 days, as a model property | M |
| FR-5.9 | View counting, deduplicated per viewer per 24 hours, aggregated asynchronously | M |
| FR-5.10 | Clone an existing posting into a new draft | S |
| FR-5.11 | Automatic close at `closes_at`, with notice to the owner beforehand | M |
| FR-5.12 | Optimistic locking so two editors cannot silently overwrite each other | M |
| FR-5.13 | External-apply mode with click tracking | C |

### 3.6 Applications and pipeline

| ID | Requirement | Pri |
|---|---|---|
| FR-6.1 | Apply with a chosen resume version, optional cover letter and screening answers; one live application per (job, candidate) | M |
| FR-6.2 | The application stores a snapshot of the job content it was made against | M |
| FR-6.3 | Configurable pipeline stages per company, defaulting to New → Reviewing → Shortlisted → Interview → Offer → Hired / Declined | M |
| FR-6.4 | Every state change appends an `ApplicationEvent`; the candidate timeline and employer activity log are two filtered views of that one stream | M |
| FR-6.5 | Candidate withdrawal at any point before an accepted offer | M |
| FR-6.6 | Employer bulk stage moves, internal notes, tags, ratings, teammate mentions | M |
| FR-6.7 | Rejection with a reason code and an optional message, never sent without a human action | M |
| FR-6.8 | Offers with salary, start date, expiry and an attached document; revisable, with one live offer at a time | S |
| FR-6.9 | Talent pools, with notes on a pooled candidate independent of any application | S |
| FR-6.10 | Candidate-visible status never exposes an internal stage name the company marked private | M |
| FR-6.11 | Notes, ratings and screening answers are company-internal, permanently | M |
| FR-6.12 | Candidate rediscovery: prior applications by the same person to the same company are surfaced | S |
| FR-6.13 | Per-candidate daily application limits, and a per-job exposure cap in recommendations | M |
| FR-6.14 | Re-application after rejection permitted after a company-configured cooldown | S |
| FR-6.15 | Employer response-time metrics, shown to the employer as a nudge and later to candidates as a public rate | S |

### 3.7 Messaging (A26)

| ID | Requirement | Pri |
|---|---|---|
| FR-7.1 | A company may open a conversation with a candidate only where a `CandidateVisibilityGrant` exists | M |
| FR-7.2 | Conversations are optionally anchored to a job or an application, and appear in both timelines | M |
| FR-7.3 | Candidates may reply, mute, block the company, or report the conversation | M |
| FR-7.4 | Unread counts, read receipts, and deep links from notifications | M |
| FR-7.5 | Attachments follow the same scanning and access rules as all files | S |
| FR-7.6 | Messages are retained under the same policy as applications and are included in data export | M |
| FR-7.7 | Rate limits per company per candidate, so messaging cannot become bulk outreach | M |

### 3.8 Matching and recommendations

| ID | Requirement | Pri |
|---|---|---|
| FR-8.1 | Score a candidate against a job 0–100 with matched and missing skill lists and a component breakdown | M |
| FR-8.2 | Labels from score bands: Strong ≥85, Good ≥70, Partial ≥50, otherwise Stretch | M |
| FR-8.3 | Candidate recommendations computed on read and cached, invalidated by profile, resume and job changes | M |
| FR-8.4 | Employer applicant ranking using the same scorer and the same breakdown | M |
| FR-8.5 | The score at application time is snapshotted onto the application and never recomputed | M |
| FR-8.6 | LLM explanation for the top results only, cached by input hash | S |
| FR-8.7 | Cold start: a candidate with no confirmed skills receives category and title-similarity results, labelled as such | M |
| FR-8.8 | Diversity and exposure limits so one job is not shown to every similar candidate | M |
| FR-8.9 | No protected attribute may enter the scorer; enforced by an allow-list and a failing test when the feature vector gains an unlisted field | M |

### 3.9 Talent search (employer side)

| ID | Requirement | Pri |
|---|---|---|
| FR-9.1 | Search only candidates whose visibility permits it, and only where a grant exists or is created by an explicit, logged opt-in | M |
| FR-9.2 | Filter by skills, experience, location, availability and salary expectation | M |
| FR-9.3 | Every profile view is logged and is visible to the candidate (FR-2.8) | M |
| FR-9.4 | Bulk export requires the `candidate.export` grant and triggers anomaly detection | M |

**Short terms (D-05).** A free-text term of three characters or more matches words in the headline and summary and skills whose name or alias contains it. A shorter term never matches by substring: it matches a skill whose whole name or alias it is — R, C, Go, UI — and, at two characters, the word itself. A one-character term that is no skill's name or alias is refused with a message. Filters-only searches are unchanged, as are the per-membership rate limit, the 200-result cap and anomaly detection.

**Built in P15.** `candidate.search` and `candidate.export` were declared in P1's grant catalogue and held no meaning until now; both are enforced in `services.talent`, and a recruiter holds the first by role preset while nobody below admin holds the second. Two access rules follow from the split and are asserted from the failing side: searching without the grant is a **403**, and a candidate outside the visible set is a **404** — never a 403, because a 403 would confirm that a person with that id is on the platform, which is the fact the visibility setting exists to withhold.

### 3.10 Interviews

| ID | Requirement | Pri |
|---|---|---|
| FR-10.1 | AI text interview: competency-driven plan, bounded dynamic follow-ups, per-answer rubric scoring with rationale, summary and recommendation | M |
| FR-10.2 | Candidate practice mode with private results | M |
| FR-10.3 | Questions are sampled from a per-competency pool and rotated, so a fixed bank cannot leak | M |
| FR-10.4 | Time-boxed answers, with paste and focus events recorded as metadata and never as score inputs | M |
| FR-10.5 | Live interview on Jitsi with per-participant short-lived JWT room access | S |
| FR-10.6 | Bookable interview slots with database-enforced non-overlap, a hold window during confirmation, ICS invites, and reminders at 24 hours and 1 hour | S |
| FR-10.7 | Reschedule, cancel and no-show flows for both sides | S |
| FR-10.8 | Independent scorecards, hidden from other interviewers until submitted | S |
| FR-10.9 | Multi-round interview plans per job | S |
| FR-10.10 | All interview kinds produce the same outcome shape | M |
| FR-10.11 | Practice results are always visible to the candidate; employer evaluations are shared at the employer's choice, defaulting to a summary on rejection | M |
| FR-10.12 | AI video interview with granular consent and content-only evaluation | C |

### 3.11 Notifications

| ID | Requirement | Pri |
|---|---|---|
| FR-11.1 | Six preference groups × two channels, exactly as the settings screens already define; a muted event is never delivered on that channel | M |
| FR-11.2 | In-app feed with unread counts, mark read, filters by kind, deep links | M |
| FR-11.3 | Transactional email with stored delivery status and a suppression list fed by bounces and complaints | M |
| FR-11.4 | Per-user send caps and quiet hours | S |
| FR-11.5 | Weekly digest with role-appropriate wording | S |
| FR-11.6 | Interview reminders | S |
| FR-11.7 | Deduplication so a burst becomes one notification | M |
| FR-11.8 | Web push | C |

**Preferences belong to the account (D-09).** One preference set per person, never per company: the same person may be a candidate, a recruiter at one company and the owner of another. The settings screens follow who is reading them — an employer with no candidate profile sees the employer rail for their company (or the company chooser with several) and no candidate sections, and opening any settings screen never creates a candidate profile. Notification settings are linked from the employer rail and the account menu.

### 3.12 Employer portal, platform administration, analytics

Employer: company profile and public page; verification as a state machine with documents and a reviewer note; team and invitations; jobs; applicant pipeline; talent search and pools; messaging; interviews; analytics; notification centre; billing.

Platform: Django admin for reference and record CRUD, plus three purpose-built staff queues — company verification review (with claim-and-lock so two reviewers cannot collide), abuse and moderation, and the AI run inspector. Plus feature flags and the audit-log viewer. System settings, email templates as rows and support tickets are deferred (§21); **announcements are deferred too (D-10)** — no model, staff screen or publishing flow in v1.

Analytics: as §16.

---

## 4. Non-functional requirements

| ID | Category | Requirement |
|---|---|---|
| NFR-1 | Performance | p95 server render under 300 ms for authenticated pages, measured with rendering and AI workers isolated from the web tier (§17.5); job search p95 under 500 ms at 1M jobs; HTMX fragment p95 under 150 ms |
| NFR-2 | Performance | No page issues more than 25 queries; asserted with `assertNumQueries` on the ten hottest views |
| NFR-3 | Async | Anything that can exceed one second — parse, analysis, render, embed, email, fan-out, match batch — runs in Celery. No HTTP request waits on an AI provider or a PDF render |
| NFR-4 | Availability | 99.5% in v1, 99.9% at scale. An AI or email provider outage degrades to a queued retry, never a 500 |
| NFR-5 | Scalability | Stateless web tier; sessions in Redis; files in object storage; horizontal scaling without code change |
| NFR-6 | Data integrity | Multi-table mutations inside `transaction.atomic`; state transitions validated in one place per aggregate; database check constraints for invariants that must never break; optimistic locking on concurrently edited aggregates |
| NFR-7 | Security | OWASP Top 10 baseline; CSP per A31; cookies `Secure`, `HttpOnly`, `SameSite=Lax`; HSTS; no private file reachable from the static server |
| NFR-8 | Privacy | Data classified per §15.3 with a retention duration per class, executed by a scheduled job |
| NFR-9 | Auditability | Every privileged action and every state transition on an application, job, company, membership or subscription is written to an append-only audit log |
| NFR-10 | Observability | Structured JSON logs carrying the correlation envelope of A37 — request, actor, company, task, event and AI-run identifiers — propagated into Celery through task headers and **cleared at task exit** so identifiers cannot bleed between tasks on a pooled worker; Sentry; queue depth and oldest-unprocessed-outbox metrics; AI cost and latency dashboards |
| NFR-11 | Testability | Services unit-tested without HTTP; ≥80% coverage on services and policies; a negative test per permission rule; golden-file tests for the scorer and for every prompt |
| NFR-12 | Accessibility | WCAG 2.1 AA maintained; server-rendered fragments and error states must not regress it; exported PDFs are tagged |
| NFR-13 | i18n | All user-facing strings through `gettext`; locale and timezone per user; localised dates, numbers and currency |
| NFR-14 | Maintainability | No business logic in views, models, templates or signals. Models hold data and invariants; services write; selectors read; views speak HTTP |
| NFR-15 | Deployability | One command from clean checkout to running stack; migrations backwards-compatible for one release |
| NFR-16 | Recoverability | RPO 15 minutes via WAL archiving; RTO 4 hours; restore rehearsed quarterly against a real dump |
| NFR-17 | Cost | AI spend per active candidate per month is budgeted, measured and enforced by quota, with a global kill switch |

---

## 5. Domain model and database

PostgreSQL 16 with `pgvector`. Every table carries `id` (BigAuto), `created_at` and `updated_at` unless stated. Externally visible identifiers are UUIDv7 `public_id`, so sequential keys never leak volume.

Notation: `PK` primary key · `FK→` foreign key · `U` unique · `IX` index · `PU` partial unique · `EX` exclusion constraint.

### 5.1 Conventions that apply to every table

1. **Money** is `Decimal(12,2)` plus a currency code. Any amount used in a comparison also stores a normalised annual base (§5.3).
2. **Enums** are `TextChoices` mirrored by a database `CHECK`, so a bad value fails even from a shell.
3. **JSONB holds documents, never relations.** Anything filtered, joined or aggregated is a column. `ResumeVersion.content` is a document; `CandidateSkill` is a relation. This line is not blurred.
4. **Every JSONB document column has a sibling schema-version column** (A27). Without it the first schema change breaks every historical row with no way to detect which shape it holds.
5. **Counters** (`views_count`, `applications_count`) are denormalised and updated asynchronously from the event stream, never with contended `F()` updates on the request path.
6. **`row_version`** (integer, incremented on save) exists on aggregates two people can edit at once — `Job`, `Company`, `CandidateProfile`, `ResumeDraft` — and is checked on submit. Last-write-wins on a job description is an unanswerable support ticket.
7. **Timestamps** are UTC. Display timezone comes from the user.
8. **Partitioning is deferred to a size threshold, not a phase** (A40). `AnalyticsEvent`, `AuditLog`, `AIRun`, `JobView` and `AttachmentAccess` stay **partition-ready** — no inbound foreign keys, `created_at` in every index — and convert to monthly range partitions when any one of them passes **20M rows**, with an alert at 15M for lead time. Conversion is a short maintenance window while a table is small and an impossible one while it is large, so the threshold sits well below the point where it stops being cheap.
9. **Soft delete is selective** (A12). Only `ResumeDocument`, `Job`, `Company`, `CompanyMembership`, `ApplicationNote`, `CompanyCandidateNote`, `TalentPoolMember`, `Message` and `Attachment` carry `deleted_at`, each with a partial unique index excluding deleted rows and a purge job with a stated retention. Everything else deletes for real. *(Corrected in v1.22: this list named `Tag`, which has never carried `deleted_at` — a tag is a label with a unique constraint per company, and soft-deleting one would stop a company re-creating a tag it had deleted — and omitted `CompanyCandidateNote`, which does. The purge is `apps.common.tasks.purge_soft_deleted`; `Attachment` purges through `files.services.purge`, which also removes the bytes from the bucket.)*

### 5.2 `accounts` — identity

**User** — `email U`, `full_name`, `signup_intent (candidate|employer)`, `is_active`, `is_staff`, `locale`, `timezone`, `last_seen_at`, `anonymised_at NULL`
`signup_intent` selects an onboarding path and nothing else (§2.1).

**EmailAddress** — `FK→User`, `email U`, `is_primary`, `verified_at` · supports work-email proof for company domain verification
**StaffRole** — `FK→User U`, `role`, `granted_by`, `granted_at`
**MFA devices** — provided by `django-otp` (ADR-A42), not modelled here. `otp_totp.TOTPDevice` holds the authenticator secret with drift tolerance and replay protection; `otp_static.StaticDevice` holds recovery codes, stored hashed. Two rules the library does not enforce are enforced in `accounts.services.mfa`: a device is unusable until confirmed by entering a code, and disabling MFA requires a valid code so a stolen session cannot remove the second factor.
**LoginIdentity** — `FK→User`, `provider`, `provider_uid`, `U(provider, provider_uid)`
**UserSession** — `FK→User`, `session_key`, `ip`, `user_agent`, `created_at`, `last_active_at`, `revoked_at` · a mirror of the Redis session store for the device list, pruned nightly

**Consent** (A33) — `FK→User`, `purpose`, `policy_version`, `granted`, `granted_at`, `revoked_at NULL`, `ip`, `evidence JSONB`, `IX(user, purpose, -granted_at)`
One table for every consent in the system: `terms`, `privacy`, `ai_processing`, `ai_training`, `interview_transcript`, `interview_audio`, `interview_video`, `marketing`. Current state is the newest row per `(user, purpose)`. Consent is never a boolean on another model, so an audit answers "what did they agree to, when, under which policy text" from one place.

**IdempotencyKey** — `key`, `FK→User NULL`, `endpoint`, `request_hash`, `response_ref JSONB`, `expires_at`, `U(key, endpoint)` · backs the idempotency guarantee in §8.1

**AuditLog** *(append-only; the application database role holds INSERT and SELECT only)* — `actor FK→User NULL`, `on_behalf_of FK→User NULL`, `verb`, `target_ct`, `target_id`, `target_repr`, `company FK→Company NULL`, `ip`, `metadata JSONB`, `created_at`
`IX(target_ct, target_id, -created_at)`, `IX(actor, -created_at)`, `IX(company, -created_at)`, monthly partitions. `target_repr` is denormalised because a generic foreign key cannot be joined cheaply and the referenced row may later be deleted.

### 5.3 `taxonomy` — shared reference data

**Skill** — `slug U`, `name`, `FK→SkillCategory`, `description`, `kind (A48)`, `status (active|deprecated|merged)`, `replaced_by FK self NULL PROTECT`, `retired_reason`, `retired_at NULL`, `usage_count`, `embedding vector(768) NULL`, `embedding_model NULL`, `embedded_at NULL` · `IX GIN(name trigram)`, `IX(status) WHERE active`, `CHECK` that `replaced_by` agrees with `status`
**SkillCategory** — `slug U`, `name`, `parent FK self NULL`
**SkillAlias** — `FK→Skill`, `alias`, `normalised` *(derived on save)*, `locale`, `source`, `confidence`, `created_by`, `created_reason`, `batch`, `U(normalised, locale)` · resolves "JS" to JavaScript
**UnresolvedTerm** (A46) — `term`, `normalised`, `locale`, `occurrences`, `source`, `source_counts JSONB`, `confidence`, `status (pending|resolved|rejected)`, `resolved_skill NULL`, `resolved_by`, `resolved_at`, `resolution_note`, `U(normalised, locale)` · the curation queue for skills the taxonomy is missing
**SkillTranslation** — `FK→Skill`, `locale`, `name`, `description`, `U(skill, locale)` · what the interface *shows*, as distinct from what resolution *accepts*
**SkillExternalId** — `FK→Skill`, `source`, `external_id`, `uri`, `U(source, external_id)` · ESCO, O\*NET and successors coexist rather than one winning a column
**SkillRelation** — `from FK→Skill`, `to FK→Skill`, `kind (broader|narrower|related|implies)`, `weight`, `source`, `confidence`, `created_by`, `created_reason`, `batch` · Django *implies* Python, which is how the scorer awards partial credit
**ImportRun** — `source`, `state (running|completed|failed)`, `phase (records|relations)`, `last_key`, `processed`, `created`, `updated`, `skipped`, `linked`, `finished_at NULL`, `error` · the checkpoint that makes an import resumable

**Skills leave service; they do not disappear (A43).** Deleting a skill would orphan every alias, relation and `CandidateSkill` that referenced it and make a curation mistake unrecoverable, so retirement is a status change. **Merging and deprecation are different facts**: a merged skill always *was* its replacement — a duplicate row — while a deprecated skill was genuinely distinct and is now obsolete. AngularJS was never Angular, and recording it as an alias would rewrite what an AngularJS developer's profile claims. Both forward through `replaced_by`, so resolution always returns the skill in service today, and the resolved match carries `was_retired` so an interface can explain the substitution instead of performing it silently.

**Names are not English-only.** `Skill.name` is the canonical English label; `SkillTranslation` carries per-locale display names, and normalisation is Unicode-aware — an ASCII whitelist folds "लेखा" to the empty string, which fails silently rather than loudly.
**JobCategory** — `slug U`, `name`, `parent FK self NULL`, `onet_code NULL`
**Industry** — `slug U`, `name`
**Location** — `country`, `region`, `city`, `geoname_id U NULL`, `lat`, `lng`, `timezone` · normalised, so "Kathmandu" is one row rather than forty strings
**Language** — `code U`, `name`

**FxRate** *(append-only — a historical rate is superseded, never mutated)* — `base`, `quote`, `rate`, `as_of`, `source`, `provider_ref`, `U(base, quote, as_of)` · refreshed daily
Salary comparison is impossible without it: a job at NPR 140,000 per month and an expectation of USD 45,000 per year cannot overlap until both are expressed as an annual base. Every salary-bearing model stores `salary_min_base` and `salary_max_base` in the platform base currency alongside the original figures, which are what the interface shows.

**Provenance is stored, not implied (A39).** Recording only the converted figure makes a historical match impossible to reproduce or explain, and "why did this job match my salary band last March" is a question both sides of the marketplace will ask. Each salary-bearing model therefore also stores `fx_rate_id` (FK), `fx_rate_value` (denormalised, so provenance survives even if the rate table is later pruned), `fx_as_of` and `fx_source`. Re-running a historical conversion reproduces the stored figure exactly.

Currency codes are validated as ISO 4217 before any rate is read: "Rs" and a genuinely missing rate would otherwise produce the same error, and an operator then hunts for the rate rather than the typo.

**External data arrives through an importer interface, not a hard-coded loader (A44).** The curated seed is a working core; ESCO, O\*NET or a national classification arrive through a source that yields `SkillRecord`s in ascending external-id order. The runner writes them in batches, checkpointing the last id committed, then makes a second pass to link hierarchy — a parent can appear after its child under any ordering keyed on id, and `ImportRun.phase` is what lets a resumed run know which half of the work it was in. Imports fill blanks and never overwrite curation: no renames, no stealing an alias another skill already claims, no replacing a curator's description.

Closed sets — `workplace`, `employment_type`, `experience_level`, `remote_scope`, `period`, `currency` — are `TextChoices`, not tables. They change only when code changes.

### 5.4 `candidates`

**`CandidateProfile` is the candidate aggregate root (A22).** It is created at registration, and every candidate-domain relation in the system points at it. `User` remains the authentication identity and holds nothing about employability. This makes erasure clean — anonymise one row and every foreign key stays valid — and removes the v1.0 inconsistency where some tables pointed at `User` and others at the profile.

**CandidateProfile** — `FK→User U`, `public_id`, `headline`, `summary`, `FK→Location NULL`, `phone (encrypted, A45)`, `notice_period_days`, `salary_min`, `salary_max`, `currency`, `period`, `salary_min_base`, `salary_max_base`, `fx_rate_id`, `fx_rate_value`, `fx_as_of`, `fx_source`, `open_to []`, `seeking`, `visibility`, `allow_contact`, `show_salary`, `search_indexing`, `completeness`, `is_searchable`, `experience_months`, `row_version`, `embedding vector(768) NULL`, `embedding_model NULL`, `embedded_at NULL`, `search_vector tsvector GENERATED`
`IX HNSW(embedding) WHERE is_searchable`, `IX GIN(search_vector)`, `IX(visibility) WHERE is_searchable`, `CHECK (salary_max >= salary_min)`
The embedding arrives with P13 and the search vector with P15, whose talent search is its only reader; everything else is built.

**`completeness`, `is_searchable` and `experience_months` are derived on every write**, never typed. A stored number describing data that changes is a number that goes stale — the frontend stand-in this replaced hard-coded 82% beside a list of gaps that contradicted the fields two inches above it. `row_version` guards the second browser tab, which is ordinary behaviour rather than an exotic one.

`experience_months` was added in P15 and is stored rather than computed per query for one reason: talent search filters and orders on months of work, and deriving it in SQL would have been a **third** implementation of arithmetic `apps.common.periods` already owns (P12's CV analyser, P13's matcher). `candidates.services.profile.experience_months` is now the one reading of a profile's work history, and `matching.extract` calls it rather than building its own list of periods. `search_vector` uses the `simple` configuration rather than `english`, per §10.5: this market writes mixed-script and transliterated content, and English stemming applied to a Nepali headline produces stems that match nothing.

**ProfileView** *(P15, FR-2.8, FR-9.3)* — `FK→CandidateProfile`, `FK→Company`, `actor FK→User NULL`, `source (search|application|pool)`, `created_at` · `IX(candidate, -created_at)` for the candidate's screen, `IX(company, -created_at)` for the anomaly check
One employer read of one profile. Its own table rather than `AttachmentAccess` — see §10.6. Append-only in intent: nothing updates a view, and a company must not be able to make one disappear. Partition-ready per ADR-A40, because this grows with employer activity rather than with the number of candidates.

**ProfileExperience** — `FK→CandidateProfile`, `title`, `org_name`, `FK→Company NULL`, `FK→Location NULL`, `start_date`, `end_date NULL`, `is_current`, `summary`, `bullets JSONB`, `order`
`CHECK (end_date IS NULL OR end_date >= start_date)`, `CHECK (NOT is_current OR end_date IS NULL)`

**ProfileEducation**, **ProfileProject**, **ProfileCertification** (`issuer`, `issued_on`, `expires_on`, `credential_id`, `file FK→Attachment NULL`), **ProfileLanguage** (`FK→Language`, `proficiency` CEFR), **ProfileLink** (`kind`, `url`, `status (ok|broken|unchecked)`, `last_checked_at`), **ProfileAward** — each `FK→CandidateProfile` with `order`

**CandidateSkill** — `FK→CandidateProfile`, `FK→Skill PROTECT`, `years NULL`, `level`, `source (self|resume|inferred)`, `evidence_ref JSONB`, `order`, `U(profile, skill)`
The table the matcher reads. **Never free text.** Input is resolved through `taxonomy.services.skills.resolve`; text that does not resolve is refused with the near matches, rather than creating a skill row per typo. `PROTECT` on the skill is what stops a curation mistake from erasing what someone said about themselves.

**SavedJob** — `FK→CandidateProfile`, `FK→Job`, `U(profile, job)`
**SavedSearch** — `FK→CandidateProfile`, `name`, `query JSONB`, `alert_frequency (off|daily|weekly)`, `last_alerted_at`
**BlockedCompany** — `FK→CandidateProfile`, `FK→Company`, `U(profile, company)`

### 5.5 `resumes`

#### 5.5.1 The draft/version split (A19)

The builder autosaves — `apps/resume/state.py` already ships `dirty`, `saving` and `saved_at`. If autosave wrote immutable versions, a twenty-minute session would produce hundreds of rows, the version list would be unusable, and "restore" would mean nothing. So the writing surface and the historical record are different tables:

```
ResumeDocument
  ├── ResumeDraft          1:1, MUTABLE — everything the builder writes
  ├── ResumeDraftSnapshot  bounded ring (25), undo/redo
  └── ResumeVersion        IMMUTABLE, append-only — created only by an explicit act
```

A `ResumeVersion` is created by exactly four acts: the candidate saves a version, requests an analysis, submits an application, or exports the document. Nothing else — and autosave is not one of them.

Analysis is on that list deliberately. Analysis runs against a version (FR-4.1) so that a score is always attributable to an exact document, and score history (FR-4.8) compares like with like.

**But analysis finalizes a version only when the content actually changed.** If the draft's content hash equals `current_version.sha256`, the existing version is reused. Without that check, ten clicks on Analyse produce ten identical versions — version spam arriving by a different route than the autosave the draft/version split was built to stop. The input-hash cache separately makes the *AI call* free; that defends the cost, not the row.

**Immutability is enforced three ways, because one is never enough:** `finalized_at` is set on insert and is `NOT NULL`; the model's `save()` raises on any update to a finalized row; and a Postgres `BEFORE UPDATE` trigger rejects the write regardless of what issued it, including a data-fix script. Without the trigger, A8 — the guarantee that an employer's copy of a CV cannot change — is a convention, and conventions do not survive contact with a production incident.

**Immutability means the historical meaning cannot change, not that storage can never be touched (A38).** Enforced literally, it made privacy erasure impossible: the row holds contact details and employment history, could not be updated, and could not be deleted because `Application.resume_version` is `PROTECT`. FR-1.8 was unimplementable.

The trigger therefore permits **exactly one transition**:

```
redacted_at IS NULL  →  redacted_at IS NOT NULL
```

In that transition, and only in it, `content` may be replaced by a fixed tombstone document and `content_schema_version`, `redaction_reason` and `redacted_by` may be set. **Every other column must be byte-identical afterwards** — `document_id`, `version_no`, `sha256`, `template_snapshot`, `created_by`, `created_reason`, `finalized_at` — and the trigger rejects the write if any of them differs. Redaction is itself single-use: a second attempt, from any source, raises.

`sha256` is deliberately retained. It is the evidentiary anchor proving what was submitted, and a hash of destroyed content is not personal data. After redaction the platform can still prove *that* a specific document was submitted and when, while holding none of it.

#### 5.5.2 Tables

**ResumeUpload** — `FK→CandidateProfile NULL`, `public_id`, `guest_token` *(hashed)*, `FK→Attachment PROTECT`, `original_filename`, `mime`, `size`, `pages`, `sha256`, `parse_status (blocked|pending|running|parsed|failed|unsupported)`, `parse_error`, `claimed_at NULL`, `PU(profile, sha256) WHERE profile IS NOT NULL`, `CHECK` that a row has an owner or a token
`parse_status` starts at **blocked** for a guest upload: the state itself is what refuses processing, rather than a comment asking callers to be careful. Scan status is read from the attachment and never mirrored here — two copies of one fact disagree eventually.
A guest upload has a `guest_token` and no profile. It is stored and scanned and nothing else (A32). Claiming attaches it to a profile and only then may processing begin. Unclaimed uploads are swept after 24 hours.

**ResumeParse** — `FK→ResumeUpload U`, `engine`, `engine_version`, `raw_text`, `structured JSONB`, `structured_schema_version`, `confidence JSONB`, `warnings JSONB`, `duration_ms` · P4's reader is deterministic and makes no model call. *(v1.15 correction: this line previously said an `AIRun` link "arrives with P9". P9 was messaging, and P12 did not add one — the deterministic reader is sufficient, and a model-assisted parse is unbuilt rather than pending. `warnings` is read by P12's `extraction` ATS check.)*
**ResumeImportProposal** (A49) — `FK→ResumeParse`, `FK→CandidateProfile`, `kind (skill|experience|education|language|contact|headline)`, `label`, `payload JSONB`, `confidence`, `evidence`, `state (offered|accepted|rejected|superseded)`, `decided_at`, `decided_by`, `outcome`, `order` · what a parse offers; accepting calls the candidate service that owns the field

**ResumeDocument** — `FK→CandidateProfile`, `name`, `kind (uploaded|built)`, `FK→ResumeUpload NULL`, `FK→ResumeTemplate`, `current_version FK→ResumeVersion NULL`, `builder_mode`, `is_primary`, `archived_at NULL`, `deleted_at NULL`
`PU(profile) WHERE is_primary AND deleted_at IS NULL` — exactly one primary CV, enforced by the database

**ResumeDraft** — `FK→ResumeDocument U`, `content JSONB`, `content_schema_version`, `row_version`, `dirty`, `saved_at`, `updated_at`
**ResumeDraftSnapshot** — `FK→ResumeDraft`, `seq`, `content JSONB`, `created_at`, `U(draft, seq)` · ring buffer of 25, trimmed on insert; replaces the session-backed undo stack in `apps/core/store.py`

**ResumeVersion** — `FK→ResumeDocument`, `version_no`, `content JSONB`, `content_schema_version`, `template_snapshot JSONB`, `sha256`, `created_by FK→User`, `created_reason (saved|analysed|applied|exported)`, `change_note`, `finalized_at NOT NULL`, `U(document, version_no)`

`content` holds exactly the shape `templates/components/paper.html` renders:
`{contact, summary, sections:[{key, title, visible, order, entries:[{title, org, dates, bullets[], source_ref, sync_state}]}], rail_sections:[…]}`

**ResumeTemplate** — `slug U`, `name`, `family`, `layout (classic|sidebar|modern)`, `accent`, `font`, `tier (free|pro)`, `suits`, `notes`, `is_active`, `preview FK→Attachment`
**ResumeRender** — `FK→ResumeVersion`, `FK→ResumeTemplate PROTECT`, `format (pdf|docx)`, `FK→Attachment PROTECT`, `sha256`, `engine`, `engine_version`, `pages NULL`, `duration_ms`, `fonts JSONB`, `U(version, template, format)`, `IX(version, format)` · rendering is a pure function of (version, template, format) **for a given engine version**; the constraint is the cache, and a render made by an older engine is replaced rather than served
`sha256` is the digest of the rendered bytes — what ADR-A8 means by the hash of the file submitted. `fonts` records what was actually used, because a fallback font is why a document can look different from last month. Redaction invalidates every render of a version: a tombstone is worthless while the original PDF is still downloadable (ADR-A38).

**ResumeAnalysis** — `FK→ResumeVersion`, `public_id`, `target_role NULL`, `FK→Job NULL`, `score`, `breakdown JSONB`, `verdict`, `headline`, `priority JSONB`, `experience JSONB`, `education JSONB`, `readability JSONB`, `skill_evidence JSONB`, `FK→AIRun NULL`, `algorithm_version`
`skill_evidence` is JSONB rather than a table: it is only ever read with its parent analysis.

**The score is deterministic (ADR-A6, extended to CVs in P12).** `algorithm_version` names the scorer that produced it; `breakdown` holds four weighted components — machine readability, evidence and impact, skills coverage, structure and length — each with its points and the note explaining them. The breakdown *is* the explanation the report renders, so there is no second account of the score that could disagree with it. `ai_run` points at the call that wrote `verdict`, `headline` and the findings marked `from_ai`; it is null when no model ran, and it is present-but-failed when one tried and could not. **A model never writes `score` or `breakdown`.**

That split is what makes the report survive an AI failure. With the kill switch off, the budget spent, the provider down or the output failing its schema, the analysis still has a score, the ATS checks, the measured facts and every deterministic finding; only the prose is missing, and the interface says so. §9.3.6 asks for exactly this and P12 is where it is demonstrated.

**ResumeFinding** — `FK→ResumeAnalysis`, `severity (critical|improve|strength)`, `code`, `title`, `text`, `evidence NULL`, `action NULL`, `section_ref NULL`, `order`, `from_ai`
`code` is stable across versions, so "this has been wrong for three versions" is a query rather than a memory. `section_ref` uses the `ResumeSuggestion.target_path` vocabulary and is what makes a finding fixable (FR-4.9); a finding without one offers no fix, and **findings written by a model never carry one** — the model does not know the content's internal ids, and a fix button writing to a guessed path is a fix button that corrupts a CV. `from_ai` is what the interface labels (§9.3.9).

**ResumeAtsCheck** — `FK→ResumeAnalysis`, `key`, `label`, `status (pass|warn|fail)`, `note NULL`, `order`, `U(analysis, key)`
No model is involved in any of these, ever. An applicant tracking system is a parser, and whether a parser can read a document is a fact about the document; asking a language model to guess at it returns a fluent answer with no relationship to what the parser does. Checks that need the original file — extraction integrity and file naming — read `ResumeParse.warnings` and `ResumeUpload.original_filename` when the document came from one, and report `pass` for a CV written in the builder rather than inventing a problem out of the absence of a file.
**ResumeSuggestion** — `FK→ResumeDraft`, `FK→ResumeFinding NULL`, `target_path`, `original`, `proposed`, `source`, `rationale`, `FK→AIRun NULL`, `state (offered|accepted|rejected)`, `decided_at`, `decided_by`
The mechanism that makes INV-3 structural where a draft already exists: AI proposes into this table; only an accept writes the draft. `ai_run` is null for a reconcile suggestion, which no model touched, and `source` records what produced it (`reconcile`, `ai`, `finding`, `assistant`). P12 filled the two foreign keys P5 had deferred for want of the tables.

**Both attributions are set when the row is created** (v1.17). P12 created the suggestion and then patched `ai_run` and `finding` in a second write, which left a row briefly existing with no attribution and — worse — meant every future AI caller would copy the patch. P14 added the second such caller (the assistant builder), so `suggestions.offer` takes them as parameters instead. Three callers now share one creation path, which is what "the only path from proposal to document" was supposed to mean.

**BuilderSession** — `FK→ResumeDocument`, `mode`, `step_state JSONB`, `transcript JSONB`, `updated_at`

**Two clarifications from building it (v1.8).** `created_reason` is a `TextChoices` with exactly four members, and *that membership is the enforcement* — a fifth act that wants to create a version is an architecture change, not a new string. `ResumeSuggestion.target_path` is a closed vocabulary (`summary`, `contact.<field>`, `entry.<id>.<field>`, `section.<id>.items`) rather than a general path expression: suggestions are generated by this codebase, and a syntax that can address anything can address the wrong thing.

#### 5.5.3 Profile↔resume reconciliation (A7, INV-7)

Provenance alone is not a protocol. Each class of field has one canonical owner, and drift is a state the interface can render:

| Field class | Canon | Direction |
|---|---|---|
| **Facts** — employer, dates, titles, institutions, credentials | `CandidateProfile` and its children | Profile → document, offered as a reconcile prompt, never silent |
| **Wording** — summary, bullets, ordering, emphasis | `ResumeVersion` / `ResumeDraft` | Document → profile only on explicit accept |
| **Skills** | `CandidateSkill` | Extraction proposes; the candidate accepts; the profile is canon |

Every document entry carries `source_ref` (the profile row it came from) and `sync_state ∈ {linked, edited, detached}`. `linked` follows the profile; `edited` has diverged and is reported as drift; `detached` is document-only by choice.

`resumes.services.reconcile(document)` returns a diff the interface renders with the same accept/keep-mine vocabulary the assistant builder already uses. It runs on `profile.updated` and `resume.version_created`, and its output drives the "two skills on your CV are not on your profile" message the dashboard already shows.

### 5.6 `companies`

**Company** — `slug U`, `name`, `legal_name`, `FK→Industry`, `size_band`, `website`, `tagline`, `about JSONB`, `founded`, `logo FK→Attachment NULL`, `FK→Location`, `verification_state`, `verified_at NULL`, `created_by`, `strict_assignment_scoping`, `state (active|suspended)`, `row_version`, `deleted_at NULL`
**CompanyDomain** — `FK→Company`, `domain`, `verified_at NULL`, `PU(domain) WHERE verified_at IS NOT NULL`
**CompanyVerification** — `FK→Company`, `state`, `submitted_at`, `submitted_by FK→User NULL`, `attempt`, `claimed_by FK→User NULL`, `claimed_until NULL`, `reviewed_by NULL`, `reviewed_at NULL`, `reason`, `steps JSONB` · `claimed_by`/`claimed_until` stop two staff reviewers colliding on the same queue item · `submitted_by` is what the D-04 conflict check reads · `attempt` is 1 for the first submission and one more for each submission after a rejection (D-02)
**CompanyVerificationDecision** *(D-02)* — `FK→CompanyVerification`, `attempt`, `state`, `reason`, `reviewed_by FK→User NULL`, `decided_at`, `submitted_at`, `submitted_by NULL`, `documents JSONB` · written once per reviewer decision and never updated or deleted: the record that keeps a rejection's reason, reviewer and time after the company applies again. A document a reviewer decided on is kept when the company removes it from a new attempt
**CompanyVerificationDocument** — `FK→CompanyVerification`, `kind`, `FK→Attachment`, `uploaded_at`

**CompanyMembership** *(the authorization spine — A1)* — `FK→Company`, `FK→User`, `role`, `permissions []` (validated grant keys, §2.4), `title`, `invited_by`, `joined_at`, `status (active|suspended)`, `deleted_at NULL`
`PU(company, user) WHERE deleted_at IS NULL`, `IX(user) WHERE deleted_at IS NULL` — the second index serves the company switcher, which renders on every employer request
A deferred constraint plus a service-level guard enforce **at least one active owner per company** (§2.6).

**CompanyInvite** — `FK→Company`, `email`, `role`, `permissions []`, `token_hash`, `expires_at`, `accepted_at NULL`, `accepted_by FK→User NULL`, `invited_by`
**CompanyOffice** — `FK→Company`, `FK→Location`, `name`, `is_headquarters`

### 5.7 `jobs`

**Job** — `FK→Company`, `slug U`, `title`, `FK→JobCategory`, `description`, `responsibilities JSONB`, `requirements JSONB`, `benefits JSONB`, `FK→Location`, `workplace`, `remote_scope (none|city|country|region|worldwide)`, `remote_countries []`, `employment_type`, `experience_level`, `salary_min`, `salary_max`, `currency`, `period`, `salary_min_base`, `salary_max_base`, `fx_rate_id`, `fx_rate_value`, `fx_as_of`, `fx_source`, `salary_visible`, `status (draft|published|closed|expired|archived)`, `published_at NULL`, `closes_at NULL`, `closed_at NULL`, `closed_reason`, `archived_at NULL`, `created_by`, `apply_mode`, `external_url NULL`, `application_cooldown_days`, `views_count`, `applications_count`, `row_version`, `search_vector tsvector GENERATED`, `embedding vector(768) NULL`, `embedding_model NULL`, `embedded_at NULL`, `deleted_at NULL`

`IX GIN(search_vector)` · `IX HNSW(embedding) WHERE status='published'` (a partial index, because nearly every vector query also filters on published) · `IX(status, -published_at) WHERE status='published'` · `IX(company, status)` · `CHECK(salary_min <= salary_max)` · `CHECK(status <> 'published' OR published_at IS NOT NULL)` · `CHECK(workplace <> 'remote' OR remote_scope <> 'none')`

`remote_scope` answers "remote from where", which `workplace='Remote'` alone does not, and whose absence is the most common complaint on every job board.

**JobSkill** — `FK→Job`, `FK→Skill`, `requirement (required|preferred)`, `weight`, `min_years NULL`, `U(job, skill)`
**JobAssignment** — `FK→Job`, `FK→CompanyMembership`, `kind (recruiter|hiring_manager)`, `U(job, membership, kind)` · assignment-scoped write access (§2.3)
One table with a `kind` rather than `JobRecruiter` and `JobHiringManager` as separate models: the two differ only in what the assignment *means*, and two tables with identical columns drift the moment one gains a field.
**JobScreeningQuestion** — `FK→Job`, `order`, `kind`, `text`, `options JSONB`, `is_required`, `knockout JSONB NULL`
**JobView** — `FK→Job`, `viewer_hash`, `FK→User NULL`, `referrer`, `created_at` · monthly partitions, rolled up nightly

`search_vector` is `GENERATED ALWAYS ... STORED` over `title` and `description` (A51). Not a trigger and not a `save()` hook: both are bypassed by `queryset.update()`, and the failure mode is a job that is silently unfindable by the words printed on it.

There is **no `JobRevision` table** (A23, A51). The requirement it existed for — an applicant seeing what they applied to — is met by `Application.job_snapshot`, which is simpler, is scoped to the row that needs it, and cannot drift.

**JobAlertMatch** — `FK→SavedSearch`, `FK→Job`, `score`, `computed_at`, `U(saved_search, job)`
The only persisted matching artefact (A21). Alerts are genuinely batch work with a fixed recipient set; recommendations are not.

### 5.8 `applications`

**PipelineStage** — `FK→Company NULL` (null = platform default), `key`, `label`, `order`, `kind (open|interview|offer|terminal)`, `is_visible_to_candidate`, `candidate_label`, `reveals_contact`
**Application** — `FK→Job`, `FK→CandidateProfile`, `FK→ResumeVersion` *(immutable)*, `submitted_render_sha256`, `FK→CoverLetter NULL`, `FK→PipelineStage`, `answers JSONB`, `source`, `job_snapshot JSONB`, `job_snapshot_schema_version`, `match_snapshot JSONB`, `contact_revealed_at NULL`, `closed_reason NULL`, `closed_at NULL`, `applied_at`, `last_activity_at`, `withdrawn_at NULL`

`PU(job, candidate) WHERE withdrawn_at IS NULL AND closed_at IS NULL` — one live application per role · `IX(job, stage, -applied_at)` · `IX(candidate, -applied_at)` · `IX(candidate, stage)`

**Reopening is an explicit move.** A closed application returns to a live stage only through `pipeline.move`, and only for an actor holding `application.decide` — the same grant the closure needed. A rejection anyone could quietly reverse is not a decision anybody is accountable for.

**`stage` is the only state (A23).** There is no `status` column: it is a property derived from `stage.kind`. There is no `Rejection` table: a rejection is a move to a `terminal` stage plus `closed_reason`, with any message carried in the `ApplicationEvent` payload. Four representations of one fact in v1.0 became one.

**ApplicationEvent** *(append-only — the single source of both timelines)* — `FK→Application`, `actor FK→User NULL`, `actor_kind (candidate|employer|system|ai)`, `type`, `from_stage NULL`, `to_stage NULL`, `payload JSONB`, `visible_to_candidate`, `created_at` · `IX(application, -created_at)`
**ApplicationNote** — `FK→Application`, `author FK→User`, `body`, `mentions []`, `deleted_at NULL` *(company-internal, permanently)*
**Tag** — `FK→Company`, `name`, `colour`, `U(company, name)`; **ApplicationTag** — `FK→Application`, `FK→Tag`
**ApplicationRating** — `FK→Application`, `rater FK→User`, `score (1-5)`, `comment`, `U(application, rater)` · one score per rater, so a panel produces a spread rather than a fight over one field
**CoverLetter** — `FK→CandidateProfile`, `FK→Job NULL`, `public_id`, `title`, `content`, `source (written|ai_draft|ai_edited)`, `tone (direct|warm|formal)`, `FK→AIRun NULL` · `IX(profile, -created_at)`

`source` is recorded because the candidate is the author of what they send, and a letter they wrote and one a model drafted are different things to them even when the text is identical. `ai_edited` is the honest middle state and the one the product is actually built for. `ai_run` is the link P8 could not create and P11 could not populate; P14 fills it, and the words changing later does not unset it — the fact that a model started them remains true.

**A letter is not versioned like a resume, and the difference is deliberate.** A `ResumeVersion` is immutable because an employer's copy of a CV must not change (ADR-A8). A letter is attached to an application by foreign key, so the application already holds the one that was sent; what has to be prevented is *editing that letter afterwards*, which would rewrite what an employer read. A sent letter is therefore read-only and offers a copy instead — the same guarantee, reached with a foreign key rather than a version table.

**INV-3 covers cover letters through a different shape.** A resume needs a `ResumeSuggestion` because a draft already exists and must not be overwritten by a model. A cover letter *is* the new thing being made, so the letter itself is the proposal: it lands in the candidate's library, they edit it, and applying is a separate explicit act with no code path from drafting to submission. Both reach the guarantee that no AI-written text becomes a candidate's submission without them choosing it.
**Offer** — `FK→Application`, `salary`, `currency`, `salary_base`, `start_date`, `expires_at`, `FK→Attachment NULL`, `state (draft|sent|accepted|declined|withdrawn|expired)`, `sent_by`, `decided_at NULL`
`PU(application) WHERE state IN ('draft','sent')` — one *live* offer, many historical. Offers get revised; a unique-per-application constraint would have forced destructive edits.

**TalentPool** — `FK→Company`, `name`, `created_by`
**TalentPoolMember** — `FK→TalentPool`, `FK→CandidateProfile`, `added_by`, `deleted_at NULL`, `U(pool, candidate)`
**CompanyCandidateNote** — `FK→Company`, `FK→CandidateProfile`, `author`, `body`, `deleted_at NULL` · notes on a sourced candidate who has no application
**CandidateVisibilityGrant** *(INV-4)* — `FK→CandidateProfile`, `FK→Company`, `basis (application|public|pool|invited)`, `FK→Application NULL`, `granted_at`, `expires_at NULL`, `revoked_at NULL` · every employer read of a candidate is authorised by a row here

### 5.9 `messaging` (A26)

The frontend already ships an `employer_messages` preference and a `message` notification kind. Without these tables the settings screen governs a feature that does not exist, and hiring conversations move to personal email where no analytics, audit, consent or retention machinery reaches.

**Conversation** — `FK→Company`, `FK→CandidateProfile`, `FK→Job NULL`, `FK→Application NULL`, `state (open|archived|blocked)`, `opened_by`, `last_message_at`, `U(company, candidate, job)`
*(No `ConversationParticipant` table. A conversation is permanently one company and one candidate, so a join table for a fixed two-party relation is generality with no case behind it. Read and mute state are columns on `Conversation`: `company_last_read_at`, `candidate_last_read_at`, `company_muted_at`, `candidate_muted_at`.)*
**Message** — `FK→Conversation`, `sender FK→User`, `body`, `attachments []` (FK→Attachment), `created_at`, `edited_at NULL`, `deleted_at NULL` · `IX(conversation, -created_at)`
**ConversationReport** — `FK→Conversation`, `reporter FK→User`, `reason`, `state`, `resolution`

**Message attachments** are shared with the recipient by an `AttachmentGrant` row (A52), not by loosening file visibility: a file leaves the sender's control for exactly one person, for a stated reason, and deleting the message takes that back.

Rules: a company may open a conversation only where a `CandidateVisibilityGrant` exists; the candidate may block, which is one operation with three effects — `state='blocked'`, a `BlockedCompany` row, and every visibility grant revoked — implemented once in `candidates.services.blocking` and reachable from both the thread and the privacy screen; per-company-per-candidate **and** per-company-daily rate limits prevent messaging becoming bulk outreach, because either limit alone permits it.

### 5.10 `interviews`

**InterviewTemplate** — `FK→Company NULL`, `kind (ai_text|live)`, `name`, `config JSONB`, `rubric_version`
`InterviewTemplate.question_pools JSONB` — competency to question list. Questions are **sampled and rotated** per session so a fixed bank cannot leak to a forum. A table was considered and rejected: the pool is never queried across templates, so it is a document, and SRS §5.1.3 puts documents in JSONB.
**InterviewRound** — `FK→Job`, `order`, `FK→InterviewTemplate`, `name`, `is_required`

**InterviewSlot** — `interviewer FK→CompanyMembership`, `starts_at`, `ends_at`, `state (open|held|booked|cancelled)`, `held_until NULL`, `session FK→InterviewSession NULL`
`EX USING gist (interviewer WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE (state <> 'cancelled')`
Double-booking is prevented by the database, not by a check-then-insert race. A hold window covers the seconds between a candidate choosing a slot and confirming.

**InterviewSession** — `FK→Application NULL` (null = practice), `FK→CandidateProfile`, `FK→InterviewRound NULL`, `kind`, `state (scheduled|in_progress|completed|abandoned|cancelled|no_show)`, `scheduled_at`, `started_at`, `ended_at`, `provider`, `room_ref`, `join_token_hash`, `created_by`, `cancelled_reason NULL`, `rescheduled_from FK self NULL`
**InterviewParticipant** — `FK→InterviewSession`, `FK→User`, `role (interviewer|observer)`, `attended`, `invited_at`
**InterviewTurn** — `FK→InterviewSession`, `order`, `question_text`, `question_origin (pool|ai_followup)`, `answer_text`, `started_at`, `duration_ms`, `integrity_meta JSONB`, `FK→AIRun NULL`
`integrity_meta` records paste and focus events. It is metadata shown to the employer as context; **it is never a scoring input**, because typing behaviour is not job performance.
**InterviewEvaluation** — `FK→InterviewSession`, `competency`, `score`, `rationale`, `rubric_version`, `FK→AIRun NULL`, `evaluator FK→User NULL`
**InterviewOutcome** — `FK→InterviewSession U`, `recommendation`, `summary`, `decided_by`, `shared_with_candidate`
**InterviewerAvailability** — `FK→CompanyMembership`, `starts_at`, `ends_at`, `recurrence JSONB`, `timezone`

Video-specific tables (`InterviewMediaAsset`, `InterviewSignal`) are **not part of v1.1** and arrive with the video phase (§21). Consent for any interview scope already lives in the unified `Consent` table (§5.2), so adding them later requires no consent redesign.

**Built in P16. Five details worth stating, because each is a place a reasonable reading would guess differently:**

- **`application IS NULL` is what makes practice private** (FR-10.2). Not a permission check — a structural one. `sessions_for_company` filters `application__isnull=False`, so no employer queryset can reach a practice session through any join, and the promise holds without anybody remembering to check.
- **`InterviewEvaluation.submitted_at` is the anchoring control** (FR-10.8). Until it is set the row is returned only to its author. A scorecard is written *and* submitted in one act; the null state exists for a future editing flow, and the visibility rule already holds for it.
- **`InterviewOutcome.recommendation` is four positions, not a score.** A number invites averaging across interviewers, which is how a panel's disagreement disappears into a mean nobody argued for. `average_score` is recorded alongside for the analytics in §16.3, and the recommendation is what a person acts on.
- **A model's evaluation and a person's are the same table**, distinguished by `evaluator IS NULL`. §11.1's uniform shape is not a convention here; it is one table with one unique constraint per (session, competency, evaluator).
- **An interview never moves an application.** The consumer records the completion on the timeline and stops. Moving somebody to rejected because a model scored them 2.4 is exactly the automated adjudication ADR-A9 refuses, and moving them forward is the same mistake wearing a friendlier face.

**Events map to the `application_updates` preference group**, not a group of their own. An interview is a step in an application, a candidate who muted application updates has already said what they want, and §13.1 requires every delivered event to map to a group somebody can toggle — a new group would need a new toggle for a decision nobody separately makes.

### 5.11 `ai`

Provider and model configuration lives **in code** (A28) — a settings module of provider definitions, model tiers, per-task routing, timeouts and cost ceilings, reviewed and deployed like everything else. Three database tables for six rows bought runtime provider swapping nobody performs at 2 a.m., plus an admin surface, plus a cache. What must be in the database is the record of what actually happened:

**PromptTemplate** — `task_key`, `version`, `body`, `body_sha256`, `output_schema JSONB`, `source_path`, `loaded_at`, `U(task_key, version)`
Prompts are files in version control (A20); a deploy step loads them and records the hash here. Reviewed, diffable, testable, rolled back with the code that depends on them — while `AIRun.prompt_version` keeps the audit property intact.

**AIRun** — `task_key`, `prompt_version`, `provider`, `model`, `input_hash`, `input_ref_ct/id`, `input_snapshot JSONB`, `output JSONB`, `output_schema_version`, `raw_output`, `status (pending|ok|schema_error|provider_error|timeout|quota_exceeded|refused|disabled)`, `error`, `tokens_in`, `tokens_out`, `cost`, `latency_ms`, `cached_from FK self NULL`, `FK→User NULL`, `FK→Company NULL`
`IX(task_key, prompt_version, input_hash)` — the cache lookup · `IX(user, -created_at)`, `IX(company, -created_at)` — cost attribution · monthly partitions

**AIFeedback** — `FK→AIRun`, `FK→User`, `verdict (good|bad)`, `comment` · the eval corpus, collected from the first day
**AIUsageQuota** — `owner_ct/id`, `period`, `runs`, `tokens`, `cost`, `limit_cost` · checked before dispatch, never after

**One pipeline (P11).** Every AI entry point in the system calls `ai.services.runner.run(task_key=…, inputs=…, user=…, company=…)`. It performs, in order: the kill switch (the `ai` feature flag), the cache lookup, the quota check, dispatch down the task's fallback chain, schema validation, and the `AIRun` record. The cache is consulted **before** the budget, deliberately: a cache hit spends nothing, and refusing a free answer to an out-of-budget account would punish the one request pattern the budget wants to encourage. A feature supplies a task key and inputs; it never names a provider, never retries, never prices a call and never writes an `AIRun` itself. This is a rule, not a convention: orchestration duplicated per feature drifts per feature, and the first copy to skip the quota check would not announce itself. Providers implement one `Adapter` Protocol (`complete(request, *, model, timeout) -> Result`) and raise one of three exceptions — `ProviderTimeout`, `ProviderFailure`, `ProviderRefused` — which is the only classification the runner needs to decide whether another attempt could possibly help.

Three properties of the implementation worth stating, because each is a place a reasonable reading would guess wrong:

- **A cache hit is a new row**, with `cached_from` pointing at the run that did the work and `cost` zero. Returning the original row would attribute one person's request to another and lose the fact that the question was asked twice.
- **Spend is recorded whatever the outcome.** A timeout that burned input tokens cost money.
- **The kill switch has its own status** (`disabled`), not `provider_error`. The two look identical on an error-rate dashboard and are opposites: one is a provider having a bad afternoon, the other is us switching AI off on purpose. Folding the deliberate case into the provider error rate fires the alert during the incident you already know about and buries the real provider failures under it. Added during P11 after the dashboard requirement in DEVELOPER.md §5.9 was read against the implementation.
- **`AIRun` is not partitioned yet.** §5.11's monthly partitions are the right shape at volume; at four months of rows they are migration complexity with no reader. The indices the cache lookup and the cost queries need exist now, and partitioning is a later migration on a table whose columns will not change.

**Evaluation (A20).** `prompts/evals/*.json` hold structural assertions — required fields, minimum item counts, terms that must or must not appear — never exact model text, which produces a suite that fails on every harmless rewording and is switched off within a month. `manage.py run_evals` exits non-zero on a failure, so a prompt change that degrades quality fails CI. The corpus runs against a deterministic fake adapter: no key, no network, no cost.

### 5.12 `events`, `notifications`, `webhooks`

**OutboxEvent** — `aggregate_ct/id`, `type`, `schema_version`, `payload JSONB`, `created_at`, `available_at`, `processed_at NULL`, `attempts`, `last_error`
`IX(processed_at, available_at) WHERE processed_at IS NULL`, drained with `FOR UPDATE SKIP LOCKED`. **Processed rows are deleted after a 24-hour grace period.** The outbox is a queue, not a history; `AuditLog` and `ApplicationEvent` are the history.

**FanoutJob** — `FK→OutboxEvent`, `audience`, `cursor_start`, `cursor_end`, `chunk_size`, `state`, `attempts`, `available_at` · the second stage of A25. The cursor is a **primary-key range, not an offset**: an offset shifts under concurrent inserts and the symptom is a recipient silently skipped, which nothing downstream can detect. `audience` is a registry key rather than a stored queryset, because the chunk executes in a different process from the one that planned it.
**NotificationDelivery** *(idempotency ledger)* — `event_id`, `recipient_id`, `channel`, `status`, `U(event_id, recipient_id, channel)` · a retried chunk physically cannot double-send

**Notification** — `FK→User`, `kind`, `title`, `body`, `actor_ct/id NULL`, `target_url`, `payload JSONB`, `dedupe_key NULL`, `read_at NULL`
`PU(recipient, dedupe_key) WHERE dedupe_key IS NOT NULL AND read_at IS NULL` · `IX(recipient, -created_at) WHERE read_at IS NULL` · `IX(recipient, kind, -created_at)`
**NotificationPreference** — `FK→User`, `event_key`, `in_app`, `email`, `push`, `quiet_hours JSONB`, `U(user, event_key)`
**EmailMessage** — `FK→User NULL`, `to_email`, `template_key`, `params JSONB` (minimal — never a full context dump, which duplicates PII and grows without bound), `provider_message_id`, `status`, `error`, `opened_at`
**EmailSuppression** — `email U`, `reason (bounce|complaint|manual)`, `created_at` · consulted before every send; without it a bounce storm costs the sending domain's reputation

**WebhookEndpoint** — `FK→Company`, `url`, `secret (encrypted, A53)`, `event_types []`, `is_active`, `consecutive_failures`, `disabled_at NULL`, `created_by` · encrypted rather than hashed because the platform signs with it; an endpoint that fails constantly is disabled rather than retried forever
**WebhookDelivery** — `FK→WebhookEndpoint`, `FK→OutboxEvent`, `attempt`, `status_code`, `response_excerpt`, `next_retry_at`, `delivered_at NULL`, `IX(endpoint, -created_at)`
A webhook URL is a user-supplied URL the server fetches, so it is subject to the same SSRF allow-list as link checking (§15.4).

### 5.13 `files`

**Attachment** — `public_id UUID U`, `owner_candidate FK→CandidateProfile NULL`, `owner_company FK→Company NULL`, `storage_key`, `filename`, `mime`, `size`, `sha256`, `visibility (private|company|public)`, `scan_status (pending|clean|infected|error)`, `uploaded_by FK→User NULL`, `expires_at NULL`, `deleted_at NULL`
`CHECK (owner_candidate IS NULL OR owner_company IS NULL)`
Ownership is two nullable foreign keys rather than a generic relation, so quota accounting, purge-on-erasure and access policy are all plain joins. Domain models point *at* `Attachment`; `Attachment` does not point back polymorphically.

**AttachmentAccess** — `FK→Attachment`, `actor FK→User NULL`, `FK→Company NULL`, `purpose`, `ip`, `created_at` · monthly partitions
The source for **"who downloaded your CV"** (FR-2.8). *(v1.18: "who viewed your profile" is `ProfileView` in §5.4 — see §10.6. A profile is not an attachment, and conflating the two meant a candidate with no CV file uploaded would have had no record of being read at all.)*

### 5.14 `billing` and `platform`

**Plan** — `code U`, `audience (candidate|company)`, `price`, `currency`, `interval`, `limits JSONB`
**Subscription** — `FK→Company NULL`, `FK→CandidateProfile NULL`, `FK→Plan`, `pending_plan FK→Plan NULL`, `state`, `current_period_end`, `provider`, `provider_ref`, `cancel_at NULL`, `CHECK (num_nonnulls(company, candidate) = 1)` · `pending_plan` holds a move to Free scheduled for the period end (D-08)

**Plan changes (D-08).** A valid Free → Pro gives Pro at once, without waiting for payment — bank transfers are settled by hand. It starts a new period on the day of the change and raises that period's full invoice. Pro → Free is scheduled for the end of the current period: Pro works until then, the date is shown on the plan screen, and the Pro invoice is still owed; the renewal that ends the period applies it (a `past_due` subscription, which is not renewed, has it applied on the same date). Choosing Pro again before then withdraws the move. An owner with an overdue unpaid invoice — on any of their subscriptions, ended ones included — cannot subscribe or move to a paid plan until it is paid, and is told which invoice. There is no proration and no refund.
**Invoice**, **Payment**, **Coupon**, **UsageCounter** (`owner`, `metric`, `period`, `value`)

**Built in P18.** Six details the implementation settled, none of them a change of design:

  * **`Plan.limits` carries `schema_version` and is read by exactly one function** (`billing.services.entitlements`), against a declared vocabulary in `billing/limits.py` (ADR-A54, ADR-A27). A plan naming an undeclared key is refused when it is *written*, because a typo in a JSONB document is otherwise indistinguishable from "unlimited" — the failure mode where a paid feature is quietly free for a year.
  * **`is_default` per audience, unique by partial index.** Every account therefore has a plan, and the enforcement path has no "unsubscribed" branch. `is_quoted` marks a plan whose price is negotiated ("talk to us"), so no checkout is offered for a price nobody has agreed and the renewal cron never invents an amount for one.
  * **A `past_due` subscription still confers its plan.** Withdrawal happens on a schedule (`expire_overdue`, fourteen days), because cutting somebody off the hour a card fails loses the customer *and* the payment. A cancellation is likewise a date, not a deletion: somebody who cancels on the 2nd keeps what they paid for until the 30th.
  * **One live subscription per owner, enforced by two partial unique constraints**, so "which plan am I on" cannot have two answers.
  * **`Payment` is unique on (provider, provider_ref)**, which is what makes a replayed gateway callback — the normal behaviour of every payment gateway — a no-op rather than a second charge. `Invoice` is idempotent per (subscription, period_start) for the same reason.
  * **`UsageCounter` meters only counted limits.** A boolean entitlement is answered from the plan and counts nothing. Counters are consumed *after* the work succeeds: a candidate whose analysis died on a provider outage has not used their monthly analysis.

**Where limits are enforced.** `resume_analyses` in `resumes.services.analysis.analyse`; `pro_templates` in `resumes.services.documents` (both `create_document` and `set_template`, since the builder posts a slug); `export_formats` in `resumes.services.renders.export` and deliberately **not** in `render_version`, because the render `submit_application` freezes is the platform's own evidentiary record (ADR-A8) rather than a download somebody bought; `ai_cost_monthly` supplies the ceiling `ai.services.quotas` already applied, falling back to `config/ai.py` when a plan is silent so an AI run never depends on the billing catalogue being loaded (ADR-A28).

**FeatureFlag** — `key U`, `is_enabled`, `rollout_percent`, `audience JSONB`, `expires_on`, `updated_by` · `expires_on` is mandatory; a flag that outlives its feature is a permanent dead branch, and a report of expired flags runs weekly
**SystemSetting** — `key U`, `value JSONB`
**EmailTemplate** — `key`, `locale`, `version`, `subject`, `body`, `U(key, locale, version)`
**LegalDocument** — `slug`, `version`, `body`, `effective_from` · acceptance is recorded in `Consent` (§5.2), not in a second table
**SupportTicket**, **AbuseReport** (`reporter`, `target_ct/id`, `reason`, `state`, `resolution`), **ModerationAction**

**AnalyticsEvent** — `actor FK→User NULL`, `anon_id`, `name`, `props JSONB`, `ts` · monthly partitions, pruned
Rollups: **JobDailyStat**, **CompanyDailyStat**, **FunnelSnapshot**, **AICostDaily**. There is no per-candidate daily rollup — candidate data volumes are small enough to compute live.

**SteeringSnapshot** *(P17)* — `day`, `metric`, `value`, `sample_size`, `detail JSONB`, `U(day, metric)`
The four measurements of §16.3, stored rather than computed live: a chart of "did the matcher get better" needs the old numbers as they were, not as today's scorer would compute them. `sample_size` travels with every value and `is_meaningful` is false below thirty, because a number shown without its sample invites somebody to act on eleven applications.

### 5.15 The four event-shaped tables

Without a stated rule, writes land in the wrong one within a month:

| Table | Lifetime | Purpose | Lossy? |
|---|---|---|---|
| `OutboxEvent` | Minutes, then **deleted** | Reliable delivery of side effects | No |
| `ApplicationEvent` | Life of the application | User-visible domain timeline | No |
| `AuditLog` | Years, append-only, immutable | Compliance and forensics | No |
| `AnalyticsEvent` | Partitioned and pruned | Product measurement | Yes, acceptably |

A state change that a user must see writes `ApplicationEvent`. A state change a regulator or investigator must see writes `AuditLog`. A side effect that must happen writes `OutboxEvent`. A fact worth counting writes `AnalyticsEvent`. Many changes write to several; none write to the wrong one by accident once the rule is stated.

### 5.16 Migration and evolution rules

1. **Expand → backfill → contract**, over three deploys. No migration both adds a `NOT NULL` column and backfills it.
2. **Every JSONB document column has a schema version** (A27) and every reader dispatches on it, with an explicit upgrade function per bump. This is what makes historical `ResumeVersion` rows renderable forever.
3. **Every vector column has `embedding_model` and `embedded_at`.** Cosine distance between two models' vectors is noise, and the failure is silent. Retrieval filters on the current model; a model change is add-column, dual-write, backfill, verify, swap, drop.
4. **`search_vector` is a Postgres generated column**, not a signal and not a trigger someone forgets to deploy.
5. **Soft-deleted rows have a purge schedule**, per §5.1.9. "Soft delete" without a purge means "grows forever" and, after an erasure request, means "we told the user we deleted it and we did not".
6. `AUTH_USER_MODEL` is already fixed and must not move.

---

## 6. Django project structure

### 6.1 The organising rule

**Domain apps own data. Portal apps own screens.**

- **Domain apps** hold models, migrations, services, selectors, policies, tasks, events, querysets and admin. No templates, no page views.
- **Portal apps** hold views, forms, urls and templates. **No models.** They import services and selectors.

`apps/employers/` is already exactly this — views and templates, zero models — so the rule describes something the codebase half-implements rather than something imposed on it. The consequence that matters: the employer portal and the candidate portal both use `applications` without either owning it, and a future API is a third consumer of the same services rather than a parallel implementation.

### 6.2 Target layout

```
config/
  settings/  base.py  dev.py  prod.py  test.py
  urls.py  wsgi.py  asgi.py  celery.py

apps/
  # -- platform / shared --------------------------------------------
  core/            base models, feature flags, navigation, marketing
                   content, templatetags ({% icon %}, ui)
  common/          DomainError hierarchy, state machine, pagination,
                   hashing, idempotency  (named modules only, never a
                   junk drawer: anything domain-specific goes home)
  audit/           AuditLog, @audited, impersonation
  taxonomy/        skills, aliases, relations, categories, industries,
                   locations, languages, FX rates + importers
  files/           Attachment, storage backends, scanning, signed access

  # -- domain (models + services; no templates) ----------------------
  accounts/        User, EmailAddress, StaffRole, MFA, sessions, Consent
  candidates/      CandidateProfile + children + CandidateSkill
  resumes/         uploads, parses, documents, drafts, snapshots,
                   versions, templates, renders, analyses, suggestions
  companies/       Company, CompanyMembership, verification, invites
  jobs/            Job, JobSkill, screening questions, views, alerts
  applications/    Application, events, stages, notes, tags, offers, pools
  messaging/       conversations, messages, reports
  interviews/      templates, pools, slots, sessions, turns, evaluations
  matching/        scorer, feature extraction, retrieval, explanations
  ai/              adapters, router, prompts, runs, quotas
  notifications/   outbox, fan-out, notifications, preferences, email,
                   webhooks, digests
  billing/         plans, subscriptions, invoices, entitlements, usage
  analytics/       capture, rollups, report queries

  # -- portals (views + templates; no models) ------------------------
  portals/
    site/          marketing, pricing, legal, design system
    candidate/     dashboard, profile, resume, jobs, applications,
                   messages, interviews, notifications, settings
    employer/      everything under /employer/<company_slug>/
    staff/         verification queue, moderation queue, AI run inspector
```

### 6.3 How the codebase gets there (A30)

**No big-bang restructure.** Refactoring eight working apps before a single feature exists is risk with no reward, and it makes every subsequent bug ambiguous — restructure error, or new code?

Domain apps are created **alongside** the existing ones. Each phase then does one portal at a time: point its views at the new services, delete its `samples.py` and `state.py`, move the views under `portals/`. Eight small reversible steps, each ending with a working application, instead of one large irreversible one.

| Existing | Destination | Moves in |
|---|---|---|
| `apps/accounts` | `apps/accounts` | P1 (authorization removed from `role`) |
| `apps/core` | `apps/core` + `apps/common` | P0 |
| `apps/preferences` | `apps/accounts` models + `portals/candidate/settings` | P3 |
| `apps/dashboard` | `portals/candidate` | P3 |
| `apps/resume` | `apps/resumes` + `portals/candidate/resume` | P4–P6 |
| `apps/jobs` | `apps/jobs` (domain) + `portals/candidate/job_views.py` | P7 — done |
| `apps/employers` | `apps/companies` + `apps/applications` + `portals/employer` | P1 shell, P8 content — done |
| `apps/notifications` | `apps/notifications` + `portals/candidate/notifications` | P10 |

The `templates/` tree does not move. Template paths stay as they are; only view imports change.

### 6.4 Standard files in a domain app

```
apps/<domain>/
  models/            one module per aggregate above ~300 lines
  migrations/
  services/          writes, one module per aggregate
  selectors.py       reads, all scoped
  policies.py        can_x(actor, obj) and require_x() raising
  querysets.py       for_actor / for_company / published / active
  tasks.py           Celery entry points (thin)
  events.py          event types + payload schemas + versions
  constants.py       TextChoices
  admin.py
  management/commands/    every scheduled job, runnable by hand
  tests/  test_services.py  test_policies.py  test_selectors.py
          test_events.py    factories.py
```

### 6.5 Rules that keep it clean after five years

1. **Signals are banned for business logic (A29).** `post_save` and `pre_save` are permitted only for cache invalidation and denormalised-field maintenance. Business reactions go through the outbox, where they are visible, retryable, ordered and testable. This one rule prevents the most common way a Django codebase becomes unmaintainable — a mutation triggering four invisible cascades nobody can find.
2. **Celery tasks take primitive arguments**, never model instances; are idempotent; declare their queue explicitly; set `max_retries` and a dead-letter path; and contain no logic beyond loading arguments and calling a service.
3. **Every scheduled job is a management command.** Beat is a scheduler, never a hiding place for logic that cannot be run by hand during an incident.
4. **No repository layer.** A repository over the Django ORM buys database portability that will never be exercised, at the cost of an indirection on every query and the loss of `select_related`/`prefetch_related` ergonomics. `selectors.py` is the pragmatic form of the same idea, and is the form Django code reviewers will recognise.
5. **`apps/common/` is constrained to named modules.** Anything domain-specific belongs to its domain, checked in review.

---

## 7. Service layer

### 7.1 Layer contract

| Layer | May do | Must never do |
|---|---|---|
| **Model** | Fields, constraints, `__str__`, trivial derived properties, querysets | Call services, send mail, hit the network, orchestrate |
| **Selector** | Scoped reads, `select_related` / `prefetch_related` | Write |
| **Service** | Validate, mutate, transact, emit events, enqueue tasks | Touch `request`, render, know about HTTP |
| **Policy** | Answer `can_x(actor, obj)` | Mutate |
| **Task** | Load arguments, call one service, handle retries | Contain business logic |
| **View** | Parse input, call policy plus service or selector, choose a template | Query the ORM, hold business rules, orchestrate |
| **Template** | Render what it was given | `Model.objects`, `.filter()`, computation |

### 7.2 Conventions

1. Functions, not classes. Keyword-only arguments — a positional swap between two IDs is silent corruption. Actor first.
2. Return the domain object. Not a dict, not an `HttpResponse`.
3. Raise `DomainError` subclasses — `NotAllowed`, `InvalidState`, `Conflict`, `QuotaExceeded`, `StaleWrite`. Views map them to status codes and messages in one place.
4. `@transaction.atomic` on the service, never the view.
5. Side effects are **never** executed inline. They become `OutboxEvent` rows in the same transaction.
6. **Concurrency is explicit.** Services that mutate a contended aggregate take `select_for_update()` on its root; services on optimistically locked models compare `row_version` and raise `StaleWrite`. An `IntegrityError` reaching a template is a bug.
7. **Bulk operations have their own services.** A bulk stage move is one transaction and one summarising event, not 200 of each.

### 7.3 The canonical service

```python
@transaction.atomic
def submit_application(*, actor, job, resume_version, cover_letter=None, answers):
    policies.require_can_apply(actor, job)
    profile = actor.candidate_profile
    if resume_version.document.candidate_id != profile.id:
        raise NotAllowed("That CV does not belong to you.")
    if not job.is_open:
        raise InvalidState("This role is no longer accepting applications.")
    quotas.require_application_budget(profile)  # FR-6.13

    render = resume_service.ensure_render(  # freeze the artefact (A8)
        version=resume_version,
        template=resume_version.document.template,
        fmt="pdf",
    )
    match = matching.score(profile, job)  # computed, not looked up (A21)

    application = Application.objects.create(
        job=job,
        candidate=profile,  # A22
        resume_version=resume_version,
        submitted_render_sha256=render.sha256,
        cover_letter=cover_letter,
        stage=pipeline.default_stage_for(job),  # A23: stage is the only state
        answers=answers,
        job_snapshot=job.as_snapshot(),
        job_snapshot_schema_version=JOB_SNAPSHOT_SCHEMA,
        match_snapshot=match.as_snapshot(),
    )
    ApplicationEvent.objects.create(
        application=application,
        actor=actor,
        actor_kind="candidate",
        type="application.submitted",
        to_stage=application.stage,
        visible_to_candidate=True,
    )
    visibility.grant(
        candidate=profile, company=job.company, basis="application", application=application
    )
    events.emit("application.submitted", application)  # outbox row, same transaction
    return application
```

Everything downstream — notifying the recruiter, incrementing the counter, re-scoring, opening the conversation channel — subscribes to `application.submitted`. The service does not know any of them exist, and a new reaction is a new subscriber rather than an edit here.

### 7.4 Service catalogue

| Module | Responsibility |
|---|---|
| `accounts/services/` | registration, verification, password, MFA, sessions, consent, deletion, export |
| `candidates/services/profile.py` | profile writes, completeness, skill sync, embedding refresh |
| `resumes/services/upload.py` | receive, scan, dedupe, claim a guest upload, dispatch parse |
| `resumes/services/parse.py` | parser orchestration, import proposals |
| `resumes/services/document.py` | create, duplicate, archive, apply template, restore a version into the draft |
| `resumes/services/draft.py` | autosave, snapshot ring, undo/redo, suggestion accept/reject |
| `resumes/services/version.py` | finalize a version — the only writer of `ResumeVersion` |
| `resumes/services/render.py` | PDF/DOCX generation and cache |
| `resumes/services/analysis.py` | deterministic checks then AI enrichment |
| `resumes/services/reconcile.py` | profile↔document drift and reconciliation (§5.5.3) |
| `companies/services/` | registration, membership, invites, ownership transfer, verification |
| `jobs/services/` | lifecycle, skills, cloning, expiry, view capture, alert batches |
| `applications/services/` | apply, withdraw, stage move, bulk move, note, tag, rate, offer, close, reveal contact |
| `messaging/services/` | open conversation, send, read, mute, block, report |
| `interviews/services/` | availability, hold, book, reschedule, cancel, start, next question, answer, evaluate, conclude |
| `matching/services/` | retrieve, score, explain, invalidate, alert batch |
| `ai/services/run.py` | the single entry point `ai.run(task_key, …)` |
| `notifications/services/` | emit, fan out, deliver, preferences, digests, webhooks |
| `billing/services/` | subscribe, change plan, enforce entitlement, record usage |
| `files/services/` | store, scan, sign, log access, purge |
| `analytics/services/` | capture, rollup, report |

### 7.5 State machines

Five machines carry most of the business risk. Each lives in one module with an explicit transition table `(from, to, required_permission, side_effects)`, is the only writer of its field, raises on an illegal transition, and writes an event on every legal one.

- **Job** — `draft → published → paused → published → closed → archived`
- **Application** — the stage graph, with terminal stages carrying `closed_reason`
- **CompanyVerification** — `not_submitted → pending → more_info → pending → verified | rejected`, and `rejected → pending` as a new attempt no earlier than thirty days after the decision (D-02)
- **InterviewSession** — `scheduled → in_progress → completed | abandoned | cancelled | no_show`
- **Offer** — `draft → sent → accepted | declined | withdrawn | expired`

---

## 8. Interfaces

### 8.1 Server-rendered views and HTMX fragments — the product (A3)

A page view returns a document; a command endpoint (`.../act/`) performs one action and returns the fragment that changed. The codebase already uses this consistently (`resume:builder_act`, `jobs:act`, `employers:applicant_act`, `settings:*_act`), and it is good design: one URL per surface, with the action in the payload.

Conventions, now formalised:

- Command endpoints are POST-only and CSRF-protected, and return `HX-Trigger` headers for cross-fragment updates (unread badge, toast) rather than making the client re-fetch.
- Each returns the **smallest** fragment that changed.
- Validation errors render an inline error fragment with HTTP 200 and `HX-Reswap`, never a bare 400 that HTMX discards.
- **An expired session returns `HX-Redirect` to the login page.** Rendering a login page into a `<div>` is what "the app broke" looks like to a user.
- Long-lived builder pages refresh the CSRF token on a timer, so autosave does not begin failing silently after an hour.
- Endpoints that create a record accept an `Idempotency-Key`, stored per §5.2, retained 24 hours.

### 8.2 Outbound webhooks — the only external interface in v1 (A17)

Events: `application.submitted`, `application.stage_changed`, `interview.completed`, `job.published`, `offer.accepted`.

Signed with HMAC-SHA256 over body and timestamp; exponential backoff; dead-lettered after 24 hours; replayable from the staff UI; subject to the SSRF allow-list (§15.4). Delivery state is in `WebhookDelivery` (§5.12).

### 8.3 The JSON API — specified, deferred

`/api/v1/` is designed but not built (A17). When a real integrator exists, it wraps the same services: cursor pagination, `Idempotency-Key` on POST, per-token rate limits, path versioning, `Sunset` headers on deprecation. Planned surface: public job search, `me` and its resumes and applications, company jobs and applications for ATS sync, webhook subscription management. Deliberately excluded, then and now: a public candidate-search API, which is a privacy hazard and the first thing a scraper would reach for.

---

## 9. AI architecture

### 9.1 Shape

```
   caller (a service)
        |  ai.run("resume.analyze", input=..., actor=...)
        v
  +-----------------------------------------------+
  | ai.services.run                               |
  |  1 resolve task config (code) + prompt version|
  |  2 build input, compute input_hash            |
  |  3 cache lookup (task, prompt_version, hash)  |
  |  4 quota + cost ceiling + kill switch         |
  |  5 select model, with a fallback chain        |
  |  6 dispatch to the adapter                    |
  |  7 validate output against the JSON schema    |
  |  8 repair-or-retry, by error class            |
  |  9 persist AIRun (tokens, cost, latency)      |
  +--------------------+--------------------------+
                       v
  adapters: openai · anthropic · gemini · openrouter · ollama
            each implements complete(), embed(), stream()
```

### 9.2 The adapter is the only thing a new provider implements

```python
class AIAdapter(Protocol):
    def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        schema: dict | None,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> Completion: ...
    def embed(self, *, model: str, texts: list[str]) -> list[list[float]]: ...
    def stream(self, **kwargs) -> Iterator[Chunk]: ...
```

`Completion` normalises `text`, `parsed`, `tokens_in`, `tokens_out`, `finish_reason` and `raw`. Provider-specific concerns — tool calling, system-prompt placement, JSON mode — stay inside the adapter. Adding a provider is one file and a settings entry (A28).

### 9.3 Rules

1. **Structured output only.** Every task declares a JSON schema; prose is a field inside it. Templates never parse model output.
2. **Prompts are code (A20).** Files in version control, loaded and hashed into `PromptTemplate` at deploy. Reviewed, diffed, tested and rolled back with the code that depends on them. `AIRun.prompt_version` preserves the audit property; the 2 a.m. admin-editing surface is gone.
3. **Nothing runs on the request path** (NFR-3). All AI is a Celery task on the `ai` queue. The existing `resume:processing` screen shows named stages, not an indefinite spinner.
4. **Failure is a first-class state.** `AIRun.status` distinguishes `schema_error`, `provider_error`, `timeout`, `quota_exceeded` and `refused`. The interface names the failure and offers a retry.
5. **Retries are classified.** Retryable: timeout, 5xx, rate limit. Terminal: context length exceeded, schema-unfixable after one repair attempt, provider refusal. Retrying a terminal error burns money and time for a guaranteed second failure.
6. **Cost is enforced before dispatch**, by per-user monthly quota, per-run ceiling, and per-task model tier. A global kill switch disables all AI dispatch without a deploy, degrading features to their deterministic parts rather than to errors.
7. **PII minimisation.** Each task declares whether it may see contact details; where it may not, they are redacted before dispatch.
8. **Training consent.** No provider is configured to train on submitted data, and `Consent(purpose='ai_training')` gates any future use of user content for evaluation or fine-tuning.
9. **Every AI artefact is labelled in the interface** and links to its run.
10. **An eval harness is a Phase 11 deliverable, not a follow-up.** A golden set of ~50 resumes with expected finding categories runs in CI against a cheap model on every prompt change. Versioned prompts give traceability of a regression; only the eval set detects one.

### 9.4 Task registry

*Implemented keys are the ones in `config/ai.py`. Where this table and that file disagreed, the file won and this table was corrected (v1.15).*

| `task_key` | Input | Output | Tier | Built |
|---|---|---|---|---|
| `resume.parse` | raw text + layout | structured resume JSON | cheap | No — P4's reader is deterministic and sufficient |
| `resume.analysis` | redacted version content + target role | findings, verdict, headline | capable | P12 |
| `resume.bullet_rewrite` | one entry's bullets + role + the problem | options + rationale | cheap | P12 |
| `resume.summary` | redacted version content + target role | summary options | cheap | P12 |
| `resume.section_draft` | conversation turn + what is already on the CV | reply, bullets, one question | capable | P14 *(not streamed — §9.5)* |
| `cover_letter.draft` | skills and roles as data + job + tone + the candidate's notes | paragraphs, what it used, what it could not claim | capable | P14 |
| `match.explain` | score breakdown | notes + advice | cheap | P13 |
| `skills.extract` | resume text | skill mentions + evidence spans | cheap | P11 (prompt); no caller yet |
| `skills.gap_plan` | missing skills + target role | ordered learning steps | mid | P13 |
| `interview.plan` | job + competencies | question plan | mid | P15 |
| `interview.followup` | turn history | next question or stop | mid | P15 |
| `interview.evaluate` | turn + rubric | score + rationale | strong | P15 |
| `candidate.rank_explain` | application + job | ranking rationale | mid | P13 |
| `embed.profile` / `embed.job` | text | vector | embedding | P13 |

### 9.5 Streaming, and where it is not used

The assistant builder is a chat, and chat without streaming feels broken. Streaming needs SSE, and SSE needs ASGI, while the deployment (§17) runs synchronous workers.

**Decision:** v1 uses HTMX polling with named progress stages for analysis and parsing, and **one ASGI-served endpoint for the assistant builder's streaming responses**, run by a small uvicorn worker pool behind the same reverse proxy. This is a bounded exception with a stated boundary rather than a discovery made during implementation.

**Built in P14, without the exception.** The assistant answers in one response, swapped over HTMX like everything else. Streaming was the reason this section exists and it turned out not to be the reason the feature was missing: from P5 to P14 the screen stored the candidate's turns and never replied at all, and an answer that arrives whole is enormously better than silence. The ASGI exception is not withdrawn — it is what streaming will need whenever the polish is worth a second serving path — but it is no longer blocking anything.

### 9.6 Local models — an honest position

Ollama is a first-class adapter for development and for self-hosted deployments. It is **not** the production cost strategy: a useful local model needs a GPU, which the deployment budget in §17.5 does not include. Production cost control is cheap hosted models for extraction and embedding, strong models only for analysis and interview evaluation, aggressive input-hash caching, and enforced quotas.

### 9.7 Guardrails specific to hiring

- **The model never decides.** Shortlist, reject and offer are human actions with a human actor in the audit log. This is the ethical position and the regulatory one.
- **Prompt injection is a live threat**: a resume is untrusted input that a model reads and an employer trusts. Resume text is passed as delimited data with an explicit instruction that it contains no instructions; output is schema-validated; any candidate-supplied text surfacing in employer-facing output is length-capped and escaped.
- **Fairness monitoring** samples AI-influenced outcomes for periodic review (§16).

---

## 10. Matching, recommendations and search

### 10.1 Nothing is persisted except alerts (A21)

For one candidate the whole pipeline runs in well under a second. A candidate×job score table is the largest and churniest object in the schema, most of whose rows are read once or never, with write amplification on every job publication.

**Recommendations are computed on read and cached in Redis** under `(profile_id, algorithm_version, embedding_model, profile_updated_at, job_generation)`, so every staleness class invalidates by key construction rather than by a sweep.

`job_generation` is a monotonic counter incremented on `job.published`, `job.updated` and `job.closed`. Without it a newly published job cannot appear in any cached feed until the candidate happens to edit their profile — the feed is stale exactly when a relevant job is posted, which is the one moment the product exists for. `embedding_model` closes the second class: a model change would otherwise keep serving vectors from the old model out of cache. A 15-minute TTL is a backstop, not the mechanism.

The cache must also be correct when empty: every key is recomputable from PostgreSQL, so a cold or flushed cache costs latency and never correctness. The only persisted artefacts are `Application.match_snapshot` (history, required by FR-8.5) and `JobAlertMatch` (genuine batch work with a fixed recipient set — search relevance, not a match score). If measurement — not intuition — later shows the on-read path is too slow, a materialised table can be added without changing any caller.

**Built in P13, and two details worth stating because a reasonable reading would guess wrong:**

- **The profile timestamp is keyed to the microsecond**, not the second. `updated_at` records microseconds, and truncating to a whole second means an edit and a read inside the same second share a key — the candidate saves a change and the feed does not move, which is exactly the staleness this construction exists to prevent. (Found by a test during P13; the first implementation truncated.)
- **A cached feed naming a posting that has since closed is recomputed, not repaired.** Showing a candidate a job that has gone is worse than the fraction of a second recomputing costs, and a partially hydrated feed would hide which rows went missing.

`matching` owns no tables. The app is services, a scorer, an extractor and two event subscribers — which is what ADR-A21 means in the file layout, and the test asserting the app has no models is the guard on it.

### 10.2 Retrieval branches on selectivity (A24)

An HNSW index cannot honour arbitrary `WHERE` clauses. Filtering after an approximate search loses recall and can return nothing from a healthy corpus; a candidate in Pokhara with a narrow salary band is exactly the case that breaks. So the pipeline chooses its strategy from the size of the filtered set:

```
0 FILTER SET   cheap COUNT with the hard filters:
               published · not expired · workplace and remote_scope compatible
               · salary base overlap · not blocked · not already applied
1 STRATEGY
   small   (< ~5k)  exact distance over the filtered set — no ANN at all
   medium           pgvector iterative index scan with filters applied
   large            ANN top-1000, then filter, then top up if thin
2 SCORE   deterministic, explainable, in process:
             skills      50%  weighted overlap, SkillRelation for partial credit
                              (Django satisfies "Python" at 0.8, not 0)
             experience  20%  years against the required band, seniority fit
             domain      10%  category and industry adjacency
             location     8%  workplace and remote_scope compatibility
             salary       7%  annual-base range overlap  (§5.3)
             recency      5%  posting freshness × candidate activity
3 ADJUST  diversity term + per-job exposure cap  (FR-8.8)
4 EXPLAIN LLM on the top 10 only, cached by input hash
```

**Thin-result floor:** if fewer than ten results survive, the softest filter is relaxed and the results are labelled as widened. An empty state reads to a candidate as "there is nothing for you", which is both untrue and the moment they leave.

**Cold start:** a candidate with fewer than three confirmed skills is matched on everything else that is known — experience, location, arrangement, pay, freshness — labelled **provisional** on every card, with a prompt to confirm skills, which is the highest-value action available to them that day. The label is not decoration: a provisional score presented as a measurement is the kind of confident nonsense this architecture is built to avoid.

**What P13 built, against what §10.2 above specifies:**

| Step | Built | Note |
|---|---|---|
| 0 Filter set | Yes | Absolute filters (blocked, already applied, published) and soft ones (salary, workplace). The soft ones are what the thin-result floor relaxes; the absolute ones are never relaxed. |
| 1 Strategy | All three | Exact distance on a small filtered set, filtered scan ordered by distance in the database, ANN-then-filter-then-top-up above `MEDIUM_SET`. Thresholds are module constants and the tests lower them to exercise each branch rather than inflating the corpus. |
| 2 Score | Yes, all six components | Weights exactly as stated; a test asserts they sum to 100. |
| 3 Adjust | Yes | Per-company diversity penalty and a per-posting daily exposure taper, both applied to *position* and never to the score. |
| 4 Explain | Yes, top 10 | `match.explain` through the P11 runner, cached by input hash by the runner rather than by a second cache here. |

**The domain component scores industry only.** §10.2 says "category and industry adjacency", and category is not scored: a `CandidateProfile` carries no job family, and the two available ways to infer one — from what the candidate has applied to, or from what the feed has shown them — both make the feed a function of itself. Industry is read from the employers on their work history, which is a fact; an empty industry list scores **neutral rather than zero**, because "we do not know your industry" is not "you are in the wrong industry".

**Vectors affect retrieval order, never the score.** A platform with no embedding provider configured, or a row not yet embedded, produces a complete feed — the filters and the deterministic scorer do all the work. This is ADR-A6 on the matching side, and it is what makes an embedding outage a degraded feature rather than a broken one.

### 10.3 Why deterministic scoring is the core (A6)

Reproducible — same inputs, same score, forever. Auditable — the breakdown *is* the explanation the interface already renders. Testable with golden files. Tunable, because weights are configuration and are measured against outcomes (§16.3). Free at any volume. And defensible in a way "the model said 73" is not.

### 10.4 Fairness

An explicit feature allow-list; a test fails the build when the feature vector gains an unlisted field. Excluded permanently: name, photo, age, gender, marital status, address precision below city, and institution prestige. Outcome sampling for adverse-impact review begins in Phase 16, with the sampling hook present from Phase 13.

**Built as `apps/matching/features.py`.** `ALLOWED_FEATURES` maps every permitted feature to a one-line justification, and `check_features` raises on anything else — called by the scorer on every score, so the control is not a document about the code but a branch in it. The exclusion list is named separately in `NEVER_ALLOWED`, and three tests assert the properties that matter: every excluded attribute is refused, the two lists do not overlap, and the vectors the *real extractor* produces stay inside the allow-list.

Why an allow-list and not a block-list of protected attributes: a block-list has to anticipate every proxy and never does. A postcode is not "address", a school's name is not "ethnicity", a photograph is not "age" — and each carries the attribute anyway. Naming what may be used inverts the burden, so adding a feature is a decision somebody writes down in a file whose diff a reviewer reads for exactly this reason.

**Institution prestige deserves its own line**, because it is the exclusion that looks harmless. "Which university" is a proxy for class, caste and city in this market specifically; a ranking that rewards it reproduces the existing distribution of opportunity while appearing to measure merit. What is scored instead is what the qualification *is* — and even that only through experience and skills, not through the institution that issued it.

### 10.5 Keyword search

Postgres full text with an explicitly chosen text-search configuration — `simple` with trigram similarity for the mixed-script and transliterated content this market actually produces, rather than `english` stemming applied to Nepali. Filters are SQL. Ranking is a single composite expression, and **every sort ends with a deterministic tiebreaker on `(-published_at, id)`**, without which cursor pagination over a computed score drifts and users see the same job twice.

Migration path to OpenSearch, if job volume or facet latency ever demands it: Postgres remains the source of truth, the index is a projection rebuilt from it, and the selector interface does not change.

### 10.6 Talent search (employer side)

A distinct subsystem, not a reversed recommendation. Candidates are searchable only where visibility permits and a grant exists or is created by an explicit, logged opt-in. There is no application signal to rank on, so ranking is skills, experience, recency of activity and stated availability. Every profile view writes a `ProfileView` row and an audit row and is visible to the candidate (FR-2.8). Bulk export requires the `candidate.export` grant and triggers anomaly detection (§15.4).

*(v1.18 correction: this section said a profile view writes an `AttachmentAccess` row. A profile is not an attachment, and a candidate with no CV file uploaded would have had no record written at all — the case where "who viewed me" matters most, because somebody found in a search is exactly somebody nobody has a file for. `ProfileView` and `AttachmentAccess` answer different questions and the candidate is shown both.)*

**Built in P15. Five decisions worth stating, because each is a place a reasonable reading would guess differently:**

- **Discoverable and readable are separate acts.** `visibility = everyone` puts a candidate in results — that setting *is* their opt-in to being found. Opening the profile is a further act that creates the `public`-basis grant and writes the view, so INV-4 holds for the first read as well as every later one. Appearing in a list is not being read.
- **`employers` means the employers they applied to, and nothing wider.** A candidate on that setting who has not applied to this company is not discoverable here. Reading it as "employers in general" would be the platform making the candidate's decision for them.
- **Searching logs nothing per candidate; opening a profile does.** A result row is a headline and a skill list. Writing a view for every row of every search would fill the candidate's screen with noise until it told them nothing.
- **The candidate's screen groups by company and day, and never names the recruiter.** Ungrouped, an interested employer reads as surveillance. Naming the individual would expose one person's browsing to another without their knowledge, and it is not what the candidate is asking — the company is what is accountable for the read.
- **An export carries no contact details and only allow-listed fields** (`talent.EXPORT_FIELDS`). ADR-A34 reveals contact details at a stage that requires an application, and finding somebody in a search is not that stage; the allow-list means a column added later cannot leave in a spreadsheet because nobody thought about it.

**Anomaly detection reports and never blocks** (§15.4). A false positive that blocks a real recruiter during a hiring push is a support incident and a lost customer; one that raises a flag costs somebody two minutes of reading. The hard limits that *do* refuse — the export cap, the grant requirement — are business rules in `services.talent`, not detection.

---

## 11. Interviews

### 11.1 One session model, two engines in v1

`InterviewSession` is uniform across kinds; only the engine differs, and all kinds converge on `InterviewEvaluation` and `InterviewOutcome`, so the pipeline, notifications and analytics have one shape to handle. v1.1 ships **AI text** and **live (Jitsi)**. Video is Phase 19 (§21).

### 11.2 AI text interview

```
start   plan = ai.run("interview.plan", job + competencies)
        questions SAMPLED from InterviewQuestionPool per competency and rotated
loop    next_question(session) -> a planned turn, or a bounded AI follow-up
        submit_answer(session, text)  -> persist turn, enqueue evaluation
        limits: 2 follow-ups per competency, a total cap, a hard token budget
        each answer is time-boxed; paste and focus events are recorded in
        integrity_meta as employer-visible context, never as score inputs
finish  conclude(session) -> aggregate evaluations into InterviewOutcome
```

The candidate never waits for scoring. **Anti-cheat is framing, not surveillance:** a candidate can consult a model, so questions are time-boxed, follow-ups reference the candidate's own prior answers (hard to fake cold), and the output is positioned as a screening signal rather than an assessment. Pretending otherwise produces a feature whose output is worthless within months of launch.

### 11.3 Live interview

Jitsi via JaaS in v1, behind a `VideoRoomProvider` interface so self-hosting later is configuration. Rooms are private, entered with a short-lived per-participant JWT — a guessable room name is a stranger walking into an interview.

*(v1.19: the room reference is a random token and carries nothing about the job or the candidate, which is the half of that sentence P16 built. The **JWT is not minted** — signing needs a JaaS private key the deployment does not have, and a self-signed token the vendor rejects at the door would be worse than an honest absence. `join_url_for` returns the room URL; when the key exists, that one function changes. The join token the platform does issue is stored as a SHA-256 hash, because a stored credential is a leak waiting for a backup to be read.)*

Scheduling runs on `InterviewSlot` with a database exclusion constraint (§5.10): availability is published, a candidate takes a hold, confirmation books it, and overlap is rejected by Postgres rather than by a check-then-insert race. ICS invites, reminders at 24 hours and 1 hour, and full **reschedule, cancel and no-show** flows on both sides — which is the majority of real scheduling traffic and was absent from v1.0.

Scorecards are per interviewer, per competency, and **hidden from other interviewers until submitted**. Anchoring is the most studied failure in interview panels and this is a two-line rule that prevents it.

### 11.4 Reports, history and permissions

Every session yields one outcome shape. An interviewer's access to the application runs from scheduling until 30 days after the interview and is revoked automatically. Practice results are always the candidate's own. Employer evaluations are shared at the employer's discretion, defaulting to a summary on rejection — because "we'll be in touch" is the single most-disliked thing about applying anywhere, and the data to do better already exists.

Interview-to-hire correlation is instrumented from the first interview (§16.3). Without it there is no way to know whether any of this predicts anything.

---

## 12. Messaging

A first-class domain (A26), because the settings screen and the notification feed already promise it, and because hiring conversations that leave the platform take audit, consent, retention and analytics with them.

**Rules:**
- A company may open a conversation only where a `CandidateVisibilityGrant` exists. There is no cold outreach to candidates who have not applied or opted in.
- Conversations optionally anchor to a job or an application and appear in both timelines.
- Candidates can mute, block (which also writes `BlockedCompany`) and report.
- Rate limits per company per candidate, and a global per-company daily cap, so messaging cannot become bulk outreach.
- Attachments follow §14 in full — scanned, private, signed, logged.
- Messages are exported with the candidate's data and retained on the application's schedule.

`message.sent` maps to the existing `employer_messages` preference group, so a muted channel is genuinely silent.

---

## 13. Events and notifications

### 13.1 Event governance

Events are past-tense facts. The contract, which v1.0 lacked:

1. **Naming** is `noun.verb_past`, lowercase, dotted.
2. **Every event declares a payload schema and a `schema_version`.** A registry test fails the build if an event is emitted with no declared schema. Events delivered to a person also declare `preference_group`, and `register()` refuses a group nobody can toggle — an event mapped to a nonexistent preference is a channel nobody can mute.
3. **Payloads carry identifiers plus the minimal denormalised fields a consumer needs** — never the whole row (which bloats and leaks), never bare IDs (which race, because the row may change before the consumer runs).
4. **No ordering guarantee.** Consumers are written to be commutative and idempotent, keyed on `(event_id, consumer)`.
5. **Effects are not events.** Notification creation and email queueing are *outputs* of the notification pipeline. Emitting them back into the bus invites feedback loops and makes "why did this send twice" unanswerable. They are rows and metrics, not events.

### 13.2 Event catalogue

**Identity and account** — `user.registered` · `account.verified` · `consent.changed` · `candidate.erasure_completed`

**Profile and resume** — `profile.updated` · `skills.changed` · `resume.uploaded` · `resume.claimed` · `resume.parsed` · `resume.version_created` · `resume.analysis_completed`

**Jobs** — `job.published` · `job.updated` · `job.closing_soon` · `job.closed`

**Applications** — `application.submitted` · `application.viewed` · `application.stage_changed` · `application.assigned` · `application.note_added` · `application.contact_revealed` · `application.withdrawn` · `application.closed`

**Offers** — `offer.created` · `offer.sent` · `offer.accepted` · `offer.declined` · `offer.expired`

**Interviews** — `interview.scheduled` · `interview.rescheduled` · `interview.cancelled` · `interview.reminder` · `interview.completed` · `interview.no_show`

**Messaging** — `message.sent`

**Company** — `company.verification_state_changed` · `company.verified` · `company.member_joined` · `company.member_removed` · `company.suspended`

**Platform** — `match.batch_ready` · `ai.run_completed` · `file.scan_failed` · `subscription.limit_reached` · ~~`system.announcement`~~ *(deferred with announcements, D-10: not registered, not emitted, no subscriber)*

Four of these deserve a note, because their absence in v1.0 was a functional gap rather than a stylistic one: **`resume.version_created`** drives re-embedding, drift detection and match invalidation, without which an edited CV never reaches matching; **`user.registered`** is distinct from verification and is what onboarding and activation analytics subscribe to; **`offer.created`** is distinct from `offer.sent` and is where an approval step will attach; and **`offer.accepted` / `offer.declined`** are separate because every consumer branches on them anyway.

### 13.3 Subscribers

| Event | Reacting services |
|---|---|
| `user.registered` | notifications (welcome), analytics, `candidates` (create profile) |
| `resume.claimed` | resumes (dispatch parse — never before this point, A32) |
| `resume.parsed` | resumes (import proposals), candidates (skill proposals), analytics |
| `resume.version_created` | resumes (reconcile), candidates (re-embed), matching (invalidate cache), notifications |
| `resume.analysis_completed` | notifications, analytics |
| `profile.updated` / `skills.changed` | candidates (completeness, embedding), matching (invalidate), resumes (reconcile) |
| `job.published` | matching (alert batch), notifications (fan-out to saved searches), analytics, webhooks |
| `job.closing_soon` | notifications (saved-job holders) |
| `application.submitted` | notifications (recruiter), jobs (counter), messaging (channel eligible), analytics, webhooks |
| `application.stage_changed` | notifications (candidate, if the stage is candidate-visible), interviews (round trigger), applications (contact reveal if the stage warrants), analytics, webhooks |
| `interview.scheduled` | notifications (both sides, ICS), interviews (reminder jobs), analytics |
| `interview.completed` | applications (stage suggestion), notifications, analytics, webhooks |
| `offer.accepted` | applications (stage → hired), jobs (close if filled), notifications, analytics, webhooks |
| `message.sent` | notifications (respecting `employer_messages`), analytics |
| `company.verified` | notifications, jobs (badge), analytics |
| `candidate.erasure_completed` | files (purge), matching (invalidate), analytics (anonymise) |

### 13.4 Delivery — two-stage fan-out (A25)

```
service ── state change + OutboxEvent in ONE transaction ──▶ postgres
                                                               │
outbox worker (2s, FOR UPDATE SKIP LOCKED) ────────────────────┘
   │  resolves the recipient set as a QUERY, never a list
   └──▶ FanoutJob(event, cursor_start, cursor_end, chunk=500)
             └──▶ chunk workers, independently retryable
                    ├─ NotificationDelivery ledger, U(event, recipient, channel)
                    ├─ in-app  → Notification (dedupe_key collapses bursts)
                    ├─ email   → suppression check → EmailMessage → provider
                    └─ webhook → WebhookDelivery with backoff
```

A `job.published` event in a popular category can match 50,000 candidates. Single-pass fan-out would be one transaction writing 100,000 rows, holding locks, blocking the outbox behind it and restarting from scratch on any failure. Two-stage fan-out makes each chunk small, independently retryable and — through the ledger's unique constraint — physically unable to double-send.

Guarantees: at-least-once delivery, exactly-once user-visible effect, nothing sent for a rolled-back transaction, and a provider outage that delays rather than loses.

### 13.5 Notification policy

Six preference groups × two channels, exactly as `apps/preferences/state.py` already defines, and **every event maps to exactly one group** — enforced by the registry test, so no event can exist that the settings screen cannot describe. Plus: per-user send caps and quiet hours; `dedupe_key` collapsing bursts; a weekly digest with role-appropriate wording; and per-category one-click unsubscribe, never a global one that also kills interview reminders.

Email runs behind an `EmailProvider` interface (SMTP now, a transactional provider later), with templates in the database, versioned and localised, and a filesystem fallback. `EmailSuppression` is consulted before every send.

---

## 14. Files and storage

### 14.1 Three visibility classes

| Class | Examples | Serving |
|---|---|---|
| `public` | Company logos, template previews | CDN, long cache, hashed names |
| `company` | Verification documents, offer letters | Signed URL, 5-minute TTL, membership-checked |
| `private` | Resumes, renders, certificates, message attachments | Signed URL, 5-minute TTL, single-use token, access logged |

### 14.2 Rules

1. **`MEDIA_URL` never serves a resume.** Private files are fetched through `/files/<public_id>/`, which runs the policy check, writes `AttachmentAccess`, and issues a signed storage redirect. The static server must not be able to see the private bucket. A guessable `/media/resumes/` path indexed by a search engine is the most common catastrophic failure in Django-based ATS products.
2. **User-supplied documents are served `Content-Disposition: attachment`** and are never rendered inline on an application origin. A PDF rendered inline is script execution in your origin.
3. **Storage is abstract from day one.** `FileSystemStorage` locally, S3-compatible object storage in production (Cloudflare R2 or Backblaze B2 — both far cheaper than S3 on egress). No code references a filesystem path.
4. **Upload validation:** magic-byte type detection rather than extension; filename sanitised, never echoed into a path; size and page caps; ClamAV scan before any processing; PDF handling with JavaScript execution disabled; zip-bomb and nested-archive guards.
5. **A failed scan quarantines** the attachment, emits `file.scan_failed`, notifies the uploader, and blocks every downstream task.
6. **Content-addressed keys** (`sha256` prefix) give deduplication across users and make renders cacheable.
7. **Lifecycle:** originals live while the account does; renders expire after 90 days and regenerate on demand; unclaimed guest uploads are swept at 24 hours; erasure runs a purge that verifies removal from the bucket, not merely the row.

---

## 15. Security and privacy

### 15.1 Authentication

Session auth with Argon2 hashing. Email verification before applying, posting, messaging or any AI processing. TOTP 2FA available to everyone and **mandatory for company owners, company admins and platform staff**, with a 14-day grace from role grant, recovery codes issued at enrolment, and a support-verified reset path that is itself audited. Login throttling per IP and per account with progressive delay and a lockout notice. Session fixation prevented on privilege change; "sign out everywhere" rotates a per-user session salt. All tokens — reset, invite, magic link, file access, interview join — are stored hashed, single-use, short-lived and compared in constant time.

### 15.2 Authorization

The three layers of §2.5, plus:

- `request.company` comes from the URL and is re-validated against an active membership on every request (A18). Never from a form field, a query parameter or the session.
- A CI check fails the build on unscoped querysets inside portal packages.
- Every policy rule has a passing negative test; the role×capability matrix is generated from code (§2.4).
- Staff impersonation carries a reason, expires in 60 minutes, cannot target another staff account, and stamps `on_behalf_of` on every resulting action.
- **`is_staff` is not access to data (D-01).** It admits a person to the Django admin and nothing else. It never opens a private CV, message attachment, verification document or other private file: `files.policies` has no staff shortcut, and staff read files only through StaffRole-gated screens that log each read (the verification review writes the file's access log and an audit row). No admin registration carries a file field or a download link, and the attachment admin cannot change or delete a row.
- Django admin is **read-mostly**: destructive operations are removed from admin and exposed only as audited service calls; admin access requires MFA and IP allow-listing. `ModelAdmin` has no object-level permission story worth trusting at this data sensitivity.

### 15.3 Data classification and retention

| Class | Data | Controls | Retention |
|---|---|---|---|
| **Restricted** | Resume files and text, phone, address, verification documents, interview transcripts, message bodies | Encrypted at rest, private storage, signed access, access logged, minimised before AI dispatch | Life of account; transcripts 12 months |
| **Confidential** | Applications, internal notes, ratings, salary expectations, match snapshots | Tenant-scoped, never cross-company, export gated by grant key | 24 months after closure |
| **Internal** | Non-public profiles, company drafts | Grant-based access | Life of account |
| **Public** | Published jobs, public company pages, opted-in public profiles | Cacheable, indexable | Indefinite |

Field-level encryption for phone numbers and MFA secrets. Managed encryption at rest for the database. TLS on every hop.

### 15.4 Application security baseline

**Content Security Policy (A31).** Enforced, not report-only, and since P19 with no `unsafe-eval`. The shipped policy is:

```
default-src 'self'; script-src 'self' 'nonce-<per-request>';
style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self';
connect-src 'self'; form-action 'self'; frame-ancestors 'none';
object-src 'none'; base-uri 'self'
```

`script-src` has neither `'unsafe-inline'` nor `'unsafe-eval'`: the inline theme script carries the per-request nonce, and the app vendors the **`@alpinejs/csp` build**, which parses directive expressions into an AST instead of handing them to `new Function`. The obligation that leaves is on templates — an Alpine expression may contain property access, member access, calls, ternaries, comparisons, logical operators, assignments and `++`, and anything statement-shaped (a semicolon, an `if`, an arrow function, a regular expression) must be a component method in `static/js/app.js`. Two expressions needed moving when the swap was made; v1.0's estimate of "75 across 22 files" described a template tree that had since been rewritten.

`style-src` keeps `'unsafe-inline'` for Alpine's `x-bind:style`.

**Everything the pages load is served from this origin.** Fonts and marketing photographs were loaded from Google Fonts and the Unsplash CDN, and the P19 audit found both **blocked in production by this policy** — pages fell back to system fonts and rendered no photography at all. Both are self-hosted now, which is what §17.5's "no external CDN" position implied all along.

**Rate limiting (A41)** is a fixed-window mechanism over the cache, with two dimensions (identity and IP) on every sensitive rule so an attacker cannot lock a victim out by targeting their email alone. The failure policy is per rule and is the decision that matters: authentication-shaped limits **fail closed** when the cache is unavailable, because refusing a login attempt is safe; browse-shaped limits **fail open**, because taking search down over a missing cache is self-inflicted damage. Domain limits — applications per day, messages per company per candidate — are business rules enforced in services, not middleware.

Also: `Referrer-Policy: strict-origin-when-cross-origin`; `Permissions-Policy` denying camera and microphone except on interview routes; `X-Content-Type-Options: nosniff`; rate limits on authentication, upload, AI, search, messaging and webhook endpoints; SSRF allow-listing on every user-supplied URL the server fetches (link checking and webhooks) with no private ranges and no redirects into them; idempotency keys on creating POSTs; `pip-audit` and dependency scanning in CI; secrets from the environment only.

**Anomaly detection** on bulk candidate export, high-volume profile viewing and unusual message volume — which is what a scraper holding a paid employer account looks like from the inside.

### 15.5 Privacy and data rights

- **Consent** is one table with purposes and policy versions (§5.2), so an audit answers what was agreed, when, and under which text, from one place.
- **Access** — data export in JSON plus original files. **Rectification** — profile editing. **Erasure** — §19.4. **Portability** — the same export.
- **Automated processing transparency** — where a score influences an outcome the candidate can see that it exists and what it measured, which is why the breakdown is snapshotted onto the application.
- **A stated limit, honestly:** a resume already delivered to an employer cannot be recalled by erasure. The privacy policy says so plainly rather than implying otherwise.
- **The audit log is append-only by permission**, not by convention — the application database role holds `INSERT` and `SELECT` only, so a compromised admin account cannot rewrite history.

---

## 16. Analytics

### 16.1 Capture and aggregation

One append-only `AnalyticsEvent` table, monthly partitions, written asynchronously from the outbox — never inline. Domain events are the primary source; interface-only events (search performed, filter used, template previewed) are explicit calls.

Nightly rollups into narrow tables that dashboards read directly: `JobDailyStat`, `CompanyDailyStat`, `FunnelSnapshot`, `AICostDaily`. **Dashboards never aggregate raw events on the request path** — the employer dashboard must stay fast on the day a posting goes viral. Rollup jobs are idempotent per (date, dimension) so a re-run cannot double-count.

**Built in P17. Four details, each a place a reasonable reading would guess differently:**

- **Rollups are computed from the domain tables, not from `AnalyticsEvent`.** The event table is lossy by design (§5.15); applications, stage moves and AI runs are not. A funnel counted from events would drift from the funnel counted from applications, and the second is the one an employer acts on. What the event table is *for* is the question nobody built a table for.
- **Yesterday, not today.** A day still in progress produces a row that is wrong until midnight, and a partial day shown beside complete ones invites a wrong conclusion every morning.
- **A dimension with no activity gets no row**, and a recompute deletes a row whose activity has been corrected away. "No data" and "all zeroes" read the same on a dashboard, and one of them costs a row per posting per day forever.
- **The candidate side computes live**, as §5.14 states. One person has tens of applications rather than thousands, and a nightly job per candidate would build a table larger than the thing it summarises.

**`backfill_rollups` exists because a rollup bug is always found after it has been writing wrong numbers for a fortnight**, and a fix with no way to correct what it already wrote is half a fix.

### 16.2 What each side sees

**Candidate** — application outcomes by stage, resume score trend, profile and CV view counts, match distribution, interview performance over time, employer response times.
**Employer** — funnel by stage with drop-off, time-to-hire, time-in-stage, source effectiveness, posting performance (views → applies → shortlists), applicant quality distribution, recruiter workload, offer acceptance rate, response-time nudges.
**Platform** — activation funnel, upload→analysis→application conversion, job fill rate, AI cost per active user, provider latency and error rates, queue health.

### 16.3 The four measurements that steer the product

1. **Match score against outcome.** Does the scorer predict interviews and hires? If not, the weights are wrong and §10.2 changes. Instrumented from Phase 13.
2. **AI suggestions accepted versus offered.** The best single measure of whether the AI is useful or noise.
3. **Employer response rate and time.** Computed once, in `CompanyDailyStat`, and read from there by both the on-page nudge and the steering snapshot; it becomes a public badge later. *(v1.20 correction: this line referred to a `hiring_summary()` that existed in the frontend mock this codebase replaced. There is no such function, and inventing a second implementation to match the sentence would have produced two answers to "did they respond".)*
4. **Interview outcome against hire.** Whether any interview format predicts anything.

### 16.4 Scale path

Postgres rollups suffice to roughly 10M events per month. Beyond that, raw events ship to ClickHouse or BigQuery and the rollup tables remain the serving layer, unchanged.

---

## 17. Deployment and operations

### 17.1 Topology

```
        Cloudflare (DNS, TLS, CDN, WAF, R2 object storage)
                          |
                    Caddy / nginx
              +-----------+-----------+
              |                       |
        gunicorn (web)          uvicorn (SSE only)     <- stateless
              +-----------+-----------+
                          |
     +--------------------+--------------------+
     |                    |                    |
PostgreSQL 16         Redis              Celery workers
+ pgvector       /0 broker           queues: default | ai | render
+ PITR backups   /1 cache                    | files | email | match
+ token ledger   /2 session cache
     |                                        |
read replica (later)                    Celery beat
```

### 17.2 Environments

| Env | Database | Purpose |
|---|---|---|
| local | Dockerised Postgres + Redis | Development. **Never SQLite** (A2) |
| ci | Ephemeral Postgres | Tests, migration drift check, lint, security scan |
| staging | Small managed Postgres | Mirrors production configuration; provider sandboxes; **data anonymised on import** |
| production | Managed Postgres with PITR | Live |

Production dumps contain resumes. Anonymisation on import to staging is mandatory, not optional, and is a management command that fails closed.

### 17.3 Pipeline

Push → ruff + format check → mypy on services and policies → tests with coverage gate → `makemigrations --check --dry-run` (fails on model drift) → prompt-hash load check → `pip-audit` → build image → deploy staging → smoke tests → manual gate → deploy production → migrate → health check → automatic rollback on failure.

### 17.4 Operations

Nightly `pg_dump` plus WAL archiving with point-in-time recovery, and a **restore rehearsed quarterly** — an untested backup is not a backup (NFR-16). Sentry; structured JSON logs with a request ID propagated into Celery; `/healthz` liveness and `/readyz` readiness covering database, Redis and storage. Alerts on 5xx rate, queue depth, **oldest unprocessed outbox row**, AI error rate, AI daily spend, database connections and disk. Zero-downtime migrations by expand/contract (§5.16). Feature flags allow shipping dark, and every flag has an expiry date with a weekly report of expired ones.

### 17.5 Cost and the performance target

A single 4 vCPU / 8 GB host running the stack via docker-compose, plus managed Postgres and R2, is adequate for the first few thousand users at tens of dollars a month. The architecture permits that topology without requiring it, because the web tier is stateless, state is in Postgres and Redis, and files are in object storage.

**One explicit reconciliation with NFR-1:** PDF rendering and AI post-processing are CPU-heavy and would otherwise consume the same budget as the p95 web target. The `render` and `ai` queues run in their own container with a concurrency cap, so a burst of exports cannot push page renders past 300 ms. This is why they are separate queues in §17.1 rather than a convenience.

Do not buy Kubernetes before there are users.

---

## 18. Scalability

| Dimension | v1 | Next step | Trigger |
|---|---|---|---|
| Web tier | 1–2 gunicorn replicas | Horizontal replicas behind the load balancer | p95 > 300 ms |
| Database | Single Postgres | Read replicas for search and analytics; PgBouncer | Read load, or >80 connections |
| Large tables | Monthly partitions on events, views, audit, AI runs, file access | Detach and archive cold partitions to object storage | Table > 50M rows |
| Job search | Postgres FTS + trigram | OpenSearch as a projection, Postgres remains canonical | > 1M jobs, or facet latency |
| Vectors | pgvector HNSW, partial index on published | Dedicated vector store or a separate Postgres | > 5M vectors, or index build time |
| Matching | Computed on read, Redis cache; alerts batched | Materialise a score table **only if measured to be necessary** | On-read p95 misses budget |
| Fan-out | Two-stage with chunk workers | More chunk workers; a dedicated queue per channel | Fan-out lag |
| Cache | Redis for sessions, fragments, hot queries | Redis cluster; per-fragment template caching | Memory or hit-rate pressure |
| Files | R2 / B2 | Lifecycle to cold storage; regional buckets | Storage cost |
| AI | Shared queue, per-user quota | Routing by price and latency, batch APIs, local models on GPU hosts | Monthly spend |
| Tenancy | Shared schema with `company_id` scoping | Stays shared — schema-per-tenant is a trap at this shape | — |
| Geography | Single region | Read replicas and CDN; residency per market if required | Market entry |

Two structural decisions protect all of it: **the web tier holds no state**, and **no user-facing request waits on an AI provider or a PDF render**. Nearly every scaling failure in this product category violates one of those two.

---

## 19. Workflows

### 19.1 Candidate — guest to first application

```
guest uploads a CV            stored + scanned only; no parse, no AI (A32)
  -> signs up and verifies    upload claimed -> resume.claimed
  -> parse runs               ResumeParse + import proposals
  -> profile import           field by field, accept or reject; never silent
  -> ResumeDocument created   draft populated from the parse
  -> analysis                 deterministic checks, then AI enrichment
  -> notification             "your analysis finished"
  -> candidate edits          autosave into ResumeDraft; undo via snapshots
  -> "save version"           ResumeVersion #1, finalized  -> resume.version_created
  -> reconcile + re-embed     drift surfaced; match cache invalidated
  -> recommendations          computed on read, explained for the top 10
  -> apply                    version frozen, render hash stored, job snapshot
                              taken, grant created, recruiter notified
```

Invariant INV-1 demonstrated: one upload, now working in five modules. Invariant INV-3 demonstrated: nothing was written into the CV that the candidate did not accept.

### 19.2 Employer — registration to hire

```
sign up -> create company -> CompanyMembership(owner)
  -> all subsequent URLs are /employer/<company_slug>/...        (A18)
  -> verification: registration documents (required), optionally a
     proven work-email domain beside them (D-07); a staff reviewer with
     no conflict of interest claims the queue item (D-04); a rejected
     company may apply again after 30 days as a new attempt (D-02)
  -> invite recruiter and hiring manager; invites bind to email
  -> draft a job (or clone last quarter's), attach weighted skills
  -> publish -> job.published -> alert batch + fan-out to saved searches
  -> applications arrive, ranked by the deterministic scorer
  -> recruiter reviews: notes, tags, ratings, bulk stage moves
  -> shortlist -> contact details unlock at the interview stage (A34)
  -> message the candidate in-platform
  -> AI text screen, then a live panel booked against InterviewSlot
  -> scorecards submitted independently, then revealed
  -> offer sent (revisable) -> accepted -> stage hired -> job closed
  -> analytics update: funnel, time-to-hire, response time
```

### 19.3 Recruiter — daily loop

Opens `/employer/<slug>/applicants/`, sees the applicants to the jobs they are assigned to and works them (§2.3, D-13). Rediscovery surfaces that an applicant applied to two other roles last year. Bulk-moves the obvious declines with one summarising event. Messages two candidates. Books an interview into a held slot. Every action is one command endpoint returning one fragment.

### 19.4 Candidate — erasure

Request → 14-day cancellable grace → erasure job. **`CandidateProfile.erasure_started_at` is set first**, and every AI and analysis task checks it on entry and aborts: without that gate a task already in flight writes resume quotes back into `ResumeAnalysis` *after* erasure completes, silently recreating the data.

Each store is handled explicitly, because the ones that are easiest to overlook hold the most:

| Store | Action |
|---|---|
| `ResumeVersion.content` | **Redacted** to a tombstone (A38). The row, `sha256`, `version_no` and lineage survive, so the employer's evidence of what was submitted survives without the content |
| `ResumeDraft`, draft snapshots | Hard delete — nothing references them |
| `ResumeParse.raw_text` / `structured` | Hard delete — derived, no evidentiary role |
| `ResumeUpload`, `Attachment` bytes, `ResumeRender` | Purged from the bucket, with absence verified rather than assumed |
| `ResumeAnalysis`, `ResumeFinding` | Hard delete — **findings quote resume lines verbatim** |
| `CandidateProfile` and children | Anonymised to a tombstone |
| **Profile embedding** | **Nulled** — a vector derived from a resume is derived personal data and is partially invertible |
| **`AIRun.input_snapshot` / `output`** | **Deleted for the erased subject** — resume text was sent to a provider and a copy is held here. The most commonly missed store |
| `Application.answers` | Redacted; `job_snapshot` is employer data and is kept |
| `Message.body`, interview transcripts | Hard delete; the conversation shell survives |
| `AttachmentAccess`, `AuditLog` | **Kept** — pseudonymised by the tombstone, and required to audit the erasure itself |
| `AnalyticsEvent` | `actor_id` anonymised, aggregates kept |

**Applications are anonymised rather than deleted**: the employer retains that an application existed and its outcome, which they may be legally obliged to keep, while holding none of the candidate's personal data.

`candidate.erasure_completed` triggers downstream purges. The erasure is audited. Two limits are stated plainly to the user rather than implied: documents already delivered to employers cannot be recalled, and text already sent to an AI provider cannot be unsent — our copy is deleted, theirs is subject to their retention.

### 19.5 Platform admin — verification review

Opens the queue, **claims** an item (`claimed_by` / `claimed_until`), reviews documents against company details, and either verifies, requests more information with a reason, or rejects. Every outcome writes an audit row and emits `company.verification_state_changed`, plus `company.verified` on success. Claims expire so an abandoned review returns to the queue.

**Conflict of interest (D-04).** A reviewer who is an active member of the company, holds a pending invitation to it, or sent the submission cannot claim it or decide it — the decision is checked again, so a conflict that arises after the claim still blocks it. The refusal is audited (`company.verification_conflict_refused`), the queue tells the reviewer why instead of offering the claim, and there is no override at any staff level: another reviewer takes it. **History (D-02).** Every decision is kept in `CompanyVerificationDecision`; the queue labels a new attempt and lists the earlier decisions beside it, and the company sees its earlier decisions and the date it may apply again.

### 19.6 A provider outage

`ai.run` receives a provider error, classifies it retryable, retries with backoff, then falls back to the next model in the chain. If all fail, the `AIRun` lands in `provider_error`, the Celery job retries later, and the interface names the failure with a retry action. No request thread blocked; no user saw a 500; the deterministic half of every feature still worked.

### 19.7 Workflow catalogue

| Workflow | Owning service | Trigger | Terminal states |
|---|---|---|---|
| Resume ingestion | `resumes.upload` / `parse` | Upload, then claim | parsed, failed |
| Resume authoring | `resumes.draft` / `version` | Builder edits | version finalized |
| Resume analysis | `resumes.analysis` | New version, or request | complete, failed |
| Profile reconciliation | `resumes.reconcile` | `profile.updated`, `resume.version_created` | continuous |
| Job lifecycle | `jobs.services` | Employer action, `closes_at` | closed, archived |
| Application lifecycle | `applications.services` | Apply | hired, declined, withdrawn |
| Messaging | `messaging.services` | Grant exists + company opens | archived, blocked |
| Company verification | `companies.verification` | Document submission | verified, rejected |
| Interview | `interviews.services` | Booking, or start | completed, cancelled, no_show |
| Recommendations | `matching.services` | On read | cached |
| Job alerts | `matching.services` | `job.published`, schedule | sent |
| Notification delivery | `notifications` | Outbox event | delivered, dead-lettered |
| Billing | `billing` | Provider webhook, cron | active, past_due, cancelled |
| Erasure | `accounts.deletion` | Request + grace | anonymised |

---

## 20. Implementation roadmap

Every phase ends with: migrations applied, services unit-tested, policies negative-tested, the mock module it replaces **deleted**, and the feature flag flipped in `apps/core/features.py`. A phase is not done while a `samples.py` or `state.py` it replaces still exists.

Each phase depends only on phases above it.

| # | Phase | Depends on | Done when |
|---|---|---|---|
| **P0** | **Foundations** — Postgres + pgvector, settings split, Celery + Redis, `common`, `files` (Attachment, storage, scanning, signed access, access log), `audit`, `FeatureFlag`, `IdempotencyKey`, **the outbox and `events.emit` API**, CI pipeline, factories | — | The stack runs from one command; a file uploads, scans, signs and serves privately; an event round-trips through the outbox to a logging consumer |
| **P1** | **Identity and tenancy** — accounts, MFA, consent, sessions; companies, `CompanyMembership`, invites with email binding, ownership transfer, last-owner protection, verification state machine; **URL-scoped tenancy middleware**; the three permission layers; staff roles | P0 | Every permission rule has a passing negative test; no unscoped tenant queryset exists; two tabs on two companies cannot cross |
| **P2** | **Taxonomy and reference data** — skills, aliases, relations, categories, industries, locations, languages, **FxRate** and salary normalisation | P0 | ~5k canonical skills seeded; "JS" resolves to JavaScript; Django implies Python; NPR/month and USD/year compare |
| **P3** | **Candidate profile** — profile and children, skills, completeness, privacy controls, settings screens on real data | P1, P2 | `apps/dashboard/samples.py` and `apps/preferences/state.py` deleted |
| **P4** | **Resume ingestion** — upload, scan, dedupe, guest-upload storage without processing, claim, deterministic parse, import proposals | P0, P3 | A guest upload cannot trigger a single AI call; a claimed upload parses |
| **P5** | **Resume documents and builders** — `ResumeDraft`, snapshot ring, `ResumeVersion` with trigger-enforced immutability, templates, all three builder modes, suggestions, the reconcile protocol | P4 | `apps/resume/samples.py` and `state.py` deleted; autosave produces no versions; a finalized version cannot be updated by any means |
| **P6** | **Rendering** — PDF and DOCX pipeline, fonts, pagination, tagged output, render cache, dedicated queue | P5 | Export matches the on-screen preview; a render burst does not move the web p95 |
| **P7** | **Jobs** — lifecycle, skills, remote scope, search (FTS + trigram + stable ranking), saved jobs and searches, alerts, cloning, auto-close, optimistic locking, company public pages, view counting | P1, P2 | `apps/jobs/samples.py` deleted; search returns real rows with real facets |
| **P8** | **Applications and pipeline** — apply with version freezing and job snapshot, stages, event stream, notes, tags, ratings, bulk services, withdrawal, closure, offers, pools, visibility grants, staged contact revelation | P5, P6, P7 | `apps/employers/samples.py` and `state.py` deleted; both timelines render from one event stream |
| **P9** | **Messaging** — conversations, messages, mute, block, report, rate limits | P8 | The `employer_messages` preference governs something real |
| **P10** | **Notifications and webhooks** — two-stage fan-out, delivery ledger, in-app feed, preference resolution, email provider, suppression, send caps, quiet hours, digests, reminders, webhook endpoints and delivery | P0, P8, P9 | `apps/notifications/samples.py` deleted; a muted event is provably never delivered; a 50k fan-out completes in chunks |
| **P11** | **AI infrastructure** — adapters (OpenRouter and Ollama first), the run pipeline with caching, schema validation, retry classification, quotas, kill switch, prompt loading from version control, **the eval harness**, the staff run inspector | P0 | A task runs, caches, is attributed, survives a provider outage, and a prompt change that degrades quality fails CI |
| **P12** | **Resume analysis** — deterministic ATS checks and scorer, findings, experience and skill evidence, score history, AI enrichment through the P11 runner, apply-a-fix as a suggestion | P5, P11 | Analysis screens render real findings; nothing reaches a draft without an accept; the report survives AI being off *(delivered. Cover letters moved to P14, the assistant's streaming endpoint and FR-4.6 deferred — §21)* |
| **P13** | **Matching and recommendations** — embeddings with model versioning, selectivity-branched retrieval, the deterministic scorer, breakdown, cold start, diversity and exposure caps, fairness allow-list tests, on-read caching, LLM explanations | P2, P3, P7, P11 | Recommendation and applicant-ranking screens run on the scorer; no score table exists *(delivered. Alert batches stayed with P7's saved-search relevance — see §21)* |
| **P14** | **Cover letters and AI writing** — the letter library, AI drafting through the P11 runner, tone and notes, edit and duplicate, sent-letter immutability, and the assistant builder's reply | P5, P8, P11 | A drafted letter is editable, attributable and never sent by the drafting; the assistant answers and proposes only through the accept path *(delivered)* |
| **P15** | **Talent search** — employer-side candidate search, grant creation on opt-in, access logging, export gating, anomaly detection | P3, P13 | A candidate can see who viewed them *(delivered)* |
| **P16** | **Interviews** — AI text engine with pooled and rotated questions, practice mode, rubrics, evaluation, outcomes; `InterviewSlot` with the exclusion constraint, Jitsi rooms, scheduling, reschedule, cancel, no-show, scorecards, reminders | P8, P11 | The `interview` flag is on; outcomes flow into the pipeline; double-booking is impossible *(delivered. ICS invites and multi-round plans deferred — §21)* |
| **P17** | **Analytics** — capture, rollups, candidate and employer dashboards, the four steering measurements, fairness sampling | P8, P13, P16 | Dashboards read rollups, never raw events *(delivered. Fairness outcome sampling deferred — §21)* |
| **P18** | **Billing** — plans, subscriptions, entitlements, usage counters, local payment providers, invoices, gating for pro templates and AI quotas | P1, P11 | A plan limit is enforced in a service, never in a template *(delivered. eSewa/Khalti/Fonepay adapters deferred — §21)* |
| **P19** | **Hardening and launch** — Alpine CSP build and `unsafe-eval` removal, retention jobs, erasure pipeline, load testing, restore drill, runbooks, legal documents, staff tooling | All | Restore rehearsed; load test at 10× expected traffic passes *(code delivered; the restore drill and the 10× run need staging infrastructure and are outstanding — §21, docs/DEPLOYMENT.md)* |
| **P20** | **AI video interview** *(gated)* | P16, P19, legal review | Consent, retention and audit paths verified; content-only scoring |

**A renumbering, recorded rather than quietly applied (v1.17).** From v1.15 this document carried two incompatible meanings of "P14": the table above said talent search, while §21 and the P12 changelog said cover letters, because the delivery plan inserted them there and the table was never updated. Two meanings of one label in one document is worse than either meaning, so the table now records the delivered order and everything from talent search onward moves up by one. Phase *names* are the stable reference — "the interviews phase" is unambiguous where "P15" now needs a date. Earlier changelog entries are left as they were written; correcting history to match a later renumbering would make the record of what was decided when unreadable.

**Why this order.** P0–P2 build ground with no visible feature, which feels slow and is correct: identity, tenancy and taxonomy are the three things every later phase reads. The outbox is in P0 because services emit events from P1 onward and retrofitting the emit call into forty services is worse than building a consumer that logs. P6 precedes P8 because `submit_application` freezes a render. P13 follows P11 because embeddings need the AI layer. P14 follows P8 and P11 because a cover letter needs both an application to attach to and a runner to draft with. P20 is last because it is the highest legal risk and the lowest defensibility in the product.

---

## 21. Deferred and out of scope

**Deferred, designed for:** the public JSON API (A17, §8.3) · web push · magic-link login · AI video interview and its media and signal tables (A16, P19) · fine-tuning on `AIFeedback`, which underperforms a better prompt below roughly 10k labels · OpenSearch · read replicas · ClickHouse · schema-per-tenant, which is a trap at this shape and is not planned at all.

**Out of scope for v1, deliberately removed from the brief:** career roadmap · hosted portfolio pages · template side-by-side comparison · AI candidate comparison · learning recommendations · EEO and diversity data collection (regulated; only with counsel). None of them change whether a candidate gets a job or an employer fills a role, which is the only test that matters now.

**Deferred out of P12, with reasons (v1.15):**

- **FR-4.6 — missing skills expressed as market demand.** Requires matching data the platform does not have until P13. The skills-coverage component scores what the CV evidences, and — when an analysis names a job — coverage against that job's declared skills, both of which are facts. Demand is not a fact this system knows yet, and generating it from a model's opinion of the labour market is the kind of confident nonsense the architecture exists to refuse.
- ~~**Cover letters**~~ — delivered in P14.
- **The assistant builder's streaming endpoint** (§9.5, `resume.section_draft`). Still deferred, and the reason narrowed in P14. **The assistant now answers**; what it does not do is stream. §9.5 chose an ASGI endpoint so the reply could arrive token by token, and §17 runs synchronous workers. An answer that arrives in one HTMX swap is less pleasant than one that types and enormously better than the silence this screen had from P5 to P14 — so the polish is deferred and the feature is not. The bounded exception §9.5 describes stays open for the phase that wants it.
- **Skill extraction into the profile** (`skills.extract`). The prompt shipped with P11 and P12 gave it no caller. Analysis reports skill *evidence* deterministically — which listed skills a bullet supports — and proposing new `CandidateSkill` rows is the import-proposal flow P4 already owns. Wiring a second proposal path for the same object would be the duplication this project keeps removing.

**Deferred out of P13, with reasons (v1.16):**

- **`Skill.embedding`** (§5.3). The column is specified and was not built: no consumer is designed for it. The matcher gets partial credit from `SkillRelation`, which is curated and explainable, and alias resolution is trigram plus the alias table. A vector on a skill would be a column with an index and no reader — and the right time to add it is when something needs nearest-neighbour search over the taxonomy itself, which nothing does yet.
- ~~**`CandidateProfile.search_vector`**~~ (§5.4) — delivered with talent search, which was its only reader. *(Struck in v1.22: the P19 audit found this still listed as deferred while the generated column and its GIN index have existed since the talent-search phase.)*
- **FR-4.6, missing skills as market demand.** Deferred out of P12 to here, and still deferred at the end of P19. The reason has not changed and has not weakened: the platform has skills-per-posting and now, since P17, application outcomes as well — so a demand figure is computable. What is not settled is whether it should be *shown*. A count of postings is not demand, and a number presented as demand tells a candidate to learn whatever employers most often type into a form. This needs a product decision about what the figure claims, not more data.
- **Alert batches on the matcher.** §20's P13 row named them. `JobAlertMatch` already exists (P7) and stores search relevance for a saved search, which is the right thing for an alert: an alert is a standing *query*, and scoring a saved search against a profile would answer a question nobody asked. A profile-scored alert is a different feature — "tell me when something matches me" rather than "tell me when something matches this search" — and it belongs with the notification-preference work rather than being retrofitted onto the search alert.

**Deferred out of P15, with reasons (v1.18):**

- **Saved talent searches and candidate alerts.** A saved job search exists for candidates (P7); the employer-side equivalent — "tell me when somebody matching this appears" — is a standing query that would send a notification about a person who has just become discoverable. That is a reasonable feature and a different consent question, and it needs the answer written down before it is built rather than inherited from the job-alert code.
- **Trigram fallback on talent search.** Job search widens to trigram similarity when full text returns little (§10.5); talent search does not, and `TalentResult.used_trigram` is always false. The reason is corpus size rather than principle: a thin result over a few thousand profiles is usually a real absence, and widening it would suggest people who do not match. It becomes worth building when the corpus is large enough for a typo to hide a real candidate.
- ~~**Rate limiting on search endpoints**~~ — delivered in P19. Job search is limited per client and **fails open** (public, and taking it down over a missing cache is self-inflicted damage); talent search is limited per *membership* and fails closed, because per company would let one compromised seat spend the team's allowance and per IP would punish an office. The P15 reasoning still holds and is why anomaly detection remains the primary defence: a per-minute limit was never what stops a paying company reading too much.
- ~~**A staff queue for anomaly signals**~~ — delivered in P19 at `/staff/anomalies/`, reading the audit rows the signals already wrote rather than a table of its own.

**Deferred out of P16, with reasons (v1.19):**

- **ICS calendar invites** (FR-10.6). Reminders at 24 hours and 1 hour are built; the `.ics` attachment is not. It needs the email provider to send an attachment, which P10 built for text and HTML only, and a calendar file that half of mail clients reject silently is worse than a reminder that works. It belongs with the email hardening pass.
- **`InterviewRound` as a working multi-round plan** (FR-10.9). The table exists and a job can carry rounds; nothing yet advances a candidate from round one to round two, because that is a pipeline-stage question — which round corresponds to which stage — and answering it inside interviews would put pipeline logic in two places.
- **Recurring interviewer availability.** `InterviewerAvailability.recurrence` is stored and not interpreted. Slots are published explicitly, which is correct and tedious; expanding a recurrence rule into slots is a scheduled job with an expiry policy, and it should be built when somebody has enough interviews for the tedium to matter.
- **The JaaS JWT** (§11.3). Explained above: the key does not exist yet, and a token the vendor rejects is worse than none.

**Deferred out of P17, with reasons (v1.20):**

- **Fairness outcome sampling** (§10.4). §10.4 says adverse-impact review begins here with the hook present from the matching phase, and the hook is present: every application carries a frozen match snapshot and every scorer input is on a written allow-list. What is not built is the sampling itself, and the reason is that adverse-impact analysis requires protected attributes to analyse *against* — which the platform deliberately does not collect (§10.4 excludes them permanently, and §21 puts EEO collection out of scope pending counsel). Sampling without them would produce a fairness report that measures nothing while claiming to measure fairness, which is worse than not having one. This needs a legal and product decision before it needs code.
- **The platform activation funnel** (§16.2, platform view). AI cost, failure rates and latency are built; the signed-out top of the funnel is not, because it needs the `anon_id` cookie and a consent posture for it, and setting an analytics identifier on a visitor who has not consented is the kind of thing §15.5 exists to prevent. `AnalyticsEvent.anon_id` is in the schema and nothing writes it.
- **ClickHouse or BigQuery** (§16.4). Postgres rollups suffice to roughly 10M events a month, and the scale path is documented precisely so nobody builds it early.

**Outstanding after P19, and why they are not code (v1.22):**

- **The restore drill has not been rehearsed.** NFR-16 requires a quarterly rehearsal against a real dump, and `docs/RUNBOOKS.md` §5 is the procedure. It needs a backup target and a machine to restore onto. Until it has been run once, this deployment has an untested backup — which, as NFR-16 says, is not a backup. **This is a launch blocker.**
- **The load test has not been run at 10× expected traffic.** `scripts/loadtest.py` exists, works, and reports per-path percentiles against NFR-1's 300 ms budget. Running it against a development server measures the development server; the exit criterion needs staging with a seeded corpus. **Launch blocker.**
- **No container image or deployment manifest.** The application runs from a checkout with the commands in `docs/DEPLOYMENT.md` §1. §17.3's pipeline step "build image" therefore has nothing to build. The web tier is stateless and dependencies are hash-locked (A36), so this is packaging work rather than design work.
- **Terms and privacy are drafts.** They are published as version `0.1-draft`, the pages say so in a banner, and the text describes what the platform actually does — including the retention schedule and the two things erasure cannot undo. Counsel replaces them and publishes 1.0 with `is_draft=False`. **Launch blocker**, and the only one that needs a lawyer rather than an afternoon.
- **Support tickets, abuse reports and moderation actions** (§5.14). The models are specified and not built. Abuse arrives through the report path P9 built on messages; a queue and a ticketing workflow are the surfaces for an operations team that does not exist yet, and building them now would repeat the mistake §21 already avoided once with the anomaly queue — a screen with no operator.
- **`SystemSetting` and `EmailTemplate` as rows** (§5.14). Email bodies are templates in version control and settings are environment variables. Both work; moving them into the database buys staff editing, which nobody has asked for.
- **Announcements (D-10, owner decision 2026-09-18).** An operator broadcast — its model, management screen, publishing workflow and the `system.announcement` event — is out of scope for v1, for the same reason as tickets and settings: it is an operations-team surface and there is no operations team yet. When one exists, the model and a staff screen are added together.

**Deferred out of P18, with reasons (v1.21):**

- **eSewa, Khalti and Fonepay adapters** (ADR-A15, ADR-A55). The `PaymentProvider` interface is built and exercised end to end — a signed callback settles an invoice, an unsigned or edited one is refused and writes nothing, and a replayed one is a no-op — but the two real adapters are not written, because neither can be verified without a merchant account. An integration whose correctness nobody has confirmed, shipped behind a button that takes money, is precisely the "never fake functionality" line this project holds. What exists instead is a settlement path that genuinely works: an invoice is raised, the customer transfers, and staff record it against the invoice with an audit entry and the same downstream events a gateway would produce. Writing an adapter is then a file, not a redesign.
- **Self-serve employer checkout.** Company plans are staff-assigned. The pricing page offers employers "talk to us", so a checkout would be advertising a purchase nobody can make, and pricing one would mean inventing employer terms this document has never stated.
- **Proration and refunds on a plan change — decided out (D-08).** Free → Pro starts a new full period that day; Pro → Free waits for the period end; nothing is prorated or refunded. A payment-provider proration model is not built.
- **Dunning beyond one email.** An unpaid invoice produces `subscription.payment_failed` once, and the grace period then ends the subscription. A retry schedule, escalating reminders and a card-update flow all belong with a real gateway; there is nothing to retry against a bank transfer.

**Product work not yet scheduled but expected within a year:** two-way calendar integration, hosted careers pages and an embeddable job widget, CSV and ATS import/export, referrals, salary benchmarking from the platform's own posting data, and recruiter outreach sequences.

---

## Appendix A — Frontend contracts the backend must satisfy

These shapes exist in the templates today and are therefore requirements.

| Contract | Source | Backend obligation |
|---|---|---|
| Resume document shape (`sections`, `entries`, `bullets`, `rail_sections`) | `apps/resume/samples.py:resume_doc` | `ResumeDraft.content` and `ResumeVersion.content`, with `content_schema_version` |
| Save state `dirty` / `saving` / `saved_at` | `apps/resume/state.py:237` | `ResumeDraft` fields; autosave must never create a version |
| Undo and redo depth 25 | `apps/core/store.py:HISTORY_LIMIT` | `ResumeDraftSnapshot` ring |
| Match object — `score`, `matched`, `missing`, `required_count`, `notes`, `advice`, banded `label` | `apps/jobs/samples.py:Match` | The `MatchResult` value object returned by `matching.score()`, snapshotted onto `Application` |
| Analysis — `breakdown`, `findings` by severity, `skills`, `missing_skills`, `ats`, `links`, `history` | `apps/resume/samples.py:analysis` | `ResumeAnalysis` + `ResumeFinding` + `ResumeAtsCheck` + `skill_evidence` |
| Verification as a four-step state machine with documents and a reviewer note | `apps/employers/samples.py` | `CompanyVerification` + documents + claim fields |
| Pipeline stages with counts and badges | `apps/employers/samples.py:pipeline_stages` | `PipelineStage` + aggregated counts |
| Notification kinds mapped to preference groups | `apps/notifications/samples.py:KIND_EVENTS` | `Notification.kind` → `NotificationPreference.event_key`, enforced by the registry test |
| Six preference groups, two channels, role-specific wording | `apps/preferences/state.py` | `NotificationPreference` rows |
| `employer_messages` preference and the `message` notification kind | `apps/preferences/state.py`, `apps/notifications/samples.py` | The messaging domain (§12) — the reason it is in v1 |
| Template catalogue as layout × accent × font × family × tier | `apps/resume/samples.py:_TEMPLATES` | `ResumeTemplate` |
| Deadline urgency surfaced only inside 14 days | `apps/jobs/samples.py:Job.closing` | A model property, computed once |
| Feature flags gate navigation | `apps/core/features.py` | Remains the shipping gate; database flags layer on top |
| Inline theme script, Alpine `x-` expressions | `templates/partials/_theme_script.html`, `static/vendor/alpine.min.js` | The CSP in §15.4 (A31) |

## Appendix B — Glossary

**Actor** — the user performing an action; always the first argument to a service.
**Aggregate** — a cluster of models with one entry point; `Application` owns its events, notes and tags.
**Canon** — the representation that wins for a class of field (§5.5.3).
**Command endpoint** — the existing `.../act/` POST URL returning an HTML fragment.
**Draft** — the mutable working copy of a resume document; the builder's only write target.
**Grant** — a `CandidateVisibilityGrant` row authorising one company to read one candidate.
**Outbox** — the transactional event table that decouples a state change from its side effects.
**Portal** — a view and template app that owns no models.
**Selector** — a scoped read query.
**Service** — a transactional write operation.
**Version** — an immutable, finalized `ResumeVersion`; documents change by gaining versions, never by mutation.
