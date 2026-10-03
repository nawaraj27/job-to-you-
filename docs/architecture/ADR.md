# CareerAI — Architecture Decision Records

**Companion to SRS v1.2.** The SRS says *what* the system is. This document says *why*, and what it cost.

**How to use it.** Before changing anything structural, find the ADR that owns it. If your change contradicts a **Reversible: No** decision, stop and escalate — that is an architecture change under the freeze policy (SRS §0.2), not an implementation choice.

**Status vocabulary.** *Accepted* — in force. *Superseded* — replaced, with a pointer. *Reversible* — Yes (a refactor), Qualified (possible with a migration plan), No (structural; assume permanence).

---

## ADR-A1 — Authorization derives from company membership

**Problem.** A recruitment platform needs to know what a person may do. The obvious model — a `role` column on `User` — was already in the codebase (`apps/accounts/models.py`).

**Context.** One person can be a job seeker, a recruiter at company A, and the owner of company B, simultaneously and permanently. "Employer", "Recruiter" and "Company Admin" are not properties of a person; they are properties of a *relationship between a person and a company*.

**Alternatives considered.**

| Option | Rejected because |
|---|---|
| `User.role` enum | Cannot express two companies, or two roles, at once. Every workaround becomes a permission bug |
| Django Groups per company (`acme_recruiters`) | Group names encode data; unqueryable; unbounded group table; no place for per-membership metadata |
| Separate `Recruiter` / `Employer` user models | Multi-table inheritance, duplicated auth, and a person who is both breaks the model entirely |
| `CompanyMembership` join model | Selected |

**Selected solution.** Three independent authority axes: account identity (`User`), company membership (`CompanyMembership(company, user, role, permissions)`), platform staff (`StaffRole`). `User.signup_intent` survives only to pick an onboarding path and is never branched on for capability.

**Why.** It is the only model where "a person at two companies with two roles" is representable without special cases, and it puts authorization data where authorization questions are asked.

**Advantages.** Multi-company is free. Per-membership metadata (title, grant keys, assignment) has a home. Revocation is one `deleted_at`. Queryset scoping derives naturally.

**Disadvantages.** Every employer query carries a join. `request.company` must exist and be validated on every request. More tables than a role column.

**Trade-offs.** A small permanent join cost for an entire class of bugs that cannot occur.

**Long-term consequences.** Every employer-side feature, query, test and URL is membership-scoped from birth. Adding a role is a data change; adding a capability is one grant key.

**Reversible.** No. Every employer table's access path assumes it.

**Migration if it ever changes.** There is no realistic reverse. Forward evolution (per-team or per-department scoping) is an extra column on the membership plus a scoping predicate, not a redesign.

---

## ADR-A2 — PostgreSQL 16 with pgvector, from Phase 0

**Problem.** The codebase shipped on SQLite. Choosing when to leave it.

**Context.** Requirements that are not optional: full-text job search, JSONB resume documents, vector similarity, array containment on skills, partial unique indexes ("one live application per job"), table partitioning, exclusion constraints for interview slots, and `SELECT … FOR UPDATE SKIP LOCKED` for the outbox. SQLite supports none of them adequately.

**Alternatives considered.** Stay on SQLite for development and use Postgres in production (rejected: environment divergence hides the bugs that matter until production). MySQL (rejected: no partial indexes, weaker JSON, no exclusion constraints, no pgvector). Postgres plus a separate vector database (deferred: premature at this scale, see A24).

**Selected solution.** PostgreSQL 16 + pgvector in every environment including local, via Docker.

**Why.** Half the SRS's invariants are enforced by Postgres-specific features. A database that cannot express them pushes enforcement into application code, where it eventually fails.

**Advantages.** One database in all environments. Constraints hold in dev. Search, vectors and queueing without extra infrastructure.

**Disadvantages.** Contributors need Docker. Slower test startup than in-memory SQLite.

**Trade-offs.** Local setup friction for correctness guarantees.

**Long-term consequences.** Postgres-specific SQL is permitted and expected. Portability to another engine is explicitly not a goal (see A29's rejection of a repository layer).

**Reversible.** No.

**Migration if it ever changes.** None planned. A future engine change would be a rewrite of constraints, search and vectors.

---

## ADR-A3 — Server-rendered views call services directly; no parallel JSON API

**Problem.** Whether to build the product on an internal HTTP API.

**Context.** The frontend is server-rendered Django templates with HTMX, already using a consistent command-endpoint pattern (`.../act/`). An API-first backend would mean writing every feature twice, with the JSON layer having no consumer.

**Alternatives considered.** DRF for everything with HTMX consuming JSON (rejected: doubles surface area, and HTMX wants HTML). GraphQL (rejected: worse for a server-rendered product, plus a new attack surface). Services called directly by views, with a narrow external API added when a consumer exists (selected).

**Selected solution.** Views parse input, call a policy and a service or selector, render a fragment. The external JSON API is specified (SRS §8.3) and deferred (A17).

**Why.** The second consumer does not exist yet. Building for it now costs a phase and delivers nothing.

**Advantages.** Roughly one phase saved. One implementation to test. No serializer drift.

**Disadvantages.** A mobile client cannot be built until the API is. Some may mistake "no API" for "no boundary".

**Trade-offs.** Deferred optionality for delivered speed — safe only because the service layer is the real boundary.

**Long-term consequences.** The service layer must stay HTTP-ignorant, or the deferred API becomes a rewrite instead of a wrapper. This is the single condition that makes A3 safe, and it is enforced by A11's layer contract.

**Reversible.** Yes. Adding `/api/v1/` is additive: serializers over existing services.

**Migration if it ever changes.** Add DRF, write serializers, expose existing services. No domain change.

---

## ADR-A4 — Permissions and tenancy are built in Phase 1

**Problem.** When to build authorization.

**Context.** The original roadmap scheduled it after the API and after mock data replacement. Retrofitting multi-tenant isolation onto existing employer data is how candidate data leaks between companies.

**Alternatives considered.** Build features first, secure later (rejected: every query written in between must be revisited, and the ones missed are invisible). Rely on view-level checks (rejected: one forgotten decorator is a breach). Build tenancy before any tenant data exists (selected).

**Selected solution.** Phase 1 delivers identity, membership, URL tenancy, the three enforcement layers and staff roles, before any tenant row exists.

**Why.** Scoping is cheap when there is nothing to scope and expensive when there is everything.

**Advantages.** Every later query is born scoped. The CI check banning unscoped querysets can be enforced from the first employer feature.

**Disadvantages.** Two phases before a visible feature. Feels slow.

**Trade-offs.** Delayed gratification for an entire class of severe bugs.

**Long-term consequences.** Security work stops being a phase and becomes a property.

**Reversible.** Not meaningfully — the alternative is to build it later, which is the failure this prevents.

**Migration.** N/A.

---

## ADR-A5 — Django admin plus three staff queues, not a third application

**Problem.** The brief asked for a full platform-administration application: users, companies, jobs, skills, templates, AI providers, email templates, flags, tickets, abuse reports.

**Context.** That is a third frontend to design, build, test and maintain, for an audience of roughly five people, competing with candidate and employer surfaces that generate revenue.

**Alternatives considered.** Full custom admin SaaS (rejected: cost wildly out of proportion to users). Pure stock Django admin (rejected: genuinely bad at queue workflows — claiming, SLAs, side-by-side document review). Django admin, customised, plus purpose-built UI only where admin is weak (selected).

**Selected solution.** Django admin for reference and record CRUD. Three custom staff surfaces: company verification review (with claim-and-lock), abuse and moderation, AI run inspector.

**Why.** Django admin is excellent at CRUD and poor at workflow. Build only the workflow.

**Advantages.** Enormous scope saving. Admin comes free with model changes.

**Disadvantages.** Django admin has no trustworthy object-level permission story, so staff see broadly. Mitigated by making admin read-mostly, removing destructive actions in favour of audited service calls, and requiring MFA plus IP allow-listing (SRS §15.2).

**Trade-offs.** Accepting a blunter tool for internal users to fund sharper tools for paying ones.

**Long-term consequences.** Revisit at roughly 100 companies, when staff workflows justify their own application.

**Reversible.** Yes. A custom admin can be built later against the same services.

**Migration.** Build staff portal screens incrementally; retire admin pages one at a time.

---

## ADR-A6 — Deterministic scoring ranks; the LLM only explains

**Problem.** How to match candidates to jobs.

**Context.** The obvious approach — ask a model to score the pair — is O(candidates × jobs). At 100k candidates and 5k open roles a full refresh is 500M inferences. It also cannot be sorted, filtered or paginated in SQL, and it is not reproducible.

**Alternatives considered.**

| Option | Rejected because |
|---|---|
| LLM scores every pair | Cost and latency impossible; not reproducible; unsortable |
| Pure vector similarity | Ranks on semantic proximity, which is not job fit; explains nothing |
| Pure keyword/boolean rules | Brittle; no partial credit; "Django" fails to match "Python" |
| Vector retrieval + deterministic scoring + LLM explanation of the top N | Selected |

**Selected solution.** SRS §10.2: filter, retrieve, score deterministically across six weighted components, adjust for diversity, explain the top ten with a model.

**Why.** Ranking must be reproducible, auditable, cheap and defensible. Explanation must be fluent. Those are different jobs, and one tool is bad at the other.

**Advantages.** Free at any volume. Golden-file testable. The breakdown *is* the explanation the UI renders. Tunable weights. Legally defensible.

**Disadvantages.** Weights are guesses until outcome data exists (P16). No emergent semantic insight in the ranking itself.

**Trade-offs.** Some ceiling on subtlety, for total control and zero marginal cost.

**Long-term consequences.** A learned ranker can be introduced later *behind the same interface*, validated against the deterministic baseline. The baseline never becomes obsolete; it becomes the control group.

**Reversible.** Yes — the scorer is one module with a stable signature.

**Migration if it changes.** Add the learned scorer alongside, run both, compare against outcomes, switch by `algorithm_version` with the old version still serving until cut over.

**P12 amendment — the same split governs the resume score.** This decision was written about matching, where the argument that carries it is cost: O(candidates × jobs) inference is impossible. Scoring one CV has no such problem — a model could score it for a fraction of a cent — so the extension needs its own justification, and it has three.

**Reproducibility.** FR-4.8 puts scores on a line over time. A model scoring the same document 61 one month and 66 the next has produced a line that measures the model, not the CV, and a candidate reading it draws a false conclusion about their own work. `ResumeAnalysis.algorithm_version` is what makes the comparison honest: two scores are comparable when it matches, and the interface marks the ones that do not.

**Defensibility.** "Your CV scored 61 because 3 of 14 bullets carry a number and two roles have dates nothing can parse" survives being challenged by the person it is about. "The model said 61" does not. The four components and their notes *are* the explanation, so no second account of the score exists to disagree with the first.

**Degradation.** §9.3.6 requires an AI feature to fall back to its deterministic parts rather than to an error. That is only possible if the deterministic parts carry the feature's value. With the kill switch off, the budget spent or the provider down, an analysis still produces the score, every ATS check, the measured facts and every deterministic finding — and the report says plainly that no model contributed. If the score came from the model, the whole screen would be an error page.

What the model does here is what it does in matching: explain, and add the judgement a measurement cannot make — whether the writing says anything, whether the roles tell a story. Those arrive as findings marked `from_ai`, labelled in the interface, and carrying no `section_ref`, so a model cannot address a fix at a part of the document whose ids it has never seen.

---

## ADR-A7 — Two candidate representations, with a reconciliation protocol

**Problem.** Is a résumé a view over a structured profile, or a document?

**Context.** A pure normalised profile with resumes rendered from it breaks the first time a candidate tailors one bullet for one application — which is the product's core use case. Pure documents make matching, search and gap analysis impossible.

**Alternatives considered.** Normalised profile only, resumes generated (rejected: tailoring impossible). Documents only (rejected: nothing is queryable; skills become string matching). Two independent copies (rejected: guaranteed to disagree, which is worse than one). Dual representation with declared canon and explicit reconciliation (selected).

**Selected solution.** `CandidateProfile` and its children are canon for **facts** and are what matching reads. `ResumeVersion.content` is canon for **wording and arrangement** and is what rendering reads. Every document node carries `source_ref` and `sync_state ∈ {linked, edited, detached}`. A reconcile service computes drift and offers it as accept/keep-mine.

**Why.** The two representations exist for genuinely different reasons. Pretending one is derivable from the other breaks a real workflow; letting them drift silently breaks the product's central promise.

**Advantages.** Tailoring works. Matching works. "Your CV mentions Celery, your profile does not" is a query rather than a guess.

**Disadvantages.** The most conceptually demanding part of the system. Reconciliation UX must be genuinely good or users will ignore it.

**Trade-offs.** Real complexity, bought deliberately, because the alternatives each break something the product promises.

**Long-term consequences.** Every feature that writes candidate data must decide which canon it writes to. The `sync_state` field makes forgetting visible.

**Reversible.** Qualified. Collapsing to one representation would delete either tailoring or matching.

**Migration if it changes.** Adding a third representation (e.g. a public profile page) reuses the same canon rules; it is a projection, not a new canon.

---

## ADR-A8 — Applications reference an immutable version and a render hash

**Problem.** What exactly did the candidate submit?

**Context.** If an application points at a mutable resume, a candidate editing their CV silently rewrites history inside an employer's ATS. Any later dispute — "this is not what I sent", "this is not what we reviewed" — is unresolvable.

**Alternatives considered.** FK to `ResumeDocument` (rejected: mutable). Copy the content into the application (rejected: duplicates a large document per application and loses the link). FK to an immutable `ResumeVersion` plus the SHA-256 of the rendered file (selected).

**Selected solution.** `Application.resume_version` (immutable, trigger-enforced per A19) and `Application.submitted_render_sha256`.

**Why.** An application is a legal-ish artefact. It must be exactly reproducible years later.

**Advantages.** Byte-level reproducibility. Disputes are answerable. Renders are cacheable by hash.

**Disadvantages.** Versions accumulate. Storage grows. Deleting a resume cannot cascade to applications.

**Trade-offs.** Storage for evidentiary integrity — an easy trade.

**Long-term consequences.** Erasure must anonymise rather than delete applications (SRS §19.4), because the employer's record must survive while the candidate's PII must not.

**Reversible.** No.

**Migration.** N/A.

---

## ADR-A9 — Video interview scoring evaluates content only

**Problem.** The brief specified confidence indicators, eye-contact estimation, engagement estimation and sentiment as scoring inputs.

**Context.** These signals are not supported as predictors of job performance; they systematically disadvantage neurodivergent, disabled and non-native-speaking candidates; and they are directly regulated — the EU AI Act treats employment screening as high-risk, NYC Local Law 144 requires published bias audits of automated employment decision tools, and Illinois AIVIA requires explicit consent and reporting for AI video analysis.

**Alternatives considered.** Build as specified (rejected: builds a discriminatory mechanism and an audit trail proving it). Drop video entirely (viable, and still the fallback). Build the pipeline, score content only, keep behavioural signals as candidate-facing practice feedback at most (selected).

**Selected solution.** Evaluate transcript content — completeness, structure, relevance, specificity of evidence. Behavioural signals are not scoring inputs. The generic signal table is deferred with the feature (P19), and `is_scoring_input` defaults false so re-enabling anything is an explicit, audited, per-jurisdiction act.

**Why.** The removed signals add legal exposure and subtract fairness while contributing nothing defensible.

**Advantages.** Regulatory posture is safe by construction. No bias-audit exposure from signals that were never used.

**Disadvantages.** Loses a superficially impressive demo feature that competitors advertise.

**Trade-offs.** Marketing sizzle for defensibility.

**Long-term consequences.** If a jurisdiction ever permits and a customer demands it, the switch is configuration plus a legal review — not a redesign.

**Reversible.** Yes, deliberately — but only through an explicit, audited configuration change.

**Migration.** Enable signals, set `is_scoring_input`, publish a bias audit. Not before counsel.

---

## ADR-A10 — AI is abstracted at the task level

**Problem.** How to avoid depending on one AI provider.

**Context.** The instinctive answer is a thin wrapper over provider SDKs. Those converge on the lowest common denominator and leak provider concepts anyway (tool calling, JSON mode, system-prompt placement).

**Alternatives considered.** Direct SDK calls at each call site (rejected: provider change touches everything). A generic client wrapper (rejected: lowest-common-denominator, still leaks). LangChain or similar (rejected: a large dependency that abstracts the wrong seam and moves faster than this project wants). Task-level abstraction (selected).

**Selected solution.** Callers say what they want done — `ai.run("resume.analyze", input, actor=…)`. Provider, model, routing, fallback, timeout and cost ceiling are configuration behind that call. Providers implement one adapter protocol: `complete`, `embed`, `stream`.

**Why.** Callers care about the task, not the vendor. The seam belongs where intent is expressed.

**Advantages.** A new provider is one file plus a settings entry. Routing, caching, quota and audit apply uniformly. Call sites never change when a provider does.

**Disadvantages.** Provider-specific capabilities are reachable only by extending the protocol. Streaming complicates the interface.

**Trade-offs.** A capability ceiling for uniform control.

**P14 amendment — the seam holds for text a candidate signs their name to.** Cover letters and the assistant builder are the first features where the model's output is not a judgement the candidate reads but *words they will send as their own*. Both go through `runner.run` unchanged; what P14 added is a second **shape** for INV-3, not a second pipeline.

Where a draft already exists — the assistant builder proposing bullets — the output becomes a `ResumeSuggestion` and only an accept writes the document, exactly as resume analysis does. Where the text *is* the new thing being made — a cover letter — the letter itself is the proposal: it lands in the candidate's library, and applying is a separate explicit act with no code path from drafting to submission.

Stating this as an amendment rather than leaving it implicit, because a reader who knew only the `ResumeSuggestion` mechanism would conclude that cover letters had no protection at all, and would be wrong about *why* rather than about the fact.

**P13 amendment — two protocols, not one method.** The selected solution above named `complete`, `embed` and `stream` on a single adapter protocol. P13 needed embeddings and split it: `Adapter` for completions, `EmbeddingAdapter` for vectors.

The reason is that not every provider does both, and a protocol whose implementations raise `NotImplementedError` from half its methods has stopped describing anything — a caller cannot tell from the type whether the call will work, which is the entire value a protocol provides. Two protocols make the capability checkable: `_dispatch_embedding` filters the fallback chain to providers that actually implement `embed`, so a completion-only provider in an embedding task's chain is skipped with a stated reason instead of producing a 404 that reads like an outage.

What did **not** change is the rule. `runner.embed` performs the same kill switch, the same budget check, the same chain walk, the same three-way retry classification and the same `AIRun` record as `runner.run`. It is a different *call*, not a different *pipeline* — a second orchestrator is precisely what the P11 amendment below exists to prevent, and writing one for embeddings would have been the first bypass. Three differences are deliberate and stated in the code: no prompt template (the text is the input, not an instruction around one), no cache lookup (the vector is stored on the row that needed it, so that column is the cache), and never raises (a failure is a status, as with `run`).

`stream` remains unimplemented and still has no caller (§9.5).

**P11 amendment — the seam is one function, and it is mandatory.** Implementation narrowed "task-level abstraction" from a shape to a rule: `ai.services.runner.run` is the only way an AI call is made, and everything uniform about a call happens inside it — kill switch, cache, quota, fallback chain, schema validation, `AIRun` record. A feature passes a task key and inputs. It does not name a provider, retry, price a call, or write an `AIRun`.

The reason for making this binding rather than customary: the advantage claimed above — "routing, caching, quota and audit apply uniformly" — is not a property of having an abstraction, it is a property of nothing bypassing it. A second entry point that called an adapter directly would still satisfy A10 as originally written while quietly spending money nobody counted. Two things enforce it: the adapters raise only `ProviderTimeout` / `ProviderFailure` / `ProviderRefused`, which are meaningless outside the runner's classification, and provider construction happens from `config.ai` inside `_dispatch`, so a caller wanting an adapter would have to build one itself and the review would see it.

`embed` and `stream` from the selected solution are not implemented: nothing needs them yet, and a protocol method with no caller is a guess about the future signed in advance. Embeddings arrive with P13's matching, which is where the shape of the requirement will actually be known.

**Long-term consequences.** Every AI feature inherits caching, cost tracking, quotas and audit for free — the compounding benefit is larger than the portability it was built for.

**Reversible.** Yes, but pointless.

**Migration.** Swapping providers is a settings change plus an embedding backfill (A27).

---

## ADR-A11 — Every AI output is persisted as an AIRun

**Problem.** AI output is nondeterministic, costly and opaque. Six months later, nobody can explain a result.

**Context.** Without a record there is no cache, no cost attribution, no reproducibility, no evaluation corpus and no audit answer.

**Alternatives considered.** Log to a file (rejected: unqueryable, unjoinable). Store only the final artefact (rejected: loses provider, model, prompt version, cost). Persist every run as a row (selected).

**Selected solution.** `AIRun` records task key, prompt version, provider, model, input hash, input snapshot, output, status, tokens, cost, latency and cache lineage. Every AI-derived artefact points at its run.

**Why.** One table answers "what produced this, at what cost, using which prompt, and can we do it again".

**Advantages.** Caching by input hash is free. Per-user cost attribution is a query. The evaluation corpus accumulates from day one. Regulatory answers exist.

**Disadvantages.** A high-volume table (partitioned monthly). Input snapshots contain PII and inherit the retention policy.

**Trade-offs.** Storage and a partitioning obligation for total observability.

**Long-term consequences.** This is the most load-bearing table for AI quality work. Fine-tuning, evaluation and cost control all depend on it existing from the first run, not from the day someone wishes it did.

**Reversible.** Yes technically, never advisable.

**Migration.** N/A.

---

## ADR-A12 — Soft delete is selective, with a purge schedule

**Problem.** "Soft deletes where appropriate" — and what *appropriate* means.

**Context.** A blanket `is_deleted` on every model poisons every query, breaks every unique constraint, and creates a legal problem: the user was told the data was deleted, and it was not.

**Alternatives considered.** Soft delete everywhere (rejected: the above). Hard delete everywhere (rejected: no undo for user-recoverable content; cascades destroy audit trails). Selective soft delete with mandatory purge (selected).

**Selected solution.** Only user-recoverable content carries `deleted_at` — resume documents, jobs, companies, memberships, notes, messages, pool members, tags. Each has a partial unique index excluding deleted rows and a purge job with a stated retention. Everything else deletes for real. Erasure anonymises where a record must legally survive.

**Why.** Undo is valuable exactly where a user can regret an action, and harmful everywhere else.

**Advantages.** Queries stay simple. Constraints keep working. Deletion means deletion when it must.

**Disadvantages.** Two deletion semantics; developers must know which applies. Every soft-deleted model needs a purge job written and scheduled.

**Trade-offs.** A small cognitive cost for correctness in both directions.

**Long-term consequences.** A new soft-deleted model without a purge job is a defect, caught in review.

**Reversible.** Yes, per model.

**Migration.** Adding soft delete to a model: add the column, add the partial unique index, update the default manager, write the purge job — in that order, in one release.

---

## ADR-A13 — A canonical skill taxonomy is infrastructure, not a feature

**Problem.** Skills arrive as free text from resumes, job postings and user input. "JS", "JavaScript" and "ECMAScript" are the same thing; "Django" implies "Python".

**Context.** Matching, gap analysis, missing-skill demand, employer filters and learning recommendations all read skills. If skills are strings, every one of those features is broken in a way that looks like a bad model rather than a bad schema.

**Alternatives considered.** Free-text skills with fuzzy matching (rejected: no partial credit, no implication, unstable results). Let an LLM normalise at query time (rejected: cost, latency, nondeterminism, and it still cannot join). A curated taxonomy with aliases and typed relations, seeded from ESCO/O*NET (selected).

**Selected solution.** `Skill` (canonical) + `SkillAlias` (surface forms) + `SkillRelation` (broader / narrower / related / implies, weighted). Built in Phase 2, before anything that reads skills.

**Why.** Everything downstream is only as good as this table. It is not a feature; it is the substrate features stand on.

**Advantages.** Partial credit in scoring. Stable filters. Real demand statistics. Alias handling in one place.

**Disadvantages.** Ongoing curation. Import and mapping work. Unmapped skills need a review queue.

**Trade-offs.** Editorial burden for analytical capability.

**Long-term consequences.** The taxonomy becomes a genuine asset and a differentiator. It also becomes something that must be maintained forever.

**Reversible.** No, in practice — every downstream feature assumes it.

**Migration.** Extending to a richer ontology (occupations, competencies) adds tables beside it; the `Skill` identity does not change.

---

## ADR-A14 — A transactional outbox decouples state change from side effects

**Problem.** "Send the recruiter an email when someone applies" — where does that run?

**Context.** Inline in the request: slow, and it sends even if the transaction later rolls back. In a Celery task fired before commit: it races the commit and reads a row that does not exist yet. In a `post_save` signal: invisible, unordered, untestable (A29).

**Alternatives considered.** Direct calls in services (rejected: couples every producer to every consumer; the apply service would import email, matching, indexing and analytics). Celery tasks fired inline (rejected: the race and the ghost-notification problem). A message broker as the source of truth (rejected: dual-write between database and broker has the same failure). Transactional outbox (selected).

**Selected solution.** Services write state and an `OutboxEvent` in the same transaction. A worker drains with `FOR UPDATE SKIP LOCKED` and dispatches. Processed rows are deleted after a grace period — the outbox is a queue, not a history.

**Why.** It is the only pattern where "the state changed" and "the world was told" cannot disagree, using only the database as a transactional boundary.

**Advantages.** No ghost notifications. Producers do not know consumers. New reactions are new subscribers. Retries are free and safe.

**Disadvantages.** A worker to run and monitor. Delivery latency of seconds. At-least-once delivery, so consumers must be idempotent.

**Trade-offs.** Eventual consistency in side effects for guaranteed consistency between state and effect.

**Long-term consequences.** The event catalogue becomes the system's public internal vocabulary. Changing an event payload is a contract change, which is why payloads carry schema versions (A27).

**Reversible.** Yes, but every consumer would need rewriting.

**Migration.** Moving to a real broker later: keep the outbox as the durable write, add a relay that publishes to the broker. The producer never changes.

---

## ADR-A15 — Nepal-first, global-ready

**Problem.** Which market shapes payment, region, currency and compliance.

**Context.** All seed content and the initial user base are Nepali. Building globally first would delay launch and add compliance work with no user.

**Alternatives considered.** Global-first with Stripe and GDPR obligations (rejected: slower, and solves problems the product does not yet have). Nepal-only with local assumptions baked in (rejected: cheap now, expensive at the first international user). Nepal-first behind interfaces (selected).

**Selected solution.** Local payment providers behind a `PaymentProvider` interface. NPR default with per-row currency and a normalised annual base everywhere (A24's salary comparability depends on it). Region Singapore or Mumbai. GDPR as the design standard — rights implemented — without the compliance apparatus.

**Why.** Interfaces make the international path additive; hard-coded assumptions make it a rewrite.

**Advantages.** Fast launch. International readiness costs an adapter, not a migration.

**Disadvantages.** Interfaces built before a second implementation exists — mild speculative generality, accepted because the currency and payment seams are the two most expensive to retrofit.

**Trade-offs.** A little unused abstraction for a cheap market expansion.

**Long-term consequences.** Adding Stripe is a new adapter. Adding a market is a currency, a locale and a legal review.

**Reversible.** Yes.

**Migration.** Add the provider adapter; backfill FX; add locale content.

---

## ADR-A16 — AI video interview is the last phase, behind a legal review

**Problem.** Where the most-requested AI feature sits in the build order.

**Context.** It is the most expensive to build, the least defensible legally (A9), and the least proven in predicting hires. Everything else in the roadmap has a clearer return.

**Alternatives considered.** Build it early as a differentiator (rejected: maximum risk before any validated value). Drop it (still the fallback). Defer to last with the data model shaped now (selected).

**Selected solution.** Phase 19, gated on legal review, content-only scoring. Consent already lives in the unified table, so adding it later requires no consent redesign.

**Why.** Deferring costs nothing because the surrounding architecture already accommodates it. Building it early costs a lot and may need to be withdrawn.

**Advantages.** Risk deferred; capital spent on features that convert.

**Disadvantages.** A competitor may advertise it first.

**Trade-offs.** Marketing position for risk-adjusted return.

**Long-term consequences.** If it is never built, nothing is stranded.

**Reversible.** Yes — it is a scheduling decision.

**Migration.** Add media and signal tables, enable consent scopes, publish a bias audit.

---

## ADR-A17 — Webhooks in v1; the public JSON API is deferred

**Problem.** What external interface v1 needs.

**Context.** No integrator exists yet. Webhooks are needed regardless, and they reuse the outbox and its delivery machinery.

**Alternatives considered.** Full API plus webhooks (rejected: a phase of work with no consumer). Neither (rejected: employers with an existing ATS need push). Webhooks only, API specified (selected).

**Selected solution.** Signed, retried, dead-lettered, replayable webhooks on five events. `/api/v1/` designed in the SRS and unbuilt.

**Why.** Webhooks are nearly free given the outbox; the API is not free and has no consumer.

**Advantages.** Integration story exists at a fraction of the cost.

**Disadvantages.** No mobile client and no pull-based integration until the API is built.

**Trade-offs.** Optionality deferred, cheaply reclaimable because services are the boundary (A3).

**Long-term consequences.** The first serious integrator triggers the API phase. Their needs shape it — better than guessing now.

**Reversible.** Yes, additive.

**Migration.** Add DRF over existing services.

---

## ADR-A18 — Tenancy is scoped by URL, never by session

**Problem.** How the system knows which company the user is acting as.

**Context.** Session-scoped tenancy holds one company per session. A recruiter at two companies with two browser tabs has one session; the tab that loses the race writes a note, a rejection or a job edit into the wrong company. This is a data-integrity and privacy failure, and it is the most common multi-tenancy bug in session-scoped SaaS.

**Alternatives considered.**

| Option | Rejected because |
|---|---|
| `request.session["company_id"]` | The two-tab bug. Unfixable once URLs are public |
| Company in a hidden form field | Trivially tampered; still ambiguous on GET |
| Subdomain per company (`acme.careerai.com`) | TLS, cookies, local development and shared-session complexity, for the same benefit as a path prefix |
| Path prefix `/employer/<company_slug>/` | Selected |

**Selected solution.** Middleware resolves the slug, validates an active membership, sets `request.company` and `request.membership`. A missing membership is a 404, never a 403 — a 403 confirms the company exists. The session holds only a `last_company_slug` hint.

**Why.** Tenancy belongs in the request, and the URL *is* the request.

**Advantages.** The wrong-tenant write is structurally impossible. Employer URLs are shareable inside a team. Bookmarks work. Logs and audit rows carry the tenant.

**Disadvantages.** Longer URLs. Every employer URL, view, test and redirect carries the slug. Renaming a company changes its URLs, so the slug is immutable once published.

**Trade-offs.** URL verbosity for correctness.

**Long-term consequences.** Every employer feature inherits it. Retrofitting later is a breaking URL change for live customers, which is why it is Phase 1.

**Reversible.** No, once URLs are public.

**Migration.** N/A — this is the migration, done before any customer exists.

---

## ADR-A19 — A mutable draft is the builder's write target

**Problem.** The builder autosaves. What does an autosave write?

**Context.** The frontend already implements `dirty` / `saving` / `saved_at` (`apps/resume/state.py`). Writing an immutable version per autosave produces hundreds of rows per session, makes the version list useless, and makes "restore" meaningless. Worse, v1.0 declared versions immutable while enforcing nothing, which would have quietly destroyed the guarantee in A8.

**Alternatives considered.** Autosave creates versions (rejected: the above). Autosave to the client only (rejected: loses work; undo cannot survive a reload). Autosave into a mutable draft, versions on explicit acts (selected).

**Selected solution.** `ResumeDraft` (1:1, mutable) is the only builder write target. `ResumeDraftSnapshot` is a bounded ring of 25 backing undo/redo. `ResumeVersion` is created by exactly four acts — save, analyse, apply, export — and immutability is enforced three ways: `finalized_at NOT NULL`, a `save()` guard, and a `BEFORE UPDATE` trigger that also stops a data-fix script.

**Why.** Editing and history are different activities with different lifetimes. One table cannot serve both without breaking one.

**Advantages.** Version history is meaningful. Undo is server-side and survives reloads. A8's guarantee is enforced by the database.

**Disadvantages.** Three tables where the naive design had one. Developers must know which to write.

**Trade-offs.** More concepts for correctness on the product's most sensitive object.

**Long-term consequences.** Any new authoring surface writes the draft and finalizes explicitly. The rule is short and memorable, which is why it will survive.

**Reversible.** No — applications reference versions.

**Migration.** N/A.

---

## ADR-A20 — Prompts live in version control, not in the database

**Problem.** Where prompt text lives.

**Context.** v1.0 stored prompts as editable database rows. That makes a prompt a piece of configuration editable at 2 a.m. with no review, no diff, no test run and no rollback — while behaving exactly like code, because it determines output.

**Alternatives considered.** Database-authored prompts with an admin UI (rejected: an outage waiting for a deadline). Hard-coded string literals (rejected: no version identity, so `AIRun` cannot say which prompt produced an output). Files in version control, loaded and hashed into `PromptTemplate` at deploy (selected).

**Selected solution.** Prompts are files. A deploy step loads them and records `body_sha256` and a version into `PromptTemplate`. `AIRun.prompt_version` continues to identify exactly what ran.

**Why.** Prompts are code with a text syntax. They deserve review, diffs, tests and rollback with the code that depends on them.

**Advantages.** Code review applies. Golden-file evaluation runs in CI on change (A11's corpus makes this possible). Rollback is a deploy. The audit property is unchanged.

**Disadvantages.** A non-engineer cannot tune a prompt without a deploy. Deliberate.

**Trade-offs.** Iteration convenience for reliability on the component most likely to silently degrade.

**Long-term consequences.** Prompt quality becomes a tested engineering artefact rather than folklore.

**Reversible.** Yes — reintroducing editing means a database body plus a review workflow, which is the thing being avoided.

**Migration.** Add a `body` override column, an approval workflow, and an eval gate before activation.

---

## ADR-A21 — Recommendations are computed on read; no score table

**Problem.** Whether to persist candidate×job match scores.

**Context.** v1.0 persisted them. That table is candidate × job × algorithm version — the largest and churniest object in the schema, with write amplification on every job publication, most rows read once or never. For a single candidate the whole pipeline runs in well under a second.

**Alternatives considered.** Persist all pairs (rejected: cost and churn out of proportion to value). Persist above a score threshold (rejected: same machinery, smaller; still solving a problem not yet demonstrated). Compute on read with a Redis cache, persist only what is genuinely batch (selected).

**Selected solution.** Recommendations computed on read, cached under `(profile_id, algorithm_version, embedding_model, profile_updated_at, job_generation)` so every staleness class invalidates by key construction. Persisted: `Application.match_snapshot` (history, required) and `JobAlertMatch` (batch with a fixed recipient set).

*(v1.16 correction: this line named three of the five key components while SRS §10.1 named five. The other two are not decoration. Without `job_generation` a newly published posting cannot appear in any cached feed until the candidate happens to edit their profile — stale exactly when a relevant job is posted, which is the one moment the product exists for. Without `embedding_model` a model change keeps serving the previous model's neighbours out of cache. §10.1 was right, and is what P13 built.)*

**Why.** Do not build a cache before measuring that one is needed — especially the most expensive kind.

**Advantages.** The schema's biggest operational liability never exists. Invalidation is free. Algorithm changes take effect immediately, with no backfill.

**Disadvantages.** Repeated cold reads recompute. A future "candidates who match this job" employer feed will need a materialised path.

**Trade-offs.** Some recomputation for a table nobody has to operate.

**Long-term consequences.** If measurement shows the read path is too slow, the table is added behind the same `matching.score()` interface with no caller change. The decision is deliberately cheap to reverse.

**Reversible.** Yes — this is its main virtue.

**Migration.** Add the table, write it from the same scorer, read through the same selector, keep `algorithm_version` as the cutover key.

---

## ADR-A22 — CandidateProfile is the candidate aggregate root

**Problem.** What candidate-domain tables point at.

**Context.** v1.0 was inconsistent: `CandidateSkill` pointed at `CandidateProfile`, while `Application`, `SavedJob` and pool membership pointed at `User`. Two identities for one concept produce joins in both directions and ambiguity in erasure.

**Alternatives considered.** Everything points at `User` (rejected: mixes authentication with employability; erasure must then destroy or tombstone the auth row that other domains use). Merge the profile into `User` (rejected: a fat user model; employer-visible data on the auth object). Everything candidate-domain points at `CandidateProfile` (selected).

**Selected solution.** `CandidateProfile` is created at registration and is the target of every candidate-domain relation. `User` remains the authentication identity and holds nothing about employability.

**Why.** One concept, one identity. Erasure anonymises one row and every foreign key stays valid.

**Advantages.** Consistent joins. Clean erasure. A clear boundary between authentication and domain.

**Disadvantages.** A profile row must exist before candidate actions, so registration creates it. Employer-side users have a `User` with no profile, which is correct but occasionally surprising.

**Trade-offs.** One extra row per user for a consistent model.

**Long-term consequences.** Every future candidate feature has an obvious owner.

**Reversible.** No, in practice.

**Migration.** N/A — established before data exists.

---

## ADR-A23 — Stage is the only application state

**Problem.** How an application's state is represented.

**Context.** v1.0 had four encodings of one fact: a `stage` foreign key, a `status` string, a `Rejection` table with a unique constraint, and a `JobRevision` table for the job content at apply time. Four representations of one truth diverge, and the divergence is discovered by users.

**Alternatives considered.** Keep status as a denormalised cache of `stage.kind` (rejected: a cache with no read pressure, guaranteed to go stale). Keep the rejection table for reason metadata (rejected: a whole table for one transition's payload). Stage only, with derived status and event-carried payloads (selected).

**Selected solution.** `stage` (FK) is the only state; `status` is a property of `stage.kind`. A rejection is a move to a terminal stage plus `closed_reason`, with any message in the `ApplicationEvent` payload. Job content at apply time is `Application.job_snapshot`, which deletes `JobRevision` entirely. Offers get a partial unique index on live states, because offers are revised.

**Why.** One fact, one place. Everything else is derived or evented.

**Advantages.** Cannot go inconsistent. Two tables removed. The timeline is the single source for both audiences.

**Disadvantages.** Filtering by conceptual status requires joining stages. Employer-facing job edit history is lost — judged a Phase 16 nice-to-have, not a v1 requirement.

**Trade-offs.** A join for consistency.

**Long-term consequences.** Custom pipelines per company work without special cases, because there is no parallel status vocabulary to keep in step.

**Reversible.** Yes, though reintroducing `status` would recreate the problem.

**Migration.** Adding job edit history later: a revision table beside the job, unrelated to the application snapshot.

---

## ADR-A24 — Vector retrieval branches on filter selectivity

**Problem.** Combining hard filters with approximate vector search.

**Context.** An HNSW index cannot honour arbitrary `WHERE` clauses. Filtering after an approximate search loses recall and can return nothing from a healthy corpus. A candidate in a smaller city with a narrow salary band is exactly the case that breaks, and it fails silently: an empty feed that looks like "no jobs for you".

**Alternatives considered.** Filter then ANN (rejected: the failure above — this was v1.0's design). ANN then filter with a large `k` (partially adopted, for large sets only). Partition vectors per filter combination (rejected: combinatorial). Selectivity-based strategy branching (selected).

**Selected solution.** Estimate the filtered set, then choose: exact distance over the filtered set when small (~<5k); a pgvector iterative index scan when medium; ANN with a large `k` then filter, topping up when thin, for large sets. Partial HNSW indexes on `status='published'`, because nearly every vector query filters on it. Plus a floor: below ten results, relax the softest filter and label the results as widened.

**Why.** Correct recall matters more than always using the index, and the cheapest correct strategy differs by set size.

**Advantages.** Correct results at every selectivity. Exact distance on small sets is both faster and more accurate than ANN.

**Disadvantages.** Three code paths to test. A selectivity estimate that can be wrong; thresholds need tuning against production data.

**Trade-offs.** Implementation complexity for correctness in the case users notice most.

**Long-term consequences.** Moving to a dedicated vector store later replaces the retrieval step only; scoring and explanation are untouched.

**Reversible.** Yes — retrieval is one module.

**Migration.** Swap the retrieval implementation behind `matching.retrieve()`; re-tune thresholds.

---

## ADR-A25 — Event fan-out is two-stage with a delivery ledger

**Problem.** One event can concern fifty thousand people.

**Context.** A `job.published` event in a popular category matches a large slice of the candidate base. Single-pass fan-out is one transaction writing 100,000 rows, holding locks, blocking every other event behind it in the outbox, and restarting from zero on any failure.

**Alternatives considered.** Single-pass fan-out (rejected: the above — this was v1.0's design). Fan out inline in the emitting request (rejected: worse in every dimension). Two-stage fan-out with chunked, independently retryable workers and an idempotency ledger (selected).

**Selected solution.** The outbox worker resolves recipients as a **query**, not a list, and writes `FanoutJob` chunks. Chunk workers deliver, guarded by `NotificationDelivery` with `U(event_id, recipient_id, channel)` — so a retried chunk physically cannot double-send.

**Why.** Fan-out size is unbounded and unpredictable; the design must not care how large it is.

**Advantages.** Small transactions. Partial progress survives failure. Double-send is impossible, not merely unlikely. Horizontally scalable by adding workers.

**Disadvantages.** More moving parts. The ledger is a large table needing pruning. Delivery is eventually consistent across a large fan-out.

**Trade-offs.** Complexity for an operational failure mode that would otherwise arrive on the platform's first successful marketing moment.

**Long-term consequences.** Every future channel (push, SMS) plugs into the same ledger and inherits idempotency.

**Reversible.** Yes, but reverting reintroduces the outage.

**Migration.** Moving to a broker: chunk workers become consumers; the ledger stays, because at-least-once delivery still needs it.

---

## ADR-A26 — Messaging is a first-class domain

**Problem.** Whether in-platform messaging is in v1.

**Context.** The frontend already ships an `employer_messages` preference and a `message` notification kind with a worked example. Without the domain, the settings screen governs a feature that does not exist — which violates the product rule stated in that file's own docstring. Meanwhile hiring conversations move to personal email, taking audit, consent, retention and analytics with them.

**Alternatives considered.** Defer messaging, hide the preference (rejected: removes a promised capability and still pushes conversations off-platform). Link to email (rejected: no record, no consent, no rate limiting, no candidate protection). Build the domain (selected).

**Selected solution.** `Conversation` / `Message` / `ConversationReport`, gated on a `CandidateVisibilityGrant` so there is no cold outreach, with mute, block, report and rate limits.

*(Amended in P9: this line originally listed a `ConversationParticipant` table. SRS §5.9 had already removed it — a conversation is permanently one company and one candidate, so a join table for a fixed two-party relation is generality with no case behind it. Read and mute state are columns. The SRS was the later and more specific statement; the implementation follows it and this line now agrees.)*

**Why.** The conversation is where hiring actually happens. A platform that cannot see it cannot measure, protect or improve it.

**Advantages.** Response-time metrics become real. Candidate protection is enforceable. Consent and retention cover the whole interaction.

**Disadvantages.** A phase of work. Moderation obligations. Abuse surface.

**Trade-offs.** Scope for product completeness and safety.

**Long-term consequences.** Messaging becomes a retention driver and a moderation responsibility. Both are permanent.

**Reversible.** Yes, but removing it would break promises already visible in the UI.

**Migration.** N/A.

---

## ADR-A27 — Every JSONB document and every vector carries a version

**Problem.** Schema evolution of things the ORM does not police.

**Context.** Two silent failures. A `ResumeVersion.content` shape change breaks every historical render, with no way to tell which shape a row holds. An embedding model change makes cosine distance between old and new vectors meaningless — and nothing errors; matching just quietly returns noise.

**Alternatives considered.** Never change the schema (not credible). Infer the shape by inspection (rejected: fragile and slow). Migrate all rows on change (rejected: impossible for immutable versions, which must render as they were). Explicit version columns with dispatching readers (selected).

**Selected solution.** `content_schema_version` beside every JSONB document; `embedding_model` and `embedded_at` beside every vector; `schema_version` on every event payload. Readers dispatch on it; each bump ships an upgrade function; retrieval filters on the current embedding model.

**Why.** Both failures are silent. Silent failures need structural prevention, not vigilance.

**Advantages.** Historical documents render forever. Embedding migration is a documented procedure. Event contracts can evolve without breaking consumers.

**Disadvantages.** Extra columns. Readers carry version branches. Old upgrade paths need retiring eventually.

**Trade-offs.** Small permanent overhead against two failure modes that are nearly undiagnosable in production.

**Long-term consequences.** Embedding model changes become routine: add a column, dual-write, backfill, verify, swap, drop.

**Reversible.** Yes technically, never advisable.

**Migration.** The versions *are* the migration mechanism.

---

## ADR-A28 — AI provider and model configuration lives in code

**Problem.** Where provider, model, routing and cost configuration lives.

**Context.** v1.0 had `AIProvider`, `AIModel` and `AITaskConfig` tables — three tables, an admin surface and a cache for perhaps six rows, bought to enable runtime provider swapping that nobody performs at 2 a.m. without a deploy anyway.

**Alternatives considered.** Database tables with admin editing (rejected: complexity out of proportion; an unreviewed change to model routing is a production incident). Environment variables only (rejected: cannot express per-task routing and fallback chains). A settings module of typed configuration, deployed like code (selected).

**Selected solution.** Providers, models, tiers, per-task routing, timeouts and cost ceilings are Python configuration. The database keeps only what records reality: `PromptTemplate` (identity, for audit) and `AIRun` (what happened).

**Why.** Configuration that changes model behaviour deserves review and rollback. Only the record of what ran must be queryable.

**Advantages.** Three tables and an admin surface removed. Routing changes are reviewed and revertible. No cache invalidation.

**Disadvantages.** Changing a model needs a deploy. Acceptable — deploys are cheap and the change is consequential.

**Trade-offs.** Runtime flexibility for reviewability, consistent with A20.

**Long-term consequences.** The kill switch stays a runtime `FeatureFlag`, because disabling AI during an incident must not require a deploy. That is the deliberate exception.

**Reversible.** Yes — add tables and read through the same accessor.

**Migration.** Introduce a config table, have the accessor prefer it, migrate values.

---

## ADR-A29 — Django signals are banned for business logic

**Problem.** Django offers `post_save` and `pre_save`. They are the most common way a Django codebase becomes unmaintainable.

**Context.** A signal turns one visible mutation into several invisible ones. Ordering is implicit, testing requires reproducing the whole cascade, and "why did this happen" is answered by grepping the entire repository. Combined with the outbox (A14), signals are also redundant.

**Alternatives considered.** Use signals for cross-app reactions (rejected: the above). Allow them with a review rule (rejected: unenforceable in practice). Ban them for business logic, with two narrow exceptions (selected).

**Selected solution.** `post_save` / `pre_save` may only maintain cache invalidation and denormalised fields. Every business reaction goes through the outbox, where it is visible, ordered, retryable and testable. A CI check flags new signal receivers outside the allowed modules.

**Why.** Explicit beats implicit, especially where money, permissions and candidate data are involved.

**Advantages.** Every reaction is discoverable from the event catalogue. Reactions are individually testable and retryable.

**Disadvantages.** Slightly more ceremony for a trivial reaction. A developer used to signals must learn the local rule.

**Trade-offs.** Verbosity for traceability.

**Long-term consequences.** Onboarding stays cheap: reading a service tells you what happens.

**Reversible.** Yes, and doing so would be a mistake.

**Migration.** N/A.

---

## ADR-A30 — Domain apps arrive alongside; portals move one per phase

**Problem.** How to reach the target project structure from the working codebase.

**Context.** v1.0 scheduled the restructure in Phase 0 — a large, all-at-once refactor of eight working apps before any feature exists, with no way to distinguish a restructure bug from a new bug.

**Alternatives considered.** Big-bang restructure first (rejected: risk with no reward, and it makes every later bug ambiguous). Never restructure (rejected: models and views entangle, and the target structure is the point). Incremental, one portal per phase (selected).

**Selected solution.** Domain apps are created beside the existing ones. Each phase points one portal at the new services, deletes that portal's `samples.py` and `state.py`, and moves its views. The `templates/` tree never moves.

**Why.** Eight small reversible steps, each ending with a working application, beat one large irreversible one.

**Advantages.** Continuously shippable. Each move is reviewable. A regression is attributable to one phase.

**Disadvantages.** Two structures coexist for several phases; the tree looks untidy mid-migration and needs a tracking table.

**Trade-offs.** Temporary inconsistency for continuous safety.

**Long-term consequences.** Structure is reached by the end of the roadmap rather than at its start, and nothing depended on having it early.

**Reversible.** Yes.

**Migration.** The migration table in SRS §6.3 is the plan.

---

## ADR-A31 — CSP uses nonces; `unsafe-eval` removed in P19

**Problem.** v1.0 required a Content Security Policy without `unsafe-inline`. Verification against the codebase showed that requirement cannot be met.

**Context.** `templates/partials/_theme_script.html` is inline and synchronous by design, to avoid a theme repaint. `static/vendor/alpine.min.js` uses `new Function`, so a policy without `unsafe-eval` breaks every interactive component. As written, the requirement broke either the theme or the whole UI.

**Alternatives considered.** Keep the unattainable requirement (rejected: a specification that lies). Drop Alpine (rejected: the entire frontend is built on it). Adopt `@alpinejs/csp` immediately (rejected: it restricts expression syntax, so every `x-data` in the template tree needs auditing — real work, wrongly placed in Phase 0). Nonce the inline script, accept `unsafe-eval`, schedule its removal (selected).

**Selected solution.** `script-src 'self' 'nonce-<per-request>' 'unsafe-eval'` with `object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'`. The Alpine CSP build and the removal of `unsafe-eval` are **hardening-phase** work.

**A stale phase number, corrected in P18.** This ADR said "Phase 18" from v1.0, and the v1.17 renumbering moved every phase from talent search onward up by one without touching it — so the number here pointed at billing while §20's table put the CSP work in P19. The phase *name* is now the reference, which is what §20 says phase names are for. Nothing about the decision changed.

**Closed in P19.** The app vendors `@alpinejs/csp`, which parses directive
expressions into an AST rather than handing them to `new Function`, and
`script-src` is now `'self'` plus a per-request nonce with no `unsafe-eval`.
The audit this ADR scheduled turned out to be two expressions, not seventy-five:
the template tree had been rewritten across P3–P18 into registered Alpine
components, and what remained were one multi-statement key handler and one
`closeMenus(); closeMobile()` pair. Both became component methods.

The exception is therefore closed, and the obligation it leaves is documented in
`apps/common/csp.py`: an Alpine expression may be an expression, and anything
statement-shaped belongs in `static/js/app.js`.

The same audit found the policy blocking the site's own assets — Google Fonts
against `style-src 'self'`, Unsplash against `img-src 'self' data:` — so every
page had been falling back to system fonts and rendering no photography.
Self-hosting both was the fix, and it is what §17.5's no-CDN position implied.

**Why.** An honest weaker policy enforced today beats a stronger policy that cannot ship.

**Advantages.** Real protection now — inline injection is blocked by nonces, and object and frame vectors are closed. The residual risk is named and scheduled.

**Disadvantages.** `unsafe-eval` weakens defence against a script that already achieves execution. XSS prevention consequently leans harder on template auto-escaping and input validation until the hardening phase.

**Trade-offs.** A known, documented, time-boxed gap instead of an aspirational policy.

**Long-term consequences.** The hardening phase must actually happen. It is a roadmap line item with a named prerequisite so it cannot be quietly dropped.

**Reversible.** Yes — this is a planned tightening.

**Migration.** Adopt `@alpinejs/csp`, audit every `x-data`, remove `unsafe-eval`, verify in staging.

---

## ADR-A32 — Guest uploads are stored and scanned but never processed

**Problem.** The onboarding flow lets a visitor upload a CV before creating an account.

**Context.** Nothing in v1.0 stopped the parse, analysis and embedding pipeline from running on it. That is unauthenticated, unattributable AI spend plus unauthenticated file storage and a malware ingestion path, reachable by anyone with `curl`.

**Alternatives considered.** Remove guest upload (rejected: it is a genuinely good conversion funnel). Process with per-IP limits (rejected: limits raise the cost of abuse without removing it, and the spend is still unattributable). Store and scan only; process on claim (selected).

**Selected solution.** A guest upload has a `guest_token`, no profile, and no processing. Claiming it after verification emits `resume.claimed`, which is the only trigger for parsing. Unclaimed uploads are swept after 24 hours. Per-IP upload rate limits still apply.

**Why.** Cost must be attributable to an account before it is incurred.

**Advantages.** No unattributable spend. Malware never reaches a parser. "Create your account to see your analysis" is a better funnel than showing the result and then asking.

**Disadvantages.** The visitor waits until after verification for results, which is a small conversion cost.

**Trade-offs.** Slight funnel friction for a closed abuse vector.

**Long-term consequences.** The rule generalises: no AI runs for an unauthenticated actor, anywhere, ever.

**Reversible.** Yes, and it would be a mistake.

**Migration.** N/A.

---

## ADR-A33 — Consent is one table with purposes and policy versions

**Problem.** Consent appeared in three places: legal-document acceptance, interview recording consent, and an `allow_ai_training` preference flag.

**Context.** Three mechanisms mean three audit paths, three retention rules and three chances to get revocation wrong. A regulator asks one question — what did this person agree to, when, under which text — and three tables answer it three ways.

**Alternatives considered.** Keep them separate as each is shaped differently (rejected: the shapes are the same — subject, purpose, version, timestamp, evidence). Booleans on the models that need them (rejected: no history, no revocation, no policy version). One `Consent` table (selected).

**Selected solution.** `Consent(user, purpose, policy_version, granted, granted_at, revoked_at, ip, evidence)`. Purposes cover terms, privacy, AI processing, AI training, interview transcript, audio, video and marketing. Current state is the newest row per `(user, purpose)`. Consent is never a boolean on another model.

**Why.** Consent is one concept with one lifecycle. Modelling it once makes revocation, audit and retention uniform.

**Advantages.** One audit query. Revocation works the same everywhere. New purposes need no schema change. Policy version changes are re-consent, tracked.

**Disadvantages.** A join to answer "may I do this", mitigated by caching current consent per user.

**Trade-offs.** Slight read cost for regulatory coherence.

**Long-term consequences.** Any new processing purpose is a new row type, never a new mechanism.

**Reversible.** Yes, and there is no reason to.

**Migration.** New purposes are data. A stricter regime adds columns (jurisdiction, method) without restructuring.

---

## ADR-A34 — Contact details are revealed by stage, not at application

**Problem.** When an employer sees a candidate's phone number and email.

**Context.** The default in most systems is at application, which means every applicant hands full contact details to every employer they apply to, including the ones who never open the application. Mature ATS products reveal at shortlist.

**Alternatives considered.** Reveal at application (rejected: maximum exposure for minimum need). Never reveal, message in-platform only (rejected: employers legitimately need to phone a candidate; and it would push conversations off-platform). Reveal by pipeline stage with an explicit unlock override (selected).

**Selected solution.** `PipelineStage.reveals_contact` (default: the first interview stage). A recruiter may unlock earlier with a reason. Every reveal writes `Application.contact_revealed_at`, an `ApplicationEvent` and an audit row.

**Why.** Contact details are the highest-value PII a candidate surrenders, and their exposure should track genuine hiring intent.

**Advantages.** Meaningfully less PII exposure. A candidate-visible trust feature. The reveal is auditable, so scraping is detectable.

**Disadvantages.** Recruiters used to instant contact will complain; the unlock path exists for exactly that. Serializers must be careful — one careless field addition leaks it.

**Trade-offs.** Slight employer friction for a real privacy improvement and a differentiator.

**Long-term consequences.** The serializer boundary must stay disciplined forever. A contract test asserts that candidate contact fields never appear in an employer payload before reveal.

**Reversible.** Yes — a default change — but reverting would be a privacy regression.

**Migration.** Configurable per company if enterprise customers demand it; the default stays conservative.

**P15 note — talent search is not a reveal stage.** This decision was written about the application pipeline, where the question is *when* along a process contact details appear. Talent search has no pipeline: an employer finds somebody who never applied. The rule extends without amendment — **an employer who finds a candidate in a search sees no contact details at all**, on the profile screen or in an export — and the way to reach that person is to message them or invite them to apply, both of which are acts the candidate can see and respond to.

The "one careless field addition leaks it" risk named above is why the export is an allow-list (`talent.EXPORT_FIELDS`) rather than a serialisation of the model: a phone column added later cannot leave in a spreadsheet because nobody thought about it. A test asserts the exported columns are exactly the allow-list, and another asserts no email appears in the CSV.

---

# Amendments — the P0 correction pass

The decisions below were made after P0 shipped. Three of them exist because
**implementation evidence contradicted the architecture**: a test found a private
file could receive a public URL, an inspection found a security control resting
on a store configured to lose data, and a repository check found the dependency
graph was not reproducible. None was visible while the architecture was prose.

A8, A19, A21 and A31 are amended below. Their original entries stay as written —
decision history is preserved, never overwritten.

---

## ADR-A35 — Redis is separated by role and holds no durable guarantee

**Problem.** One Redis instance and one keyspace served the application cache,
the session store and the Celery broker.

**Context.** Three consequences, discovered by reading the shipped settings
against the shipped compose file. `cache.clear()` — a routine operation when a
feature flag changes — logged every user out. A fan-out backlog could evict
session data. And most seriously, `docker-compose.yml` runs Redis with
`--save "" --appendonly no`: persistence is **disabled**, so anything in Redis is
lost on every restart, not merely under memory pressure.

**Alternatives considered.**

| Option | Rejected because |
|---|---|
| Leave one keyspace, document the risk | The risk is a security guarantee, not an inconvenience |
| Separate Redis *instances* per role | Three containers to operate for a problem that logical separation plus moving state solves |
| Enable AOF persistence | Still an fsync window, and it makes a security property depend on an ops setting nobody re-checks after an image bump |
| **Logical databases + move durable state to PostgreSQL** | **Selected** |

**Selected solution.** `/0` broker, `/1` cache, `/2` session cache, with
`SESSION_CACHE_ALIAS` bound explicitly. Sessions use `cached_db`, so Redis is a
read cache over a durable table. Single-use token state moves to PostgreSQL
(A38). The instance runs `maxmemory-policy volatile-lru`.

**Why `volatile-lru` specifically.** `maxmemory-policy` is instance-wide, not
per-database — a fact worth stating because the obvious reading of "separate
databases" implies otherwise, and designing on that assumption would leave the
original problem in place. `volatile-lru` evicts only keys carrying a TTL, which
is cache and session-cache entries, both recoverable from PostgreSQL. Broker
keys have no TTL and are therefore never evicted, so a memory spike cannot
silently drop queued work.

**Advantages.** `cache.clear()` cannot reach sessions. Each role is monitored
and flushed independently. Losing Redis entirely now costs latency and a
re-login, not correctness.

**Disadvantages.** Three URLs to configure. `cached_db` adds a database write
per session change.

**Trade-offs.** A small amount of configuration for the removal of an entire
class of failure.

**Long-term consequences.** The rule generalises and is worth stating as one:
**Redis may hold nothing whose loss changes a security or durability outcome.**
Anything that fails that test belongs in PostgreSQL.

**Reversible.** Yes — configuration.

**Migration.** Change the URLs; the token ledger migration is additive.

---

## ADR-A36 — Dependencies are locked with hashes

**Problem.** Six of seven runtime dependencies used unbounded `>=`, with no
lockfile and no hashes.

**Context.** Two installs a week apart resolved differently; CI could go red
with no commit in between; and `pip-audit` validated whichever graph happened to
be installed rather than the one that would be deployed.

**Alternatives considered.** Poetry and PDM (rejected: replace the whole
workflow for one problem). A constraints file (rejected: pins without hashes
solves half of it). pip-tools (viable, and the initial recommendation).
**uv** (selected).

**Selected solution.** `requirements.in` and `requirements-dev.in` hold intent;
`uv pip compile --generate-hashes` produces fully pinned, hash-verified files;
CI installs with `--require-hashes` and fails if recompiling produces a diff.

**Why uv over pip-tools.** The project brief's own guidance is uv unless
evidence favours something else, and here the evidence agrees: this project
develops on Windows and deploys on Linux, and uv's resolution avoids the
platform-specific wheel-hash divergence that is pip-tools' one real weakness in
exactly that split. uv was already installed on the development machine.

**Advantages.** A clean machine reproduces the graph exactly. Upgrades become a
reviewable diff. `pip-audit` now audits what will actually deploy.

**Disadvantages.** Adding a dependency is edit-`.in`, recompile, commit — and
that friction is the feature. `uv` resolves against the environment it
discovers, which on a machine with several interpreters is a real trap; the
README states which interpreter to install into.

**Long-term consequences.** The first compile immediately surfaced drift that
unbounded ranges had hidden: Django 6.0.7 → 6.1 and Celery 5.4 → 5.6.3. Both
were adopted deliberately and the suite verified against them, which is exactly
the behaviour the lock exists to produce.

**Reversible.** Yes.

**Migration.** Delete the `.in` files and unpin. There is no reason to.

---

## ADR-A37 — One correlation envelope, propagated explicitly

**Problem.** NFR-10 claimed a request identifier propagated into Celery. It did
not, and only one of the six required identifiers existed at all.

**Context.** The identifier lived in a ContextVar, which cannot cross a process
boundary — so every worker log was uncorrelated, and workers are where the
interesting failures live. The middleware also sat **last** in the chain, so
session, CSRF and authentication all logged with no identifier, including
authentication failures.

**Alternatives considered.** A logging adapter per call site (rejected: every
caller must remember). Thread-local storage (rejected: same process-boundary
problem, worse under async). Sentry scopes alone (rejected: couples correlation
to one vendor). **An explicit envelope propagated through task headers**
(selected).

**Selected solution.** A `LogContext` dataclass — request, actor, company, task,
event, AI run — in one ContextVar, emitted by the JSON formatter. Middleware
moves to index 0. `before_task_publish` copies the envelope into task headers;
`task_prerun` restores it and adds the task id; **`task_postrun` clears it**.
The outbox binds `event_id` around each consumer; `ai.run` will bind
`ai_run_id`.

**Why the clear is not optional.** Without it, a value set by one task persists
into the next task on the same pooled worker, and one user's work is logged
under another user's identifiers. That is both a debugging trap and a privacy
problem, and it is asserted by a test.

**Advantages.** A single request is traceable end to end. Absent identifiers are
omitted rather than emitted as nulls.

**Disadvantages.** Two Celery signal handlers to maintain; a payload slightly
larger per task.

**Reversible.** Yes; logging degrades, nothing breaks.

---

## ADR-A38 — Immutability permits redaction; token state lives in PostgreSQL

**Problem.** Two guarantees, both stated absolutely, both wrong at the edges.

**Context — erasure.** `ResumeVersion` was append-only, enforced by a trigger
that rejected every update, and `Application.resume_version` is `PROTECT`. The
row holds contact details and employment history. It could not be updated and
could not be deleted, so FR-1.8 — the right to erasure — was unimplementable.

**Context — tokens.** Single-use download tokens recorded consumption with
`cache.add`. Under A35's findings that record could vanish to eviction, to
`cache.clear()` or to any Redis restart, re-arming a token for a private CV.

**Selected solution, erasure.** Immutability means *the historical meaning
cannot change*, not that storage can never be touched. The trigger permits
exactly one transition — `redacted_at` null to non-null, `content` replaced by a
tombstone — and requires every other column to be byte-identical. Redaction is
itself single-use. `sha256` survives, so the platform can still prove what was
submitted while holding none of it.

**Selected solution, tokens.** A `ConsumedToken` ledger in PostgreSQL, with a
unique constraint on `(jti, purpose)`. Concurrency is resolved by the constraint
rather than a lock: two simultaneous requests both attempt the insert, exactly
one wins.

**Why the database rather than a better-configured Redis.** A security decision
belongs where the transaction is. The failure mode is also the right one: if
PostgreSQL is down nothing is served at all, so there is no window in which a
token can be replayed. The download path already runs several queries and writes
an access row, so one indexed insert is not a meaningful cost.

**Advantages.** Erasure becomes implementable without weakening the evidentiary
guarantee. Token replay is impossible by constraint, not by convention.

**Disadvantages.** A table to purge. One write on the download path. The trigger
is more complex than "reject everything", and its correctness now matters more —
which is why it is asserted from both directions in tests.

**Long-term consequences.** The generalised rule: **an absolute guarantee that
makes a legal obligation impossible is not a guarantee, it is a defect.** Carve
the exception deliberately, narrowly, and with the same enforcement as the rule.

**Reversible.** No, in practice — applications depend on both properties.

---

## ADR-A39 — Salary normalisation stores FX provenance

**Problem.** Normalised annual figures were computed "from the rate current at
that moment", with no record of which rate.

**Context.** A historical match could not be reproduced or explained, and the
matcher's salary component is one of six scoring inputs a candidate or employer
may reasonably ask about.

**Selected solution.** Every salary-bearing model stores `fx_rate_id`,
`fx_rate_value`, `fx_as_of` and `fx_source` alongside the normalised amount.
`FxRate` becomes append-only — a rate is superseded, never mutated.

**Why the rate value is denormalised too.** The FK alone is not enough if the
rate table is ever pruned or re-sourced; the figure that was actually used must
survive independently of the row it came from.

**Reversible.** Yes, but reverting reintroduces unexplainable history.

---

## ADR-A40 — Partitioning converts at a size threshold, not on a phase schedule

**Problem.** v1.1 required monthly partitioning "from day one" for five
high-volume tables. P0 shipped them unpartitioned and asked for a ruling.

**Context.** Django has no declarative partitioning: it needs raw DDL,
complicates primary keys and restricts inbound foreign keys. The benefit is
proportional to table size, and at MVP scale these tables hold thousands of rows.

**Selected solution.** Tables stay **partition-ready** — no inbound foreign
keys, `created_at` in every index, both already true — and convert when any one
passes **20M rows**, with an alert at 15M.

**Why a threshold rather than a phase.** The cost of conversion depends on data
volume, not on the calendar, so the trigger should too. Conversion is a short
maintenance window while a table is small and effectively impossible while it is
large; 20M sits well below that crossover.

**Disadvantages.** Someone must watch the threshold. Mitigated by an alert
rather than a diary entry.

**Reversible.** Yes — this is a deferral with a named trigger, not a refusal.

---

## ADR-A41 — Rate limiting is fixed-window with a per-rule failure policy

**Problem.** The SRS described rate limiting; the repository had none.

**Alternatives considered.** `django-ratelimit` (rejected: a dependency larger
than the ~80 lines it replaces, and the domain limits are custom regardless).
A sliding window (rejected: a sorted set and a cleanup path for no security gain
at these numbers). API-gateway limiting (rejected: no gateway, and it cannot see
identity). **Fixed-window counters in the cache** (selected).

**Selected solution.** A `Rule` dataclass — name, limit, window, failure policy —
with counters keyed on a hash of the identifier, so no email address ever
appears in a cache key. Every sensitive rule is enforced per-identity *and*
per-IP.

**The decision inside the decision.** Failure policy is per rule.
Authentication-shaped limits **fail closed**: if the cache is unavailable,
refusing a login attempt is safe. Browse-shaped limits **fail open**: taking job
search down because a cache is missing is self-inflicted damage. A single global
policy would be wrong in one direction or the other.

**Disadvantages.** A boundary-straddling burst can reach 2× the limit. For "5
logins per 15 minutes" that is 10 attempts in an instant — not a meaningful
attack, and the honest price of not building a sliding window.

**Reversible.** Yes — one feature flag disables enforcement.

---

## ADR-A42 — Multi-factor authentication uses django-otp

**Problem.** TOTP needed an implementation.

**Context.** RFC 6238 is HMAC-SHA1 over a time counter, and the arithmetic is
about thirty lines against `hmac` in the standard library. The project has a
demonstrated preference for small in-tree implementations over dependencies —
`uuid7`, the file sniffer, the rate limiter — and the RFC publishes test
vectors, so an in-tree version would have been verifiable rather than merely
trusted. That was the initial recommendation.

**It was the wrong call, and the owner overruled it.** Authentication is the one
area where community review is worth more than one fewer dependency. An
in-tree TOTP would have been correct on the day it was written and then
unmaintained: no drift tolerance tuning, no replay window, no recovery-code
plugin, no security advisories, and every future reviewer would have to audit
cryptographic-adjacent code that a library already gets right.

**Alternatives considered.**

| Option | Rejected because |
|---|---|
| In-tree RFC 6238 | Unreviewed authentication code, and it would still need device models, drift handling and recovery codes built on top |
| `django-two-factor-auth` | Brings its own views, URLs and templates — an opinionated flow that would fight the existing design system |
| `pyotp` | Only the algorithm. Device storage, confirmation state and recovery codes would still be ours |
| **`django-otp`** | **Selected** |

**Selected solution.** `django-otp` with the `otp_totp` and `otp_static`
plugins. It supplies the device model with drift tolerance and replay
protection, recovery codes as static tokens stored hashed, and `OTPMiddleware`,
which makes `request.user.is_verified()` a real question. It is the long-standing
Django MFA library, is narrow in scope, and brings no view layer to fight.

`apps/accounts/services/mfa.py` wraps it with the two rules the library does not
enforce: a device is unusable until **confirmed** by entering a code, so an
abandoned enrolment cannot lock anyone out; and disabling MFA requires a valid
code, so a stolen session cannot quietly remove the second factor.

**Advantages.** Reviewed by more people than this project will ever have.
Recovery codes come free. Security advisories arrive through the dependency
audit that already runs in CI.

**Disadvantages.** One more runtime dependency and three more migrations. A
device model whose shape is not ours to choose.

**Trade-offs.** A dependency, deliberately, in the one place where the argument
for writing it ourselves was weakest.

**Long-term consequences.** SRS §5.2's `MFADevice` is not built: `TOTPDevice`
and `StaticDevice` replace it. Anything that needs to know whether a user has a
second factor asks `apps.accounts.services.mfa`, never the device tables.

**Reversible.** Qualified — swapping libraries would mean migrating device rows,
and users would need to re-enrol if the secret format differed.

**Migration.** N/A.

## ADR-A43 — Skills retire by status; merge and deprecation are different facts

**Problem.** P2 needed a way for a skill to leave service. The obvious one —
delete the row — is unavailable, and the obvious replacement — a single
`is_canonical` flag with a `merged_into` pointer, which is what the first cut
of the taxonomy shipped — turned out to be modelling two unlike things as one.

**Context.** `Skill` is referenced by aliases, translations, external ids,
relations and, from P3, by every `CandidateSkill` and job requirement. Deleting
one cascades those away and makes a curation mistake unrecoverable.

But "this row is a duplicate" and "this technology is obsolete" are different
claims about the world:

  * **Merged.** The two rows always meant the same thing. "Postgre SQL" and
    "PostgreSQL" are one skill someone typed twice. Folding one into the other
    loses nothing, because there was never a distinction to lose.
  * **Deprecated.** The skill was genuinely distinct and is now obsolete.
    **AngularJS was never Angular** — different framework, different API, and a
    developer who listed AngularJS did not list Angular. Recording it as an
    alias of Angular would silently rewrite what their profile claims.

A single flag cannot express the second case. Under `is_canonical=False` +
`merged_into=angular`, AngularJS becomes indistinguishable from a typo, and the
information that a candidate's experience is with the older framework is gone
from the system entirely.

**Decision.** `Skill.status` is `active | deprecated | merged`, with
`replaced_by` (self FK, `PROTECT`) carrying the forward pointer and a database
`CHECK` keeping the pair coherent: an active skill forwards nowhere, a merged
skill must forward somewhere, a deprecated skill may or may not — sometimes a
technology simply stops being asked for, and Adobe Flash has no successor.

Three consequences follow:

1. **Resolution always returns the skill in service today**, following
   `replaced_by` through a bounded, cycle-safe walk, so a profile written three
   years ago still scores against current requirements.
2. **The substitution is visible.** `Match.was_retired` is returned to the
   caller, so an interface can say "recorded as Angular, which replaced
   AngularJS" rather than quietly changing what someone typed.
3. **Merged skills cannot be reinstated; deprecated ones can.** Un-merging would
   split data since written against the survivor. Un-deprecating is just an
   opinion changing.

Deletion is not forbidden outright — a skill nothing references is a typo and
may go — but the admin refuses it for anything referenced, and bulk delete is
removed because it cannot explain which row is protected or why.

**Alternatives considered.**

*Keep `is_canonical` + `merged_into`.* Two facts in one flag, as above. It was
what P2 started from, and the AngularJS case is what killed it.

*Delete and repoint foreign keys.* Fast to describe, impossible to audit: the
`CandidateSkill` rows change meaning with no record that they did.

*A separate `SkillRetirement` table.* One row per skill at most, always joined,
and the `CHECK` constraint tying status to replacement becomes unenforceable.

**Consequences.** Every read that offers skills for *new* records must use
`Skill.objects.active()` — `selectors.active_skills()` is the single place that
does. Reads of *existing* records must not filter, or historical data
disappears from view. The distinction is the price of keeping the history
honest.

**Reversible.** Qualified — the states are additive over A13's substrate, but
data written against them (which skills were retired, and why) would be lost.

**Migration.** `apps/taxonomy/migrations/0001_initial.py`. The app was
unreleased when this landed, so the initial migration was regenerated rather
than amended.

---

## ADR-A44 — External taxonomies arrive through a source interface

**Problem.** The curated seed is ~110 skills. The real taxonomy is ESCO
(~13,000) or O*NET, and later a national classification. The first cut coupled
the import command directly to an ESCO-shaped CSV and wrote an `esco_uri`
column, which makes the second source either overwrite the first or grow a
parallel field.

**Context.** An import writes the table that every match in the system reads.
Three failure modes matter more than throughput: running it twice, killing it
halfway, and an export that collides with data a curator maintains by hand.

**Decision.** Three separations.

*Identity is pluggable.* `SkillExternalId` holds `(source, external_id, uri)`
with a uniqueness constraint per source, so ESCO and O*NET coexist. Skills are
keyed on that pair, not on a name, so an upstream rename updates a skill rather
than forking it.

*Reading is a source, writing is a runner.* `TaxonomySource.records()` yields
`SkillRecord`s; the runner knows nothing about files, and a new source is one
class and one registry line. The source contract carries the two rules that make
resumption meaningful: stable external ids, and ascending order.

*Progress is durable.* Work commits in batches, each batch checkpointing the
last id it wrote into `ImportRun.last_key`. Hierarchy is linked in a **second
pass** over the same source, because a parent can appear after its child under
any ordering keyed on id — so `ImportRun.phase` records which half a resumed run
was in. Both phases are idempotent, which is what makes resuming a few rows
early harmless.

**Imports fill blanks; they never overwrite curation.** No renames, no
overwriting a description or category a curator set, and no stealing an alias
another skill already claims. A source that can rewrite the taxonomy is a source
that can break matching platform-wide with one bad export, and the blast radius
is every candidate and every job at once.

**Alternatives considered.**

*Keep the ESCO-coupled command.* Rejected on the owner's explicit instruction,
and independently by the `esco_uri` column problem.

*Load everything into memory, then write in one transaction.* One transaction
means no checkpoint, so a failure at row 29,000 costs the whole run — and it
holds a write transaction open for minutes against the hottest reference table
in the system.

*Store deferred hierarchy edges in a table.* A third table to avoid a second
read of a file we already have. The phase flag costs one column.

**Consequences.** A source must be re-iterable, which rules out a one-shot
stream unless it can be reopened. `CsvSource` sorts in memory by default (tens of
thousands of rows is nothing) and streams when told the file is pre-sorted.

**Reversible.** Yes — the interface is internal, and no external contract
depends on it.

**Migration.** `apps/taxonomy/migrations/0001_initial.py` (`ImportRun`,
`SkillExternalId`).

---

## ADR-A45 — Field-level encryption is one primitive, with rotation built in

**Problem.** DEVELOPER.md §6.11 has required field-level encryption for phone
numbers since P0. P3 created the first column that needs it.

**Context.** A candidate's phone number is the one contact detail this platform
holds that is directly actionable by whoever reads it, and ADR-A34 exists
because employers must not see it before a stage unlocks it. Column-level
encryption is what keeps that promise across a database backup, a read-replica
snapshot, or a support engineer with `psql` — none of which the application's
authorization layer is present for.

**Decision.** One `EncryptedCharField` in `apps/common/crypto.py`, over
`cryptography`'s Fernet, keyed by `FIELD_ENCRYPTION_KEYS` — a **list**, newest
first: the first key encrypts, every key decrypts. Rotation is prepend a key,
re-save the rows, drop the old key, with no window where data is unreadable.

Three deliberate consequences:

1. **Encrypted columns are unsearchable.** Fernet output is randomised, so no
   equality lookup, no index, no uniqueness. A searchable encrypted phone
   number is a decrypted phone number with extra steps; if the need arises it
   is a separate blind index, not a weakening of this field.
2. **Empty stays empty.** Encrypting `""` produces a non-empty ciphertext, and
   every `if not profile.phone` in the codebase would quietly become false.
3. **A missing key fails the deploy, not the first save.** `common.E001` is a
   system check, because the alternative symptom is a 500 in production on a
   real person's data.

**Why a dependency.** `cryptography` is the audited primitive library every
Python project uses for this. An in-tree AES wrapper would be exactly the
custom cryptographic code this project already decided against once (ADR-A42),
and the argument has not changed.

**Alternatives considered.**

*`django.core.signing`.* Already a dependency, and not encryption — signed
values are readable by anyone holding the payload. It answers a different
question.

*Database-level (pgcrypto) encryption.* The key ends up where the data is, so a
database backup carries both. Managed encryption at rest stays in place; this
sits above it.

*Encrypt nothing; rely on access control.* That is the status quo this replaces,
and it fails precisely in the cases access control is not running: backups,
replicas, exports.

**Consequences.** A dropped key makes affected rows unreadable and the field
raises rather than returning ciphertext — loud, because returning it would put
a base64 blob on screen where a phone number belongs. Any future encrypted
column inherits all of this by using the same field.

**Reversible.** Qualified — decrypting back to plaintext is a data migration,
not a code change.

**Migration.** `apps/candidates/migrations/0001_initial.py` (the column is
`TEXT`; the plaintext `max_length` is a form-level constraint).

---

## ADR-A46 - Unrecognised terms are recorded, not discarded

**Problem.** Resolution refuses to create a skill per typo (ADR-A13), which is
correct. The consequence, until now, was that the term itself vanished.

**Context.** That term is the only evidence a skill is missing. Fifty
candidates typing "Kubeflow" is the strongest possible argument for adding
Kubeflow - and it is an argument that cannot be reconstructed afterwards from
any other table. Emerging-skill tracking, missing-skill demand and the curation
backlog all read the same data, and all three are permanently poorer for every
week it is not collected. This is the "cheap now, impossible later" case in its
purest form.

**Decision.** `UnresolvedTerm`: one row per (normalised, locale), counted, with
per-channel counts in `source_counts`, a status, and the skill a curator
eventually mapped it to. Written from candidate profile edits today; resume
parsing, job import and AI extraction pass their own `TermSource`. An admin
queue ordered by demand resolves a term onto an existing skill, and **by
default writes the alias at the same time** - resolving without teaching
resolution leaves the next person hitting the same wall and the queue refilling
with a question already answered.

Two properties are load-bearing:

1. **Recording never raises into the caller.** A profile save must not fail
   because a bookkeeping row could not be written.
2. **Recording is not in the caller's transaction.** `add_skill` deliberately
   resolves *before* opening its atomic block: a single transaction would roll
   the ledger row back with the refusal that follows it, which is exactly the
   data loss this ADR exists to stop.

Rejected terms are kept, not deleted. "asap" and "team player" arrive thousands
of times, and a rejected row stops the same judgement being made again every
week.

**Alternatives considered.**

*Log it and grep later.* Logs have a retention measured in days and no
aggregation. The first question anyone asks is "how often", which a log answers
badly and a counter answers exactly.

*Auto-create skills below a similarity threshold.* This is the failure the
taxonomy is designed to prevent, reintroduced with a threshold on it.

*A row per occurrence.* Unbounded growth for data whose only use is a count.

**Consequences.** The queue needs curation, and an uncurated queue grows. That
is a staffing question, not a design flaw - and an unread queue is still
strictly better than no record.

**Reversible.** Yes.

**Migration.** `apps/taxonomy/migrations/0003_...`.

---

## ADR-A47 - Every alias and relation records its provenance

**Problem.** P13 will infer relations from co-occurrence and P4-P6 will extract
aliases from resumes. Nothing distinguished a curated assertion from a guessed
one, and nothing identified the rows one run wrote.

**Context.** An inference batch with a bad threshold can write thousands of
wrong edges. Without provenance the only options are to leave them or to delete
edges by shape and hope, which also deletes the curated ones that happen to look
similar. "Reversible" is not a property you can add after the rows exist: by
then nothing says which rows they were.

**Decision.** `ProvenancedModel` on `SkillAlias` and `SkillRelation`: `source`
(curated | imported | inferred | ai_generated | manual), `confidence`,
`created_by`, `created_reason`, `batch`. `services.skills.add_relation` is the
only writer outside the seed and the importer, and it takes all of them.

`revert_batch(batch=...)` deletes the batch's **inferred and ai_generated** rows
only. Curated and imported rows carrying the same batch id survive and are
reported in the `kept` count. An undo that deletes someone's deliberate work is
worse than no undo, and finding out from a count beats finding out from a gap in
the taxonomy six months later.

**Alternatives considered.**

*A separate audit table.* `AuditLog` already records the *event*; it cannot
answer "which rows are currently inferred", which is a query on the rows.

*A boolean `is_inferred`.* Three of the five sources would collapse into one,
and there would still be nothing to revert by.

*Version the whole taxonomy.* Enormous, and the operation people actually want
is "undo that one batch".

**Consequences.** Every writer must supply provenance; the default is `curated`,
which is right for hand edits and wrong for anything automated, so automation
must be explicit. Admin stamps `created_by` from the request rather than asking.

**Reversible.** Yes - the columns are additive.

**Migration.** `apps/taxonomy/migrations/0003_...`. Existing rows default to
`curated`, which is accurate: everything that existed came from the seed.

---

## ADR-A48 - Skills carry a kind, independent of their category

**Problem.** `SkillCategory` answers "which part of the world is this from".
Nothing answered "what sort of thing is this".

**Context.** The matcher will weight a missing programming language differently
from a missing methodology; gap analysis groups by kind; clustering and learning
recommendations both need it. The cost of adding the column late is not the
migration - it is classifying tens of thousands of imported rows by hand
afterwards, which nobody funds. Classified at import time, it is free.

**Decision.** `Skill.kind`, a `TextChoices` of twelve values plus `unknown`,
extensible by adding a member. The seed classifies every skill via a
category-default map with per-skill overrides (Docker is a tool, not a platform;
pandas is a library, not a technology). Imports may *set* a kind on a skill that
is still `unknown` and may never **re**-classify one: a wrong confident label is
worse than an honest blank, because a curator can find "unknown" and cannot find
"confidently mislabelled".

**Alternatives considered.**

*Infer kind from category.* Wrong for exactly the cases that matter - Python and
Django share a category and are a language and a framework.

*A many-to-many of tags.* A skill has one kind; a set invites "framework and
library and tool", which no consumer can weight.

**Consequences.** New skills default to `unknown` and need curation. The admin
filters by kind so that backlog is visible rather than theoretical.

**Reversible.** Yes.

**Migration.** `apps/taxonomy/migrations/0003_...`.

---

## ADR-A49 - Extraction proposes; it never writes the profile

**Problem.** P4 reads a CV into structured data. The obvious next step - write
it onto the candidate's profile - is wrong, and it is wrong in a way that only
shows up later.

**Context.** A CV is evidence of what someone wrote *at some point*. It is not
authority to change the record the matcher reads. Applying a parse directly
means a two-year-old phone number silently replaces a current one, an employer
the candidate has since left reappears, and a keyword-stuffed skills line adds
forty skills nobody claimed. All three are invisible: the candidate never
opted in, so they never look.

The reconciliation protocol in SRS §5.5.3 already states the rule - "extraction
proposes; the candidate accepts; the profile is canon" - but nothing enforced
it, because until P4 there was no extraction.

**Decision.** `ResumeImportProposal`: one row per suggested change, with the
payload in the shape the receiving service takes, the evidence line it came
from, and a state. Accepting calls **the same service the profile screen
calls** - `candidates.services.skills.add_skill`, `sections.add_entry`,
`profile.update_basics` - so an imported skill goes through taxonomy resolution
exactly like a typed one and an imported role gets the same date validation.

Consequences that follow from that choice rather than being bolted on:

  * An unresolvable extracted term lands in the unknown-term ledger (ADR-A46)
    like any other, because it travels the same path.
  * A proposal already true on the profile is never offered, so the list is
    things to decide rather than things to skim.
  * Re-parsing **supersedes** old offers rather than deleting them: a proposal
    someone rejected must not reappear looking new.
  * Email is never proposed at all. It is the account's identity and changes
    through the verified-email flow, not through a line in a document.

`accept_all` reports `{applied, failed}` instead of raising: one role with an
unreadable date must not stop the other fourteen.

**Alternatives considered.**

*Apply automatically, let them undo.* Undo requires noticing. The profile is
read by matching, not by its owner, so the wrong version can be live for months.

*Apply high-confidence items only.* The confidence is the parser's opinion of
itself. A regex is confident about an email address it matched and has no
opinion about whether the candidate still works there.

*Write to a staging profile and diff.* The diff is this table with more steps.

**Consequences.** More clicks than an importer that just writes. That is the
product decision: the candidate's profile is the one record they must trust.

**Reversible.** Yes.

**Migration.** `apps/resumes/migrations/0001_initial.py`.

---

## ADR-A50 - ReportLab and python-docx, and what "tagged" honestly means

**Problem.** P6 needs deterministic PDF and DOCX output on a 4 vCPU host
(SRS 17.5), running identically on a Windows development machine and a Linux
container, with FR-3.11 asking for tagged PDFs at priority S.

**Context.** The obvious candidate is WeasyPrint: it renders the HTML and CSS
the preview already uses, and recent versions emit PDF/UA structure tags. It
also needs Pango, Cairo and HarfBuzz — system libraries that make the render
container a different image from the web container and that do not install
cleanly on the development platform this project is actually written on. A
renderer that cannot be run locally is a renderer nobody can debug.

**Decision.** ReportLab for PDF, python-docx for DOCX. Both are pure Python:
no system packages, one image, and the same bytes on every machine.

Two consequences are load-bearing rather than incidental:

  * **Determinism is configured, not hoped for.** `rl_config.invariant` fixes
    the PDF's creation date and document id; the DOCX zip is rewritten with a
    fixed timestamp per entry. Without both, two renders of one document differ
    in bytes, the cache never hits, and ADR-A8's "SHA-256 of the rendered file
    submitted" identifies nothing.
  * **Layout is decided once**, in `rendering/layout.py`, and each renderer only
    decides how a block looks. Two independent layout passes drift inside a
    phase, and the symptom is a PDF and a Word file of one CV that disagree.

**On FR-3.11.** ReportLab's open-source build does not emit PDF/UA structure
elements, so **tagged output is not claimed and not implemented**. What is done
is the part that helps a real screen reader today: extractable text in reading
order, document metadata, a document language, and no content locked in images.
FR-3.11 is priority S (v1.1) and stays open, with two honest routes when it is
scheduled - a licensed ReportLab Plus, or WeasyPrint confined to the render
container where its system dependencies cost nothing.

**Alternatives considered.**

*WeasyPrint.* Rejected on the operational grounds above, not on quality. It
remains the best answer for tagging when tagging is scheduled.

*Headless Chromium.* Renders the preview exactly, and costs a browser per
render on a host sized for the whole application.

*A hosted PDF API.* A CV is the most sensitive document this platform holds;
sending every one to a third party to gain a page break is a poor trade.

**Consequences.** Layout is written in Python rather than shared with the HTML
preview, so a template change is made twice. That is the price of not shipping
a browser, and the shared layout module keeps it to one place per renderer.

**Reversible.** Yes - the renderer is behind `rendering.engine.register`, and a
format's implementation can be replaced without touching the service, the cache
or the model.

**Migration.** `apps/resumes/migrations/0004_resumerender.py`.

---

---

## ADR-A51 - `search_vector` is a generated column, and P7 adds no revision table

**Problem.** P7 needs two things the SRS names but does not settle mechanically:
a full-text column that can never disagree with the row it describes, and an
answer to "what did this posting say when somebody applied to it".

**Context.** The usual way to maintain a `tsvector` is a `BEFORE INSERT OR
UPDATE` trigger, or a `save()` override. Both are a second place where the
truth lives: a trigger can be dropped by a migration nobody read, a `save()`
hook is bypassed by every `queryset.update()` in the codebase, and the failure
mode of either is silent — a job that is simply unfindable by the words printed
on it, with no error anywhere.

Separately, an earlier draft of the schema carried a `JobRevision` table so an
applicant could see the version of a posting they applied to. That table has to
be written on every edit, pruned on some schedule, and joined on every read
that wants "as it was" — and P8 already stores `Application.job_snapshot` for
exactly the row that needs it.

**Decision.** Two parts.

  * **`search_vector` is `GENERATED ALWAYS ... STORED`**, expressed as Django's
    `GeneratedField` over `SearchVector("title", "description", config="english")`.
    Postgres computes it on every write, including bulk `UPDATE`, and no
    application code may set it. The GIN index is on the generated column.
  * **There is no `JobRevision` table.** Postings carry `row_version` for
    optimistic locking and nothing else; the "what did it say then" requirement
    is met by the per-application snapshot in P8, which is scoped to the row
    that needs it and cannot drift from it.

**Consequences.** The vector's contents are fixed at the schema level: adding a
field to it is a migration, not a code change, and changing the text-search
configuration rewrites the column. That is the intended trade — a generated
column is the only version of this that cannot silently fall behind.

The cost of dropping `JobRevision` is that an employer cannot browse the edit
history of a posting. That history is not a requirement anywhere in the SRS,
and the audit log already records that an edit happened, by whom, and when.

**Status.** Accepted, P7.

## ADR-A52 - Extra readers of a private file are a row, not a branch

**Problem.** P9 needs a message attachment to be readable by the person it was
sent to. `files.policies.can_access` grants a private attachment to its
uploader and nobody else, which is exactly right for a CV and useless for a
file somebody deliberately shared.

**Context.** Three ways to fix it. Loosen the visibility — rejected, because a
CV attached to one message would become readable in a way its owner never
chose. Teach `files.policies` about conversations — rejected twice over: it
points a platform app at a domain app, and it grows a branch per domain, each
one silently wrong when the domain's rules change. Widen `COMPANY` visibility
to cover the candidate — rejected, the candidate is not a company member.

**Decision.** `AttachmentGrant(attachment, user, reason, expires_at,
revoked_at)`. The domain that shares a file writes a row saying who may read it
and why; `files.policies` asks "is there a live grant" and never learns what a
conversation is. `files.services.share_with` / `revoke_share` are the only way
to create and withdraw one, and both are idempotent per reason.

This is the shape INV-4 already established for `CandidateVisibilityGrant`:
access is a record with a reason attached, not an inference.

**Consequences.** Sharing is auditable and revocable — deleting a message takes
back exactly what it granted, and only for the reason it granted. A grant
cannot override the scanner or a deletion: `is_servable` is checked first, so
an infected or deleted file is readable by nobody regardless of grants.

The cost is one more table and one more query on the private-file path. The
query is indexed on `(user, attachment)` and runs only after the uploader check
fails, so the common case is unchanged.

**Status.** Accepted, P9.

## ADR-A53 - A webhook signing secret is encrypted, not hashed

**Problem.** P10 stores a per-endpoint secret and signs every outbound delivery
with it. The reflex — the one the first draft of this code followed — is to
hash it like a password.

**Context.** That reflex is wrong here, and the wrongness is not subtle. A
password is **verified**: the server compares a presented value against a hash
and never needs the original. A signing key is **used**: the server computes an
HMAC with it on every request it sends. Hash it and there is nothing left to
sign with — the `X-CareerAI-Signature` header becomes a constant, and a
receiver dutifully verifying it verifies nothing at all. That is worse than
shipping no signature, because it manufactures confidence.

**Alternatives considered.** Sign with the hash (rejected: the hash is then the
secret, stored in the clear, with extra steps). Keep a second plaintext copy
elsewhere (rejected: two sources of truth for one credential). Ask the company
to re-supply the secret on every delivery (not a thing that can happen).

**Decision.** `WebhookEndpoint.secret` is an `EncryptedCharField` (ADR-A45,
Fernet with key rotation). The plaintext is recoverable by the platform because
the platform must recover it; a leaked database still yields nothing without
the encryption key, which is held separately.

The screen still treats it as show-once — the value appears after creation and
after a rotation and at no other time — because a page that reprints a signing
key on every load is a page somebody eventually screenshots into a support
ticket. That is a UI decision, not a storage one, and the two are now
separately justified rather than conflated.

**Consequences.** Rotation is a real operation: it replaces the stored value
and every signature made with the old one stops verifying, which is tested.
Key rotation for the encryption key itself is A45's existing procedure and
covers this column with no extra work.

**Reversible.** Yes — but only towards *more* protection (an HSM or a KMS),
never towards hashing, which cannot support the operation at all.

**Status.** Accepted, P10.

## ADR-A54 - A plan's limits are a versioned document, enforced by one service

**Problem.** P18 has to answer "may this account do this?" for a handful of
commercial limits — analyses per month, Pro templates, download formats, the AI
budget — and the answer has to be reachable from every service that could be
the thing somebody was refused.

**Context.** Two obvious shapes, both bad. A boolean column per feature on
`Plan` means a migration every time commercial terms move, and commercial terms
move considerably more often than schemas should; six months in, the table is a
graveyard of `allow_` columns nobody dares drop. Hard-coding the limits in
Python means a price change is a deploy, which is the thing a `Plan` model
exists to avoid.

The third temptation is worse than either: enforce in the template. Hide the
Pro cards from a free account and the problem appears solved, right up until
somebody posts the form directly — and the builder *does* post a template slug,
and the export endpoint *does* take a format.

**Alternatives considered.** A column per entitlement (rejected: migrations for
commercial changes). Limits in code (rejected: a price change becomes a
deploy). A free-form JSONB with no vocabulary (rejected: a typo in a limit key
is indistinguishable from "unlimited", which is the failure where a paid
feature is quietly free for a year and nothing alerts). Template-level gating
(rejected: not a limit at all).

**Selected solution.** `Plan.limits` is a JSONB document carrying its own
`schema_version` (ADR-A27, §5.16 rule 2). Every key it may contain is declared
in `apps/billing/limits.py` with its kind, its reset period and **the sentence
somebody reads when they hit it**; a plan naming an undeclared key is refused
when it is written, not when a candidate first hits it. Exactly one module
reads the document — `billing.services.entitlements` — exposing `allows()`,
`require()`, `consume()` and `remaining()`. A view may call `allows()` to
decide whether to render an upgrade prompt; the refusal itself is always
`require()`, inside a service.

**Why.** The exit criterion for the phase is one sentence — *a plan limit is
enforced in a service, never in a template* — and this is the shape that makes
it checkable rather than aspirational. Every gate is a service-level test with
no request object in it.

**Advantages.** Commercial terms are a row edit. The refusal wording lives
beside the limit rather than in whichever view caught the exception.
`PlanLimitExceeded` is 402, distinct from `RateLimited`'s 429, so "this costs
money" and "slow down" are different answers — which they are, and only one of
them is fixed by waiting. The pricing page is generated from the same
documents, so a limit cannot be advertised without being enforced.

**Disadvantages.** A JSONB document is not type-checked by the database; the
vocabulary module and its validation are doing work a column would have done
for free. Reading a limit is a query for the plan rather than an attribute
access, which matters if it ever lands in a loop.

**Trade-offs.** A small runtime vocabulary check in exchange for commercial
terms that move without a migration.

**Long-term consequences.** New limits are added in one file and enforced in
one call. A second entitlement mechanism appearing anywhere is a bug, not a
style difference.

**Reversible.** Yes. Promoting a stable limit to a column is a migration and a
one-line change in the reader.

**Status.** Accepted, P18.

---

## ADR-A55 - Payment providers sit behind two methods, and bank transfer is one of them

**Problem.** ADR-A15 says Nepal-first: eSewa, Khalti and Fonepay behind a
`PaymentProvider` interface, with Stripe added on market entry rather than
swapped in. P18 has to make that a real interface rather than a sentence.

**Context.** Two things are true at once. The interface has to be small enough
that three local gateways and one international one all fit it honestly — the
mistake ADR-A10 had to correct for AI adapters, where a required method half
the implementations raised `NotImplementedError` from had stopped describing
anything. And the platform has no merchant account yet, so any adapter written
today is code whose correctness nobody has confirmed, sitting behind a button
that takes money.

There is also a fact about the market that a Stripe-shaped design would erase:
a company here pays for a hiring plan by bank transfer and emails the receipt.
A billing system that cannot represent that has not modelled the market it
launched in.

**Alternatives considered.** A fat protocol with refunds, stored cards and
subscription objects (rejected: no local gateway implements all of it, and A10
already taught this lesson). Write the eSewa and Khalti adapters now against
their published specs (rejected: unverifiable against a live merchant account,
and shipping unverified payment code is the "never fake functionality" line).
Treat manual settlement as a temporary hack in the admin (rejected: it is the
normal path here, and a hack does not emit the events or write the audit rows
the real path does).

**Selected solution.** Two methods. `start(invoice) -> Charge` says where to
send the customer and what reference the settlement will carry; `verify(payload)
-> Settlement` turns a callback into a verified statement that money moved, or
raises `PaymentUnverified`. Nothing else is in the protocol.

Three implementations ship. `ManualProvider` — a staff member records a
transfer they have read on a statement, and everything downstream is the same
code path a gateway would take. `FakeProvider` — signs its callbacks with an
HMAC the way a real gateway does, so the *callback* path is exercised by the
test suite rather than by the first live payment. And nothing else: the eSewa
and Khalti adapters are deferred to §21 with the reason stated.

Two properties are enforced rather than documented. Verification happens before
anything is written, so an unverifiable payload is not a failed payment — it is
not a payment at all, and it leaves no row. And `Payment` is unique on
(provider, provider_ref), which makes a replayed callback — the normal
behaviour of every gateway — land on the row it already created.

**Why.** The interface is only worth having if a provider can be added as a
file. Two methods, a verified fake and a working manual path give that; a
speculative adapter gives the appearance of it.

**Advantages.** A real settlement path exists on day one. The dangerous half of
a gateway integration — the callback — is under test. Adding eSewa is one file
and its credentials.

**Disadvantages.** No self-serve card payment at launch. Manual settlement is
staff work that scales with customers.

**Trade-offs.** A slower path to money for a payment path that is actually
verified.

**Long-term consequences.** Every provider, including Stripe, arrives through
the same two methods and the same idempotency constraint. Refunds, when they
are needed, are a separate decision rather than a hole in this protocol.

**Reversible.** Yes — towards more methods, if a real provider needs one. The
idempotency constraint is not reversible and should not be.

**Status.** Accepted, P18.


## ADR-A56 - Erasure is gated at the AI entry point, before anything is deleted

**Problem.** §19.4 requires that "every AI and analysis task checks
`erasure_started_at` on entry and aborts". A dozen tasks can read a resume; a
check in each is eleven chances to add a thirteenth without one.

**Context.** The failure this prevents is specific and silent. An erasure walks
a dozen tables over some seconds. A task queued *before* the walk started, and
picked up by a worker halfway through it, reads the resume it was given and
writes findings, an embedding, or an `AIRun` snapshot — into tables the walk has
already passed. Nothing errors. The account finishes the erasure holding data
that was recreated after it. Nobody discovers this except the person who asked
to be erased.

There is a second, quieter version: the task holds a `CandidateProfile` instance
loaded before the walk began, so `profile.erasure_started_at` is `None` in
memory even though the row says otherwise.

**Alternatives considered.** A check in every task (rejected: eleven chances to
forget one, and no way to test that none was). A database trigger refusing
writes (rejected: it would abort transactions rather than skip work, turning an
erasure into a wave of 500s). A queue drain before erasing (rejected: unbounded
wait, and it does not help the task that is already executing). Deleting rows
last and hoping (not a mechanism).

**Decision.** The gate lives at the **one mandatory AI entry point** (ADR-A10):
`runner.run` and `runner.embed` refuse before the cache, the budget and dispatch,
recording an `AIRun` with the new terminal status `erased`. Analysis carries a
second check of its own, because its deterministic half writes findings that
quote resume lines and never touches the runner.

Two supporting decisions, both load-bearing:

- **`erasure_started_at` is set before the first row is removed**, in its own
  transaction, and mirrored onto `CandidateProfile` so a task holding a profile
  does not need a join to answer the question.
- **`is_erasing` reads the database, not the instance.** One indexed lookup per
  AI call, against a provider call that costs money and milliseconds, buys
  immunity to the stale-instance case.

`erased` is a distinct status rather than `disabled` or `refused` for the same
reason `disabled` is distinct from `provider_error`: on a dashboard they are
opposites, and a privacy request must not appear on the provider error-rate
graph. It is terminal in the strongest sense — retrying would be the exact
recreation the erasure exists to prevent.

**Consequences.** Adding an AI feature inherits the gate. A feature that
bypassed the runner would bypass the gate too, which is one more reason the
runner is mandatory. The erasure walk itself is idempotent and resumable, so a
worker killed mid-walk leaves a gated account that the next sweep finishes.

**Reversible.** Yes, and it would be a mistake.

**Status.** Accepted, P19.


## Appendix — Decision index

| ID | Title | Reversible | Owning SRS section |
|---|---|---|---|
| A1 | Membership-based authorization | No | §2 |
| A2 | PostgreSQL 16 + pgvector | No | §5 |
| A3 | No parallel JSON API | Yes | §8 |
| A4 | Permissions in Phase 1 | — | §20 |
| A5 | Django admin + three staff queues | Yes | §3.12 |
| A6 | Deterministic scoring, LLM explains (P12: CV scoring too) | Yes | §10, §5.5.2 |
| A7 | Two representations + reconciliation | Qualified | §5.5.3 |
| A8 | Immutable version + render hash | No | §5.8 |
| A9 | Content-only interview scoring | Yes (audited) | §11 |
| A10 | Task-level AI abstraction (P11: one mandatory entry point; P13: separate embedding protocol; P14: two INV-3 shapes) | Yes | §9, §5.11, §5.8 |
| A11 | AIRun for every output | Yes | §5.11 |
| A12 | Selective soft delete | Yes | §5.1 |
| A13 | Skill taxonomy as infrastructure | No | §5.3 |
| A14 | Transactional outbox | Yes | §13 |
| A15 | Nepal-first, global-ready | Yes | §1.4 |
| A16 | Video interview last | Yes | §20 |
| A17 | Webhooks now, API deferred | Yes | §8 |
| A18 | URL-scoped tenancy | No | §2.2 |
| A19 | Draft is the write target | No | §5.5 |
| A20 | Prompts in version control | Yes | §9.3 |
| A21 | No score table (P13: five-part cache key) | Yes | §10.1 |
| A22 | CandidateProfile as aggregate root | No | §5.4 |
| A23 | Stage is the only state | Yes | §5.8 |
| A24 | Selectivity-branched retrieval | Yes | §10.2 |
| A25 | Two-stage fan-out | Yes | §13.4 |
| A26 | Messaging as a domain | Yes | §12 |
| A27 | Schema and model versions | Yes | §5.16 |
| A28 | AI config in code | Yes | §5.11 |
| A29 | Signals banned | Yes | §6.5 |
| A30 | Incremental restructure | Yes | §6.3 |
| A31 | CSP with nonces; `unsafe-eval` removed in P19 | Yes | §15.4 |
| A32 | Guest uploads unprocessed | Yes | §5.5.2 |
| A33 | Unified consent | Yes | §5.2 |
| A34 | Staged contact revelation (P15: search reveals nothing) | Yes | §2.7, §10.6 |
| A35 | Redis separated by role; nothing durable in it | Yes | §17.1 |
| A36 | Hash-locked dependencies (uv) | Yes | — |
| A37 | Correlation envelope into Celery | Yes | NFR-10 |
| A38 | Redaction carve-out; token ledger in PostgreSQL | No | §5.5.1, §19.4 |
| A39 | FX provenance stored | Yes | §5.3 |
| A40 | Partition at a 20M-row threshold | Yes | §5.1 |
| A41 | Fixed-window rate limiting, per-rule failure policy | Yes | §15.4 |
| A42 | MFA via django-otp, not an in-tree TOTP | Qualified | §5.2 |
| A43 | Skill lifecycle: status + replaced_by, nothing deleted | Qualified | §5.3 |
| A44 | Importer interface, resumable two-phase runner | Yes | §5.3 |
| A45 | Field-level encryption with key rotation | Qualified | §5.4 |
| A46 | Unrecognised terms recorded in a curation queue | Yes | §5.3 |
| A47 | Provenance on every alias and relation; reversible batches | Yes | §5.3 |
| A48 | `Skill.kind`, independent of category | Yes | §5.3 |
| A49 | Extraction proposes; never writes the profile | Yes | §5.5 |
| A50 | ReportLab + python-docx; tagged PDF deferred, not claimed | Yes | §5.5.2 |
| A51 | Generated `search_vector`; no `JobRevision` table | Yes | §5.7 |
| A52 | Extra file readers are grant rows, not policy branches | Yes | §5.9, §14 |
| A53 | Webhook secrets are encrypted, not hashed | Yes | §5.12 |
| A54 | Plan limits are a versioned document, enforced in one service | Yes | §5.14 |
| A55 | Two-method payment provider protocol; manual settlement is a provider | Qualified | §5.14, §1.4 |
| A56 | Erasure gated at the AI entry point, before the first delete | No | §19.4 |
| A57 | Video interview practice ships in three stages; video never leaves the device | Qualified | §11.2, §21 |

**Ten decisions are effectively irreversible** (A1, A2, A8, A13, A18, A19, A22, A38, plus A4 and A7 in practice). They concern authorization, the database engine, evidentiary integrity, the skill substrate, tenancy, resume history and candidate identity. Everything else can be revisited with a migration plan. Contradicting one of the nine is an architecture change under SRS §0.2 — stop and escalate.

---

## ADR-A57 — AI video interview practice is delivered in three stages, and stage one is not video

**Status.** Accepted. Stage one is specified here and **not yet built**; nothing in the product claims video practice today.

**Problem.** The product should let a candidate rehearse an interview the way they will actually sit it — speaking, under time pressure — and get told how they came across: pace, filler words, structure, confidence, eye contact. The text interview (FR-10.1–10.4) rehearses the *content* of an answer and nothing about the delivery, which is most of what a real interview measures.

**Context.** What exists today: a turn-based text engine, an evaluator that scores a transcript against a rubric, a `VideoRoomProvider` seam used for scheduled live interviews with a human, and an `ai` Celery queue. What does not exist: any media capture, any storage of a candidate's recorded voice or face, any speech-to-text, and any per-frame analysis.

The distance between those two lists is the whole of this decision. Each of the things a candidate wants told to them has a very different cost and a very different reliability:

| Signal | How it is actually measured | Honest reliability |
|---|---|---|
| Words said, structure of the answer | Speech-to-text, then the existing rubric | High — this is the text engine with a different input |
| Filler words, speaking rate, long pauses | Counted from a timed transcript | High, and fully deterministic |
| Answer length against the time box | Wall clock | Exact |
| Confidence, warmth, "professionalism" | Inferred from prosody or from wording | **Low.** Correlates with accent, first language, neurotype and audio quality |
| Eye contact | Gaze estimation from video frames | **Low, and discriminatory in practice.** Depends on camera placement, screen size, corrective lenses, and disability |

**Alternatives considered.**

| Option | Rejected because |
|---|---|
| Ship "AI video interview" now, scoring everything in the table above | It would be a product that tells a candidate their eye contact is poor on evidence that is mostly camera placement. The output would be worthless, and confidently so |
| Record video, store it, evaluate later with a vision model | A candidate's face is the most sensitive thing this platform could hold. It needs a retention policy, an erasure path, a lawful basis and a processor agreement before a single frame is captured — none of which exist |
| Do nothing until full video is feasible | Most of the value — speaking out loud, hearing the answer back, being told about filler words and pace — needs no video at all |
| Three stages, most-honest-signal first | **Selected** |

**Selected solution.** Three stages, each shippable and useful on its own.

**Stage 1 — Spoken practice, no recording kept.** A new `InterviewKind.AI_SPOKEN` reusing the existing turn loop. The browser captures audio and transcribes it locally (Web Speech API where available, with the existing text box as the fallback and never as a second-class one). Only the transcript and the timings leave the device; **no audio is uploaded or stored**. The report adds a *delivery* panel beside the existing rubric scores: words per minute, filler-word count with the words quoted, longest pause, and answer length against the time box. Every one of those is counted, not judged, so every one can be shown with its working.

**Stage 2 — Server-side transcription.** A `TranscriptionProvider` seam alongside `VideoRoomProvider`. Audio is uploaded, transcribed, and **deleted on transcription** — the retention unit is the transcript, not the recording. This buys accuracy, browsers without local recognition, and languages the browser does not cover. It requires: a processor agreement, `retention.AUDIO_MINUTES`, an erasure path through `apps/candidates/services/erasure.py`, and an explicit consent recorded like every other consent (§15.5).

**Stage 3 — Video, for the candidate only.** Camera on, recorded locally, played back to the candidate. Framing and gaze hints computed in the browser and never sent anywhere. **No video is transmitted, stored, or scored, and no video-derived signal is ever shown to an employer.** A candidate watching themselves is the intervention that actually improves an interview; a model telling them a number about their face is not.

**Why.** It orders the work by how much a signal can be trusted, not by how impressive it sounds. Stage 1 delivers the majority of the value with no new data class, no new provider and no new legal surface — the platform never holds a recording of anybody.

**Advantages.** Each stage ships alone. Stage 1 adds no personal data of a new kind. Delivery feedback is deterministic and defensible line by line. The seam for stage 2 matches the one already proven for video rooms.

**Disadvantages.** Stage 1 depends on browser speech recognition, whose quality varies by browser and accent — which is why the typed answer stays a first-class path rather than a fallback for the unlucky. No employer-facing spoken assessment at any stage, which forecloses a feature a competitor will ship.

**Trade-offs.** The product will not have "AI video interviewing" on its front page for some time. In exchange it will not have to defend a score derived from a candidate's face.

**Long-term consequences.** Delivery metrics are candidate-facing, always. If an employer-visible spoken assessment is ever wanted, it is a new decision with its own ADR and its own bias evaluation — not an extension of this one.

**Reversible.** Yes for stages 1 and 2. **No** for the rule that video never leaves the device and never reaches an employer: reversing it changes what class of data the platform holds.

**Migration if it ever changes.** Stage 2 arrives as a provider implementation plus a retention rule; stage 1's transcripts are already in the shape it produces.

---

## ADR-A58 — Hosted sandbox gateways: eSewa and Khalti behind one checkout service

**Problem.** ADR-A55 deferred the eSewa and Khalti adapters because no merchant
account existed to verify them against. Both providers now publish sandbox
environments, and the product needs a self-serve way to pay an open invoice.

**Discovery.** SRS §21 and ADR-A15 name eSewa, Khalti and Fonepay. Checked
against official documentation (2026-10-01):

| Provider | Sandbox | Usable here |
|---|---|---|
| eSewa ePay v2 | `rc-epay.esewa.com.np` form, `rc.esewa.com.np` status API; UAT product code and secret key published | Yes: integrated, and its signing, form hand-off, cancel path and status API verified against the live sandbox |
| Khalti KPG-2 | `dev.khalti.com/api/v2/`; test wallets published | Code complete and tested against the documented API shapes; the sandbox key is issued per merchant at test-admin.khalti.com, so a real sandbox run needs that key |
| Fonepay | No public sandbox or merchant API documentation | Not integrated; needs a Fonepay merchant agreement |

**Selected solution.** A second, separate interface for hosted gateways
(`apps/billing/gateways/`): `begin` (form or redirect), `parse_return` (check
what the browser carries; eSewa signs it, Khalti does not), `lookup` (ask the
gateway, server to server). A `PaymentAttempt` row is written before the
customer leaves; every return is matched to it by a random reference, amount
and gateway token. **Only `lookup` decides an outcome.** A success is recorded
through `payments._record`, the same path a bank transfer takes, so the
invoice, the audit trail and past-due reactivation behave identically.

Properties enforced by tests (`apps/billing/tests/test_gateways.py`): invalid
signatures, wrong amounts, unknown or re-pointed references, and another
merchant's responses write nothing; duplicate and concurrent returns produce one
`Payment`; an unreachable gateway leaves the attempt open and a five-minute
beat task (`reconcile_payments`) asks again; a refund at the gateway marks the
payment refunded; a second payment of a paid invoice is recorded and flagged as
owing a refund; only the invoice's payer (candidate, or a company member with
`billing.manage`) can start a payment or see its result.

**Test mode only.** `PAYMENTS_ENVIRONMENT` must be `test` (`billing.E001`); an
enabled gateway without its credentials (`billing.E002`) or pointing at a live
host (`billing.E003`) fails `manage.py check`. Credentials come only from the
environment. eSewa and Khalti settle in NPR only, so a USD invoice (the
current Pro catalogue price) is offered bank transfer; an NPR price for Pro is
a business decision this ADR does not make.

**Consequences.** Adding a gateway is a module, a settings block and its tests.
Going live needs merchant agreements, live keys and removing the E001 guard
deliberately.

**Status.** Accepted, 2026-10-01. Supersedes the "deferred" part of ADR-A55.

**Note (2026-10-02).** The sentence about USD is out of date: ADR-A59 made
the NPR price that business decision (Pro NPR 1,700), so Pro invoices are now
payable through eSewa and Khalti. ADR-A59 also moves the start of a paid plan
to `payments._record`, after this ADR's server-side verification.

---

## ADR-A59: Paid access starts when payment is verified; plan allowances per AI feature

**Problem.** Two things in the product contradicted what a customer expects.
- **Access before payment.** Decision D-08 switched Pro on the moment it was
  chosen and billed afterwards. That access was not paid for and not
  verifiable, and in testing a sandbox invoice looked like a paid plan.
- **Few metered limits.** The free plan metered only CV analysis. Every other
  AI feature could be used without limit on a local model that takes about a
  minute per call.

**Decision 1: checkout before access.** The product owner asked for this rule
in the 2026-10-01 brief.
- Choosing a paid plan creates a subscription in the new state `incomplete`
  ("Awaiting payment") plus its invoice. `incomplete` is not a live state and
  confers nothing.
- The plan starts only when the invoice is settled through
  `payments._record`. That happens on a gateway's server-side verification
  (ADR-A58) or on a staff-recorded bank transfer.
- On settlement, `subscriptions.activate_paid` makes the subscription active.
  The period starts on the payment day and the invoice's period moves with it.
  The owner's previous live plan ends in the same transaction.
- One checkout per owner, enforced by partial unique constraints. Choosing the
  same plan again returns the same checkout.
- Abandoning a checkout voids its invoice. Unpaid checkouts expire after
  `CHECKOUT_EXPIRES_DAYS` (7) through the nightly `expire_checkouts` task.
- A payment that arrives after expiry is recorded, flagged as owing a refund,
  and grants nothing.
- Unchanged from D-08:
  - downgrades wait for the period end;
  - renewal invoices keep a plan running during the grace period;
  - overdue invoices block upgrades;
  - no proration.

**Decision 2: prices are in NPR.** The wallets settle only in NPR.
- Pro is NPR 1,700 a month: USD 12 at the stored rate of 141.5, rounded to the
  nearest 50.
- Prices live in `billing/seeds.py` (`PRO_PRICE_NPR`), and `seed_plans` keeps
  plans in step.
- Every price is written by `billing.money.format_money` (the `money`
  template filter).
- Historical USD invoices keep their currency.

**Decision 3: an allowance per AI feature.** Each feature has its own metered
limit, set in the plan and enforced by the existing atomic `claim`/`release`
(no second quota system).

| Allowance | Period | Free | Pro |
|---|---|---|---|
| Assisted CV replies | day | 20 | 300 |
| CV improvement drafts | day | 5 | 100 |
| Practice interviews | month | 2 | 40 |
| Cover letter drafts | month | 2 | 50 |
| CV analyses | month | 1 | unlimited |

- Units are claimed before the work and refunded when the platform fails:
  - a provider error;
  - a draft that never arrives;
  - a stalled job;
  - an interview that cannot start.
- Repeated forms, second tabs and running jobs do not claim again.
- The refusal states the use, the limit, and the reset date that enforcement
  uses (`entitlements.resets_on`).
- Interviews an employer invites a candidate to are never metered.

**Consequences.**
- Nobody holds a paid plan they have not paid for.
- A customer who chooses Pro and pays by bank transfer waits for staff to
  record it. That is the cost of the rule.
- The tests that encoded D-08's "at once" now encode this ADR
  (`test_billing_decisions.py`, `test_checkout_and_quotas.py`).

**Status.** Accepted, 2026-10-01. Amends D-08.
