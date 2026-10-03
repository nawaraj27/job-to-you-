# CareerAI — Developer Handbook

**Binding on all code in this repository.** The SRS says what to build; the ADRs say why; this document says how to write it.

**Precedence.** SRS v1.22 → ADRs → this handbook → personal preference. A rule here that contradicts the SRS is a bug in this document; report it rather than following it.

**When a rule blocks you.** Rules exist because of a failure they prevent. If one blocks legitimate work, say so and propose an amendment. Do not route around it silently — a silent exception becomes the new convention within a month.

---

## 1. General engineering rules

### 1.1 The layer contract

| Layer | Does | Never does |
|---|---|---|
| **Model** | Fields, constraints, `__str__`, trivial derived properties, custom querysets | Calls services, sends mail, hits the network, orchestrates |
| **Selector** | Scoped reads, `select_related` / `prefetch_related` | Writes |
| **Service** | Validates, mutates, transacts, emits events, enqueues tasks | Touches `request`, renders, knows about HTTP |
| **Policy** | Answers `can_x(actor, obj)` | Mutates |
| **Task** | Loads arguments, calls one service, handles retries | Holds business logic |
| **View** | Parses input, calls a policy plus a service or selector, picks a template | Queries the ORM, holds business rules, orchestrates |
| **Template** | Renders what it was given | `Model.objects`, `.filter()`, computation |

**Views orchestrate. Services execute. Selectors read. Templates render.**

### 1.2 On repositories

This project has **no repository layer**, by decision (SRS §6.5, ADR-A29's sibling rule). `selectors.py` *is* our repository pattern: one module per domain app, holding every read query, all of them scoped.

The reasoning, so it is not relitigated: a repository over the Django ORM buys database portability we have explicitly rejected (ADR-A2 commits to Postgres-specific features), at the cost of an indirection on every query and the loss of `select_related` / `prefetch_related` ergonomics. Selectors give the same benefit that matters — data access confined to one reviewable place — without the cost.

So when a rule elsewhere says "repositories access data", read it as: **selectors access data, and nothing else does.**

### 1.3 Absolute rules

1. **No business logic in views.** A view parses input, calls a policy, calls one service or selector, and picks a template. If a view has an `if` about domain state, it belongs in a service or a policy.
2. **No business logic in models.** Models hold data and invariants. A property that formats a date is fine; a method that changes state, sends mail or decides eligibility is not.
3. **No business logic in templates.** No `{% if application.stage.key == 'shortlisted' and user.membership.role == 'recruiter' %}`. Compute it in the view or selector and pass a boolean with a name.
4. **No ORM in views or templates.** Portals may not query. CI enforces this.
5. **Never duplicate logic.** The second copy is where the bug lives. If two services need the same rule, it belongs in one of them or in the domain's `rules.py`.
6. **Never hardcode IDs.** No `Skill.objects.get(id=42)`, no `stage_id=3`. Look up by slug or key, or take it as an argument. Fixtures and seeds use slugs.
7. **Never hardcode business rules in more than one place.** Score band thresholds, cooldown windows, quota limits, retention durations: each lives in exactly one constant, referenced everywhere.
8. **Never bypass permissions.** Every tenant-scoped read goes through a scoped queryset; every mutation goes through a policy. There is no "internal" path that skips them — internal callers have an actor too.
9. **Never bypass services.** No `Model.objects.create()` for a domain object outside its service, and no `.save()` on a state field outside its state machine. Tests use factories; factories are not a bypass because they do not pretend to be domain operations.
10. **Never call an external API directly.** Providers sit behind adapters: `ai.run()`, `EmailProvider`, `PaymentProvider`, `VideoRoomProvider`, storage. A bare `requests.post()` to a third party fails review.
11. **Maintainable over clever.** If a reviewer needs an explanation, write the boring version. The cleverness budget for this codebase is spent on the matcher and the reconciliation protocol; everywhere else, be plain.
12. **Never implement fake functionality.** No placeholder returns, no stubbed logic pretending to work, no hardcoded sample data in a code path a user can reach. If it is not built, it is behind a feature flag and not advertised (SRS INV-6).

### 1.4 Naming

- Services are verbs: `submit_application`, `publish_job`, `finalize_version`.
- Selectors are nouns or questions: `applications_for_actor`, `current_document`, `unread_count`.
- Policies read as sentences: `can_reject`, `require_can_apply`.
- Booleans read as assertions: `is_published`, `has_offer`, `reveals_contact`.
- No abbreviations in domain names. `application`, not `app` — `app` already means something in Django.

---

## 2. Database standards

### 2.1 Naming

| Thing | Convention | Example |
|---|---|---|
| Model | Singular PascalCase | `ResumeVersion` |
| Table | Django default (`app_model`) | `resumes_resumeversion` |
| Field | `snake_case` | `finalized_at` |
| Foreign key | Singular noun, no `_id` in the model | `candidate`, `job` |
| Boolean | `is_` / `has_` / `can_` / verb | `is_primary`, `reveals_contact` |
| Timestamp | `<past_participle>_at` | `published_at`, `revoked_at` |
| Enum choices | `TextChoices`, lowercase values | `Status.PUBLISHED = "published"` |
| Index | Django default names | — |
| Constraint | `<model>_<rule>` | `application_one_live_per_job` |

### 2.2 Keys

- Primary keys are `BigAutoField`. Never expose them externally.
- Anything appearing in a URL, an email, an API payload or a file path uses `public_id` (UUIDv7). Sequential IDs leak volume.
- Foreign keys declare `on_delete` explicitly and deliberately. `CASCADE` only where the child is meaningless without the parent. `PROTECT` for anything referenced by a record that must survive (a `ResumeVersion` referenced by an `Application` is `PROTECT`). `SET_NULL` for optional attribution.
- Every foreign key used in a filter has an index. Django creates one by default; composite filters need explicit composite indexes.

### 2.3 Indexes

Add an index when a query filters, sorts or joins on a column in a code path a user waits for. Do not add indexes speculatively — each one costs write throughput.

- Composite index column order follows selectivity and the query's `WHERE` then `ORDER BY` shape: `IX(job, stage, -applied_at)`.
- Partial indexes for partial queries: `IX(recipient, -created_at) WHERE read_at IS NULL`.
- `GIN` for `tsvector` and array containment; `HNSW` for vectors, partial on the status that every vector query filters by.
- Every new index arrives with the query it serves, named in the migration's docstring.

### 2.4 Unique constraints

- Prefer database constraints over service checks. A service check is a race; a constraint is a guarantee.
- Soft-deleted models use **partial** unique indexes excluding deleted rows, or deletion breaks re-creation.
- Business uniqueness lives in the database: one live application per job, one primary resume per candidate, one live offer per application, one active membership per (company, user).

### 2.5 Constraints and validation

- `CHECK` constraints for invariants that must never be violated by any code path: `salary_min <= salary_max`, `status <> 'published' OR published_at IS NOT NULL`, `num_nonnulls(company, candidate) = 1`.
- Enum values are validated by both `TextChoices` and a `CHECK`. The `CHECK` catches shell and data-fix writes.
- Application-level validation is for user-facing messages; database constraints are for truth. Write both.

### 2.6 Transactions

- `@transaction.atomic` on services, never on views. A view wrapping a transaction hides the boundary from the person reading the service.
- Keep transactions short. Never call a provider, render a PDF or make an HTTP request inside one — that is what the outbox is for.
- `transaction.on_commit()` for anything that must run after commit and is not an outbox event (cache warming, log emission).
- Nested `atomic` blocks are savepoints; use them only where partial failure is genuinely acceptable.

### 2.7 Locking and concurrency

Two mechanisms, chosen by contention shape:

- **Pessimistic** — `select_for_update()` on the aggregate root where two requests genuinely race for the same row: stage moves, offer decisions, slot booking, quota consumption. Lock the root, not the children.
- **Optimistic** — `row_version` on aggregates a human edits over minutes: `Job`, `Company`, `CandidateProfile`, `ResumeDraft`. The form submits the version it loaded; a mismatch raises `StaleWrite` and the UI shows what changed.

`IntegrityError` reaching a template is a bug. Catch the constraint violation you expect and raise the corresponding `DomainError`.

### 2.8 Normalisation, and when to break it

Normalise by default. Denormalise only for a measured read problem, and only in these approved forms:

| Denormalisation | Kept correct by |
|---|---|
| `Job.views_count`, `applications_count` | Async rollup from the event stream, never `F()` on the request path |
| `Application.match_snapshot` | Written once at submit; never updated (it is history, not a cache) |
| `Application.job_snapshot` | Written once at submit |
| `AuditLog.target_repr` | Written once; the target may later vanish |
| Daily stat rollups | Idempotent nightly jobs, re-runnable without double counting |

Anything else that duplicates a value needs an ADR.

### 2.9 JSONB

**JSONB holds documents. Columns hold relations.** The test: if you would ever filter, join, aggregate or index on it, it is a column.

- Permitted: `ResumeVersion.content`, `ResumeParse.structured`, `Application.answers`, event payloads, `Plan.limits`, `AIRun.output`.
- Not permitted: anything the matcher reads, anything a filter touches, anything with a foreign key relationship.
- **Every JSONB document column has a sibling `*_schema_version`** (ADR-A27) and a reader that dispatches on it.
- Validate JSONB on write against a schema in code. An unvalidated JSONB column becomes an unqueryable landfill.

### 2.10 Migrations

- One logical change per migration. Name them descriptively: `0007_add_contact_revealed_at.py`.
- **Expand → backfill → contract, across three deploys.** Never add a `NOT NULL` column and backfill it in one migration.
  1. Add the column nullable, deploy, write to it.
  2. Backfill in a batched data migration or management command.
  3. Add `NOT NULL` and the constraint, deploy.
- Data migrations are separate from schema migrations and are idempotent and batched (never `.update()` across a whole large table in one statement).
- `makemigrations --check --dry-run` runs in CI; model drift fails the build.
- Never edit a migration that has run anywhere but your machine.
- Every migration is tested by running it forwards on a copy of realistic data. Reverse operations are provided where possible; where not, say so in the migration docstring.

### 2.11 Versioning

- **Documents** version through immutable rows (`ResumeVersion`), never in place.
- **Payloads** version through `schema_version` on events and JSONB.
- **Vectors** version through `embedding_model`; a model change is add-column, dual-write, backfill, verify, swap, drop.
- **Algorithms** version through `algorithm_version` on stored results, so old outputs stay explainable.

### 2.12 Soft delete

Permitted only on: `ResumeDocument`, `Job`, `Company`, `CompanyMembership`, `ApplicationNote`, `Message`, `TalentPoolMember`, `Tag`.

Adding it to a model requires, in one release: the `deleted_at` column, a partial unique index excluding deleted rows, a default manager that excludes them, an `all_objects` manager for staff and purge paths, and **a purge job with a stated retention**. A soft-deleted model without a purge job is a defect.

Everything else hard-deletes. Erasure anonymises where a record must legally survive (SRS §19.4).

### 2.13 Immutability and redaction

Append-only models reject update and delete (`AppendOnlyModel`). Where a model
is append-only **and** holds personal data, it must also define a redaction
path, or privacy erasure becomes impossible (ADR-A38).

The pattern: one permitted transition, guarded by the same trigger that rejects
everything else, changing only the columns that carry personal data and leaving
every evidentiary column byte-identical. Redaction is itself single-use.

Before making a model append-only, ask the question that caught this the first
time: **if a user exercises their right to erasure, what happens to this row?**
If the answer is "nothing can", the design is not finished.

### 2.13a Reference data: retirement, derivation and imports

Reference rows that other tables point at — `Skill` above all — are retired by
status, never deleted (ADR-A43). Three rules follow, and all three have already
been broken once by an implementation that looked correct:

- **Merging and deprecating are different operations.** Merge asserts the two
  rows always meant the same thing; deprecate asserts one is obsolete. Do not
  collapse them into a flag. AngularJS was never Angular.
- **Derived match columns are derived in `save()`, not by callers.**
  `SkillAlias.normalised` is computed by one function that resolution also
  uses. An alias normalised differently from the input it must match resolves
  to nothing, and nothing anywhere reports it. Unicode-aware by construction:
  an ASCII whitelist silently deletes every non-Latin alias.
- **Imports fill blanks; they never overwrite curation** (ADR-A44). No renames,
  no replacing a description or category someone set, no repointing an alias
  another skill already claims. Import writes are idempotent and checkpointed,
  so re-running is always the safe response to a failure.

Offer surfaces read `selectors.active_skills()`. Reads of existing records must
**not** filter by status, or historical data disappears from view.

### 2.14 Audit logging

Write an `AuditLog` row for: any staff action, any permission or membership change, any state transition on an application, job, company, offer or subscription, any contact reveal, any bulk export, and any impersonation.

- Append-only: the application database role holds `INSERT` and `SELECT` on it and nothing else.
- Never log secrets, passwords, tokens or full resume text. Log identifiers and the changed fields.
- `target_repr` is written at log time because the target may later be deleted.
- Audit is not analytics and not the outbox. See SRS §5.15 before choosing where a write goes.

---

### 2.15 Trigger-enforced immutability

A model guard binds code that goes through the ORM. `ResumeVersion` also carries
a Postgres `BEFORE UPDATE` trigger, because the writes that matter here are the
ones that do not: a data-fix script, a shell session, a bulk `.update()`, a
migration written by somebody who has not read ADR-A8.

When a table's immutability is a promise made to a third party — an employer's
copy of a CV, an audit row, a submitted document — enforce it in the database
and treat the model guard as the readable error message on top of it.

Two rules for writing one:

- **State the permitted transition explicitly, and reject everything else.**
  `ResumeVersion`'s trigger allows `redacted_at IS NULL -> NOT NULL` and
  compares every evidentiary column for equality (ADR-A38). A trigger that
  merely blocks "some" updates is a trigger somebody will route around.
- **Test it with raw SQL.** A test that only exercises `Model.save()` proves
  the guard, not the trigger, and the guard is the half that does not bind a
  data fix.

---

### 2.16 Rendering and generated files

Rendering is a pure function of (version, template, format), and the unique
constraint on `ResumeRender` is the cache. Three rules keep that true:

- **Render an immutable row, never a mutable one.** A file produced from a
  draft cannot be reproduced, so it cannot be proven. Every renderer takes
  content plus a template *snapshot*; nothing in the package can load a model.
- **Determinism is configured explicitly.** PDF creation dates, document ids
  and zip timestamps all default to "now" and must be pinned. Any new output
  format must be byte-identical across two runs before it ships, with a test
  asserting it.
- **Bump the engine version when output changes.** A stored render made by an
  older engine is stale and gets replaced rather than served; that check is the
  only thing standing between a layout fix and files nobody can explain.

Files the platform generates for itself are stored through `apps.files` like
any other, then marked clean with `services.mark_clean(reason=...)` - the input
was already scanned on the way in, and the reason is audited. Never call it for
bytes a user supplied.

---

## 3. Service layer standards

### 3.1 Responsibilities

A service performs **one domain operation** end to end: authorise, validate, mutate, record, emit. If it does two, it is two services, and a third that calls both.

### 3.2 Signature

```python
@transaction.atomic
def publish_job(*, actor: User, job: Job, publish_at: datetime | None = None) -> Job:
    """Publish a draft job. Idempotent for an already-published job."""
```

- **Keyword-only.** A positional swap between two IDs is silent corruption.
- **Actor first**, always, even when unused — it documents that authorisation was considered.
- **Returns the domain object.** Not a dict, not an `HttpResponse`, not a tuple of flags.
- **Docstring states the invariant**: idempotency, preconditions, what it emits.

### 3.3 Dependency direction

```
portals ──▶ services ──▶ models
   │            │
   └──▶ selectors ──▶ models
                │
        services ──▶ adapters (ai, email, storage, payment, video)
```

- Domain apps may import `common`, `taxonomy`, `files`, `audit` and their own models.
- **A domain app may not import a portal.** Ever.
- Cross-domain imports go **service to service**, never model to model across domains for writes. `applications` may call `matching.score()`; it may not reach into `matching`'s tables.
- Circular imports are a design smell: the shared thing belongs in `common`, or one direction should be an event.

### 3.4 Service boundaries

An operation belongs to the domain that owns the data it changes. `submit_application` lives in `applications` even though it touches resumes and matching, because it creates an `Application`. It *calls* `resumes.ensure_render()` and `matching.score()` rather than writing their tables.

### 3.5 DTOs

Used sparingly and only where a value object genuinely is not a model:

- `MatchResult` — the computed score and breakdown (ADR-A21: no table).
- `Completion` — the normalised AI provider response.
- `ReconcileDiff` — the profile↔document drift set.

They are frozen dataclasses in the domain's `types.py`, with an `as_snapshot()` where they are persisted into JSONB. Do not introduce a DTO that merely mirrors a model.

### 3.6 Validation

Three layers, all of them real:

1. **Form / input validation** — types, required fields, formats. Produces user-facing messages.
2. **Service validation** — domain rules: is this job open, does this CV belong to this candidate, is the quota exhausted. Raises `DomainError`.
3. **Database constraints** — invariants that must hold regardless of the path.

Never rely on only one. A form check is bypassed by another caller; a service check races; a constraint has no message.

### 3.7 Error handling

```python
DomainError
├── NotAllowed       # authorisation failed            → 403
├── NotFound         # scoped lookup found nothing     → 404
├── InvalidState     # legal object, illegal action    → 409
├── Conflict         # concurrent or duplicate         → 409
├── StaleWrite       # optimistic lock mismatch        → 409
├── QuotaExceeded    # limit reached                   → 429
└── ProviderError    # external dependency failed      → 502 / queued retry
```

**Consume the limit after everything that can refuse, and outside the
transaction.** A limiter is a counter in Redis, and rolling a database
transaction back does not give the count back — so a request rejected for a
missing field would still cost the person a slot out of a budget they never
spent. Validate, then consume, then write. `applications.services.apply` and
`messaging.services.conversations.send` are both shaped this way.

Rules:
- Services raise; views map. The mapping lives in one place (`portals/common/errors.py`), not in each view.
- Every `DomainError` carries a message written for the person who will read it. `"This role is no longer accepting applications."` — not `"invalid state transition"`.
- Never catch a bare `Exception` outside a task boundary.
- Never swallow an error to keep a page rendering. A silent failure is worse than a visible one.
- A missing tenant-scoped object raises `NotFound` (404), never `NotAllowed` (403) — a 403 confirms the object exists.

### 3.8 Retry policy

| Failure | Retry? | How |
|---|---|---|
| Provider timeout, 5xx, rate limit | Yes | Exponential backoff, jitter, capped attempts |
| Deadlock, serialisation failure | Yes | Immediate, small fixed attempts |
| Schema validation of AI output | Once | One repair attempt, then terminal |
| Context length exceeded, provider refusal | No | Terminal; record and surface |
| `DomainError` | No | It is a decision, not a fault |
| Constraint violation | No | It is a bug or a race the caller must handle |

Every retryable operation must be **idempotent**, or it must be guarded by an idempotency key.

### 3.9 Background jobs

- Tasks take primitive arguments — IDs and strings, never model instances (a pickled model is a stale model).
- Tasks are idempotent: safe to run twice with the same arguments.
- Tasks declare their queue explicitly: `default`, `ai`, `render`, `files`, `email`, `match`.
- Tasks contain no business logic. Load, call one service, handle retries.
- Every task sets `max_retries` and has a dead-letter destination. A task that vanishes on failure is invisible data loss.
- Anything that can exceed one second is a task (SRS NFR-3). No request waits on an AI provider or a PDF render.

---

## 4. Event standards

### 4.1 Emitting

```python
events.emit("application.submitted", application, payload={...})
```

Inside the same `transaction.atomic` as the state change. Never after commit; never in a signal; never from a view.

### 4.2 Naming

`noun.verb_past`, lowercase, dotted, from the catalogue in SRS §13.2. Events are **facts that already happened**, never commands. `application.submitted`, not `application.submit` and not `send_application_email`.

### 4.3 Ownership

The domain that owns the aggregate owns the event: only `applications` emits `application.*`. Its payload schema lives in that app's `events.py` with a `schema_version`. Changing a payload is a contract change — add fields freely, remove or repurpose them only with a version bump and a consumer audit.

### 4.3.1 Preference group

An event that ends up in front of a person declares which notification
preference governs it:

```python
register(
    "message.sent",
    schema_version=1,
    required={"conversation_public_id", "company_id", "sender_kind"},
    preference_group="employer_messages",
)
```

`register()` refuses a group that is not a real `NotificationPreference.Event`
member, because an event mapped to a preference nobody can toggle is a channel
nobody can mute. Events that are internal facts — something a consumer reacts
to, with no message to anybody — declare no group.

### 4.3.2 Audience and wording

An event that reaches a person also declares **who** and **what it says**:

```python
register(
    "application.submitted",
    required={"application_public_id", "job_public_id", "company_id", "job_title"},
    preference_group="application_updates",
    audience="application_company_team",
    title="New application for {job_title}",
    body="Somebody applied. Open the pipeline to review them.",
    dedupe="application.submitted:{job_public_id}",
)
```

  * `audience` is a key into `notifications.services.audiences`, resolved to a
    **query** at fan-out time and re-run per chunk. Never a list — see ADR-A25.
  * `title` / `body` are format strings over the payload. A missing key renders
    blank rather than raising: a notification with a gap is a small failure, a
    fan-out chunk that dies is a large one. Anything a template names must be
    in `required`.
  * `dedupe` collapses a burst into one row that counts, while it is unread.

`register()` refuses an audience with no title, because a notification nobody
can read is not a notification.

### 4.4 Payload contents

Identifiers **plus the minimal denormalised fields consumers need**. Not the whole row — that bloats the outbox and leaks PII into a table with a different retention. Not bare IDs either — the row may change before the consumer runs, and the consumer needs to know what was true at emit time.

### 4.5 Consumer rules

1. **Idempotent.** At-least-once delivery is the guarantee. Key on `(event_id, consumer)`.
2. **Commutative.** There is no ordering guarantee. A consumer that requires event B after event A must check state, not assume sequence.
3. **Isolated.** A failing consumer must not block others. Each subscriber is its own unit of retry.
4. **Fast.** A consumer that does slow work enqueues a task; it does not do the work inline.
5. **No cross-emission without thought.** A consumer may emit a new event, but a cycle is a production incident. `A → B → A` fails review; if two things must react to each other, one of them is wrong.

### 4.6 Effects are not events

Notification creation and email queueing are **outputs** of the notification pipeline, not domain events. Emitting them back into the bus creates loops and makes "why did this send twice" unanswerable. They are rows and metrics.

### 4.7 Registry tests

CI fails if an event is emitted with no declared schema, or with no mapping to a notification preference group. No event may exist that the settings screen cannot describe (SRS §13.5).

---

## 5. AI standards

### 5.1 The only entry point

```python
from apps.ai.services import runner

run = runner.run(task_key="resume.analysis", inputs={"cv_text": text}, user=user, company=None)
if run.status == RunStatus.OK:
    findings = run.output["findings"]
```

**This is the only way an AI call is made.** Not a convention — a rule, and the one a review should check first.

`run()` always returns an `AIRun`. It does not raise for a model failure, because a failed run is a fact worth recording and a caller made to catch four exception types will catch three. Check `status`; `RunStatus.retryable()` tells you whether offering a retry is honest.

What the runner does, and therefore what you must not do:

| Inside `runner.run` | Never at a call site |
|---|---|
| The kill switch (`ai` flag), recorded as `disabled` | Checking whether AI is on |
| Cache lookup, then quota | Deciding affordability |
| The task's provider fallback chain | Naming a provider or model |
| Retry classification | `try/except` around a provider |
| Schema validation of the output | Trusting `raw_output` |
| Writing the `AIRun` | Writing an `AIRun` |

The cache is consulted **before** the budget. A cache hit spends nothing, and refusing a free answer to an out-of-budget account would punish the one request pattern the budget wants to encourage.

A new capability is a task key in `config/ai.py`, a prompt file, and a schema. It is never a new integration. Off the request path, use `apps.ai.tasks.run_task` — the same runner, on the `ai` queue, taking ids rather than objects.

### 5.2 Provider abstraction

Providers implement one protocol — `complete(request, *, model, timeout) -> Result` — and raise exactly one of three exceptions: `ProviderTimeout`, `ProviderFailure` (transient; try the next model in the chain), `ProviderRefused` (the model declined; trying again produces the same answer more expensively). That three-way split is the only classification the runner needs, and it is the adapter's job to produce it: an adapter that let a bare `requests.RequestException` escape would make every outage look permanent.

Provider-specific behaviour (JSON mode, schema support, system-prompt placement) is handled inside the adapter and never leaks. Ollama has no native schema mode, so `OllamaAdapter` asks for JSON in the prompt — an honest fallback, not a pretence of parity. Adding a provider is one adapter file plus an entry in `config/ai.py` (ADR-A28), which is code, reviewed and deployed — with one deliberate exception: **the AI kill switch is a runtime feature flag**, because disabling AI during an incident must not require a deploy.

`embed` and `stream` are not in the protocol. Nothing needs them yet, and a method with no caller is a guess about the future signed in advance; embeddings arrive with P13.

### 5.3 Prompts

- Prompt bodies are files in version control, one per task, at `prompts/<task_key>.md`. JSON front matter carries `system` and `schema`; the body below it is the user prompt.
- `manage.py load_prompts` loads them and records `body_sha256` and version into `PromptTemplate`. It is idempotent: a file whose body **and output schema** are unchanged is skipped, so running it on every deploy does not manufacture versions — and a schema-only edit does make a new version.
- `manage.py load_prompts --check` lists every prompt file whose text or schema differs from the loaded version and exits non-zero. A skipped `load_prompts` does not fail anything — the previous versions keep running — so deploys run it and verify with `--check` (DEPLOYMENT.md §3).
- **Editing the file is how you make a new version.** The stored row is what must never change — `AIRun.prompt_version` points at it and has to stay meaningful forever. Editing `prompts/resume.analysis.md` and reloading creates v2 and leaves v1 exactly as it ran.
- Never build a prompt by string concatenation at the call site. Templates take named parameters via `{placeholder}`; a placeholder with no matching input renders blank rather than raising, because a missing optional field should not take the feature down.
- Every task declares a **JSON output schema** in its front matter. Free prose is a field inside the object, never the response.
- A prompt file with no matching task key in `config/ai.py` is ignored; a task key with no prompt file raises `PromptMissing` loudly rather than falling back to a default, because a silent default is a prompt nobody reviewed.

### 5.4 Untrusted input

A resume is untrusted input that a model reads and an employer trusts. Therefore:

- Candidate-supplied text is passed as **delimited data** with an explicit instruction that it contains no instructions.
- Output is schema-validated before use.
- Any candidate-supplied text appearing in employer-facing output is length-capped and escaped.
- Never interpolate user text into the instruction portion of a prompt.

### 5.5 Caching

Keyed on `(task_key, prompt_version, input_hash)`, where `input_hash` sorts keys so two identical requests written in a different order hash the same. A cache hit writes a **new** `AIRun` with `cached_from` set and `cost` zero, so attribution and audit stay complete — returning the original row would credit one person's question to another and lose the fact that it was asked twice. Never cache across prompt versions: the version is in the key, so a new prompt version cannot read the old one's answers. Never cache a failed run. `TaskSpec.cache_hours = 0` means never cache, which is right for anything whose output should differ between two identical requests. `force=True` skips the cache — for a person pressing "try again", the only legitimate reason to pay twice for one input.

### 5.6 Retries and fallback

Classified per §3.8, from the three adapter exceptions. Fallback chains are declared per task in `config/ai.py`, tried in order, and never chosen at the call site — the chain is per task rather than global because "summarise a CV" and "extract structured skills" fail differently and deserve different second choices. A provider with no key configured is **skipped rather than attempted**: trying it produces a 401 that looks like an outage and wastes a chain slot a working provider could have used. Dispatch stops on a terminal status (`RunStatus.terminal()`): a schema error is the same prompt producing the same shape and will fail identically on every model in the chain. Exhausting the chain produces `provider_error`, a queued retry, and a named failure in the UI with a retry action — never an indefinite spinner, never a 500.

### 5.7 Token accounting and cost

- Every run records `tokens_in`, `tokens_out`, `cost` and `latency_ms`.
- Quota is checked **before dispatch**, never measured after. A limit enforced once the money is gone is an audit trail wearing a limit's name. Per-owner monthly limit (`AIUsageQuota`), per-run ceiling (`TaskSpec.max_cost`), per-task model tier.
- Cost is attributed to a user and, for employer features, to a company. A run inside a company charges **both**, because both are true — the person used it and the company paid for it.
- **Spend is recorded whatever the outcome.** A timeout that burned input tokens cost money; a budget that counts only successes is not a budget.
- Spend is incremented in the database with `F()` expressions, never read-then-written: two runs finishing together must not lose one another's spend, which is exactly the race that makes a budget leak.
- A daily spend alert fires before the budget, not after.

### 5.8 Evaluation

Cases live in `prompts/evals/*.json`, beside the prompts they test. `manage.py run_evals` runs them and **exits non-zero on a failure**, which is the whole point: without the exit code, CI reports green on a prompt change that degraded quality. Versioned prompts give traceability of a regression; only the eval set detects one.

Assertions are **structural, never exact text** — required paths (`has`), minimum array lengths (`min_items`), fields on every item (`each_has`), terms that must appear (`contains`) and terms that must not (`absent`, the guard that catches a prompt starting to invent things). Asserting exact model output produces a suite that fails on every harmless rewording and is switched off within a month.

The corpus runs against `apps.ai.providers.fake.FakeAdapter` — a real adapter implementing the real protocol, so CI needs no key, no network and no budget, and a golden corpus gets a model that answers the same way twice. Run it against a real provider by hand before shipping a prompt change. Cases never read the cache: an evaluation that passes because it read an old answer has evaluated nothing.

`AIFeedback` accumulates real-world labels from day one and feeds the set.

### 5.9 Observability

Log task key, prompt version, model, status, latency and cost per run — never the full input. Dashboards: runs per task, error rate by class, p95 latency by provider, spend per day, cache hit rate. Alert on error rate, spend and queue depth.

**Error rate is by class, and the classes must stay distinct** for the alert to mean anything. `disabled` (we switched AI off) is not `provider_error` (a vendor is failing); a run refused for budget is `quota_exceeded`, not an error at all. Any new status must be a thing an operator would act on differently.

Staff inspect runs through the read-only admin: every field visible, nothing editable except `AIUsageQuota.limit_cost`. A run is a record of what happened, and a record somebody can edit is not one.

**Retention.** `input_snapshot` and `raw_output` carry personal data and inherit the retention of the record they came from; `purge_run_snapshots` clears them past the window. The `AIRun` row itself stays — cost history and the evaluation corpus outlive the text that produced them, and deleting the row would take both with it.

### 5.10 Replacing a provider

1. Write the adapter.
2. Add it to configuration with a low routing priority.
3. Run the eval set against it.
4. Shift traffic per task, watching error rate and cost.
5. If embeddings change, follow the vector migration in §2.11 — **never mix embedding models in one index** (ADR-A27).

### 5.10a The shape of an AI feature

`resumes.services.analysis` is the reference implementation, and the shape is deliberate:

```
deterministic work            always runs, produces the feature's value
        |
        v
runner.run(...)               may fail; every failure is a status, not an exception
        |
        v
one row, with both            ai_run linked whatever happened (ADR-A11)
```

**Build the deterministic half first, and make it carry the feature.** Then add the model on top for the part a measurement cannot do. Applied to analysis: the score, the ATS checks, the experience facts and the structural findings are arithmetic; the model writes the verdict and the judgement findings. With AI off the report loses its prose and keeps everything else, which is what §5.9's kill switch is for. If switching AI off would empty the screen, the split is in the wrong place.

Three rules that follow, and each one is a test in `apps/resumes/tests/test_analysis.py`:

- **A failed run is still recorded and still linked.** But the feature must not then claim to be AI-assisted: `ResumeAnalysis.has_ai` checks the run's *status*, not the foreign key. A label that is true because a provider timed out is a lie told by a foreign key.
- **Model output is untrusted input on the way back out.** Anything the model quotes from a CV is capped on the way in and escaped on render (§5.4, §6.7). A model-authored finding gets no `section_ref`: it has never seen the document's internal ids, and a write addressed at a guessed path corrupts a CV.
- **AI never writes the user's document.** It writes a `ResumeSuggestion` and a person accepts it (INV-3). This is why the accept path built in P5 did not change when a model started proposing text — by the time that happened, the only way into a draft was already a row somebody has to say yes to.

### 5.10b Embeddings

`runner.embed(text=…)` — the same pipeline as `run`, a different call. Kill switch, budget, chain, classification and `AIRun` all apply. What differs, and why:

- **No prompt, no schema, no `prompt_version`.** The text is the input, not an instruction wrapped around one.
- **No cache lookup.** The vector is stored on the row that needed it — `Job.embedding`, `CandidateProfile.embedding` — so that column *is* the cache. Keeping 768 floats on every `AIRun` would make the cheapest call in the system the largest thing in the table.
- **`EmbeddingAdapter` is a separate protocol.** Not every provider embeds, and the chain filters to those that do (ADR-A10's P13 amendment).

**Three columns, always together** (`EmbeddedModel` in `apps/common/models.py`): `embedding`, `embedding_model`, `embedded_at`. Cosine distance between two models' vectors is noise and **nothing errors** — matching just quietly returns nonsense (ADR-A27). Retrieval filters on `embedding_model`; `needs_embedding(row, model=…)` is what makes a model change a query rather than a guess about which rows were done.

`EMBEDDING_DIMENSIONS` is a constant, not a setting. A model producing a different width needs its own column and a backfill, and the runner **refuses a vector of the wrong size** rather than padding or truncating it — a truncated vector is a plausible answer to a different question.

**A null vector is an ordinary state.** Nothing raises, no screen breaks, and the feed is complete: filters and the deterministic scorer do all the work, and the vector only ever changed the order in which candidates were considered.

### 5.10c AI that writes for a person

Analysis and matching produce *judgements* a candidate reads. Cover letters and
the assistant produce *words a candidate will send under their own name*, and
that changes what the code has to guarantee.

**The rule: a model may draft, and a person must choose.** Two shapes reach it,
and which one applies depends on whether the text already exists:

| Situation | Shape | Where |
|---|---|---|
| A draft already exists and must not be overwritten | `ResumeSuggestion`; only an accept writes | resume analysis, reconcile, the assistant builder |
| The text *is* the new thing being made | The draft lands in the candidate's own library; applying is a separate act | cover letters |

Both satisfy INV-3. Neither is satisfied by a prompt that says "be careful".

Four rules that follow, each of them a test:

1. **Record what wrote it.** `CoverLetter.source` and `ResumeSuggestion.ai_run`
   exist because a candidate accepting a sentence into their CV, or sending a
   letter under their name, is entitled to know whether a person or a model
   wrote it. `ai_edited` is not a nicety — it is the common case.
2. **Never claim what the person has not said.** The prompt gets skills and
   roles as *data*, never prose it can extend, and the screen tells the
   candidate to read it. A model that invents a job is the worst failure this
   class of feature has, because the candidate is the one who gets caught
   with it.
3. **Say what it could not claim.** `cover_letter.draft` returns `gaps`, and the
   editor shows them. Knowing which requirement the letter does not address is
   more useful than a letter that addresses it by inventing something.
4. **Keep what they typed.** The assistant records the candidate's turn
   *before* calling the model, so a provider outage costs them the answer and
   not their description. Losing twenty minutes of somebody's writing to a
   timeout is not a degraded feature, it is a broken one.

### 5.11 What AI must never do

Decide. Shortlist, reject, offer and hire are human actions with a human actor in the audit log. AI produces text and scores that a human acts on. This is not a style preference; it is the product's legal posture (ADR-A9).

---

## 6. Security standards

### 6.1 Authentication

Argon2 hashing. Email verification before applying, posting, messaging or any AI processing. TOTP 2FA mandatory for company owners, company admins and platform staff, with a 14-day grace and recovery codes. Login throttled per IP and per account. Session rotated on privilege change. All tokens — reset, invite, file access, interview join — stored **hashed**, single-use, short-lived, compared in constant time.

### 6.2 Authorization

Three layers, all mandatory, in this order:

```python
# 1. route guard  — cheap rejection, UX only
@require_permission("application.decide")

# 2. queryset scoping — the actual security boundary
applications = Application.objects.for_actor(request)

# 3. object policy — decisions and mutations
policies.require_can_reject(request.user, application)
```

- **Never write `Model.objects.all()` or `.filter()` for tenant data in a portal.** CI fails the build. Managers expose `for_actor()` / `for_company()`.
- `request.company` comes from the URL and is re-validated against an active membership every request (ADR-A18). Never from a form field, a query parameter or the session.
- A scoped lookup that finds nothing raises `NotFound`, not `NotAllowed`.
- Every policy rule ships with a **passing negative test**. A permission without a failing-case test does not count as implemented.

### 6.3 RBAC and object-level permissions

Roles are presets; `CompanyMembership.permissions` holds validated grant keys from a fixed enum (SRS §2.4). Never invent a grant key at a call site. The role×capability matrix is generated from `apps/companies/permissions.py`, so documentation cannot drift from enforcement.

Object-level checks answer questions about *this* object and *this* actor: does the recruiter have this job assigned, does the interviewer's access window cover today, does a visibility grant exist for this candidate and company.

### 6.4 File uploads

Magic-byte type detection, never the extension. Size and page caps. Filename sanitised and never echoed into a path. ClamAV scan **before any processing**; a failed scan quarantines, emits `file.scan_failed`, notifies and blocks every downstream task. PDFs processed with JavaScript execution disabled. Zip-bomb and nested-archive guards.

**Private files are never served by the static server.** They go through `/files/<public_id>/`, which runs the policy check, writes `AttachmentAccess`, and issues a short-lived signed redirect. User-supplied documents are served `Content-Disposition: attachment` and never rendered inline on an application origin.

**Sharing a private file is a grant row, never a visibility change** (ADR-A52).
A private attachment is readable by its uploader; anybody else needs a row:

```python
file_service.share_with(attachment=attachment, user=recipient, reason="message")
file_service.revoke_share(attachment=attachment, user=recipient, reason="message")
```

`reason` belongs to the granting domain and the file layer never interprets it,
so `files.policies` does not grow a branch per domain. Withdraw what you
granted when the thing that granted it goes away — deleting a message revokes
its attachments. A grant never overrides the scanner or a deletion: an infected
or deleted file is readable by nobody.

**A secret you *use* is encrypted; a secret you *verify* is hashed** (ADR-A53).
A password is compared against a digest and the original is never needed again.
A signing key — a webhook secret, for instance — is fed to an HMAC on every
outbound request, so hashing it leaves nothing to sign with and the signature
becomes a constant that a receiver can verify without learning anything. Use
`EncryptedCharField` (ADR-A45) for the second kind. Deciding which kind you
have is one question: *does this server ever need the original value again?*

### 6.5 Rate limiting

Use `apps.common.ratelimit` (ADR-A41). Never invent a counter at a call site.

```python
from apps.common import ratelimit

ratelimit.enforce(
    [
        (ratelimit.LOGIN_PER_IDENTITY, email),
        (ratelimit.LOGIN_PER_IP, ratelimit.client_ip(request)),
    ]
)
```

Rules:

- **Two dimensions on anything sensitive** — identity *and* IP. Identity alone
  lets an attacker lock a victim out; IP alone is defeated by any botnet.
  Identity first, so a user behind a shared address hits their own limit.
- **Choose the failure policy deliberately.** `fail_closed=True` for anything
  auth-shaped or expensive; `False` for browse-shaped reads. A cache outage must
  not admit logins, and must not take search down.
- **Identifiers are hashed into keys** by the module. Never build a key
  containing an email address.
- Domain limits (applications per day, messages per company) are business rules
  and live in services, not here.

### 6.6 Content Security Policy

Enforced by `apps.common.csp` (ADR-A31). Rules:

- Any inline `<script>` must carry `nonce="{{ csp_nonce }}"`. There is exactly
  one today; adding a second needs a reason.
- **Never add `'unsafe-inline'` to `script-src`.** It defeats the nonce, which
  is the control that blocks injected résumé content.
- The `'unsafe-eval'` exception exists for Alpine's standard build and is
  measured, documented and scheduled for removal in P18. Do not widen it, and do
  not cite it as precedent for another exception.
- New external origins need a policy change and a review, not a wildcard.

### 6.7 Logging context

Use `apps.common.logging` (ADR-A37). Bind what you know; never log what you
should not.

```python
from apps.common import logging as log

token = log.bind(actor_id=user.pk, company_id=company.pk)
try:
    ...
finally:
    log.reset(token)
```

Always pair `bind` with `reset`, and always in a `finally`. At a task boundary
use `set_context` on entry and `clear` on exit — the clear is not optional,
because a pooled worker otherwise carries one user's identifiers into the next
user's task.

### 6.6 CSRF

Django's protection stays on everywhere. Command endpoints are POST-only and CSRF-protected. Long-lived builder pages refresh the token on a timer so autosave does not start failing silently. Never exempt a view; if you think you need to, you need an idempotency key and a signed payload instead.

### 6.7 XSS

Template auto-escaping stays on. `|safe` and `mark_safe` require a reviewer's explicit approval and a comment explaining why the content is trusted. User-supplied text is never interpolated into a `<script>` block or an HTML attribute without escaping. The CSP (ADR-A31) is defence in depth, not the primary control — and until Phase 18 it permits `unsafe-eval`, so escaping discipline carries more weight, not less.

### 6.8 SQL injection

Use the ORM. Raw SQL requires parameterisation, a comment justifying it, and review. Never format a query with an f-string. `.extra()` is banned.

### 6.9 SSRF

Every user-supplied URL the server fetches — link checking, webhook endpoints — resolves through an allow-list: no private ranges, no link-local, no redirects into them, DNS re-checked at connection time.

### 6.10 Secrets

From the environment only. Never in the repository, never in `settings.py` literals, never in a migration, never in a log line, never in an error message shown to a user. Rotation is possible without a code change. `pip-audit` and dependency scanning run in CI.

### 6.11 Encryption and PII

Field-level encryption for phone numbers and MFA secrets. Managed encryption at rest for the database. TLS on every hop.

Use `apps.common.crypto.EncryptedCharField` — never a hand-rolled cipher, never
`django.core.signing` (which is signing, not encryption). Keys come from
`FIELD_ENCRYPTION_KEYS`, newest first: the first encrypts, all decrypt, so
rotation is prepend → re-save rows → drop the old key (ADR-A45). Three rules
follow from Fernet being randomised and authenticated:

- **No lookups on an encrypted column.** No `filter(phone=...)`, no index, no
  unique constraint. If search is genuinely needed, add a separate blind index;
  do not weaken the field.
- **Empty is stored empty**, so `if not profile.phone` keeps meaning what it says.
- **A value no key can read raises.** Returning the ciphertext would render a
  base64 blob where a phone number belongs.

`manage.py check` fails without a valid key (`common.E001`/`E002`), so the
failure lands on a deploy rather than on the first person to save a number.

Data classes and retention are in SRS §15.3 and are enforced by scheduled jobs, not intention. Contact details reveal by pipeline stage (ADR-A34) — **a contract test asserts that candidate contact fields never appear in an employer payload before reveal.** Adding a field to an employer-facing serializer requires checking that test.

### 6.12 Audit

Per §2.13. Impersonation carries a reason, expires in 60 minutes, cannot target staff, and stamps `on_behalf_of` on every resulting action.

---

## 7. Frontend standards

The design system is built and is authoritative. Backend work must not degrade it.

### 7.1 Design system usage

- Every visual element comes from the component layer in `static/css/src/input.css` or a partial in `templates/components/`. If it is not in the system, add it there first and add it to `/design-system/`.
- **No inline styles.** No `style="..."` attributes. No one-off CSS in a template.
- **No class names built by string concatenation** — `bg-brand-{{ step }}` produces no CSS, because Tailwind scans source text.
- Semantic tokens only (`surface`, `content`, `hairline`, `accent`). Never a raw primitive, never a hex value in markup.
- Icons come from the `{% icon %}` registry. No inline one-off SVG.

### 7.2 Accessibility

WCAG 2.1 AA is a requirement, not an aspiration (SRS NFR-12).

- Every form field renders through `components/form_field.html` so labels, hints and errors are consistent.
- One focus style, defined once, never removed.
- `prefers-reduced-motion` respected globally.
- HTMX fragment swaps must not destroy focus. After a swap that replaces the focused element, move focus deliberately.
- Dynamic regions that update announce themselves (`aria-live`) where the change matters and would otherwise be silent.
- Exported PDFs are tagged.
- Every screen is checked at 375px and at desktop width.

### 7.3 The four states

**Every surface that can be empty, slow, wrong or successful must handle all four.** A screen missing one is incomplete, not "to be polished later".

| State | Requirement |
|---|---|
| **Loading** | Named progress where the wait exceeds a moment ("Extracting text", "Checking formatting"), not an indefinite spinner. Skeletons for list content |
| **Empty** | Never a blank panel. Say what would be here and give the one action that fills it |
| **Error** | Say what failed, in the user's terms, and what to do. Offer a retry where a retry can work. Never a stack trace, never "something went wrong" alone |
| **Success** | Visible confirmation. For autosave, the honest state — `dirty` / `saving` / `saved` — never a lie |

### 7.4 Feedback

**Every interaction provides feedback within 100ms**, even if only a pressed state or a disabled button. A command endpoint returns the smallest fragment that changed. Cross-fragment updates (unread badges, toasts) ride `HX-Trigger` rather than a client re-fetch. An expired session returns `HX-Redirect` — never a login page rendered into a `<div>`.

### 7.5 No dead UI

No control that does nothing. No link to an unbuilt page. No setting that is stored and never read. A module that is not built is not advertised — that is what `apps/core/features.py` is for (SRS INV-6).

### 7.6 Animation

Motion clarifies a change; it does not decorate. Durations from the token scale. Nothing over 300ms on an interaction path. All of it disabled under `prefers-reduced-motion`.

---

## 8. Testing standards

### 8.1 What each layer gets

| Type | Scope | Rule |
|---|---|---|
| **Unit** | One function, no database where possible | Scorers, hashing, state machines, date logic. Fast, exhaustive on edge cases |
| **Service** | One service, real database, no HTTP | The primary suite. Every service has happy path, each failure mode, and idempotency |
| **Selector** | Scoping and query shape | Assert that another tenant's data is not returned. Assert query counts on hot paths |
| **Permission** | Every policy rule | **A passing negative test per rule.** The matrix suite iterates the role×capability table and fails on an untested capability |
| **Integration** | View through service to database | One per critical user path: apply, publish, book interview, send message |
| **Event** | Payload schemas and consumer idempotency | Emitting twice produces one user-visible effect |
| **Regression** | One per fixed bug | Reproduce the bug first, then fix it. A fix without a test is not a fix |
| **Performance** | `assertNumQueries` on the ten hottest views | Fail on N+1. Budget stated in the test |
| **Accessibility** | Rendered markup | Labels present, one `h1`, focus order sane, no removed focus styles |
| **Golden file** | Scorer output, prompt output | Deterministic scoring and AI evaluation compared to committed expectations |

### 8.2 Rules

- Factories, not fixtures. One `factories.py` per app. Factories may create model rows directly; that is not a service bypass, because they do not claim to be domain operations.
- Tests name the behaviour, not the method: `test_withdrawn_application_allows_reapplying`.
- No sleeping. No network. No real provider calls — adapters are faked at the protocol boundary.
- Coverage gate: **≥80% on services and policies.** Coverage elsewhere is informational; coverage there is a floor.
- A test asserting that a permission *fails* is worth more than three asserting that it succeeds.
- Time is injected or frozen. Never assert on `now()`.

---

## 9. Git standards

### 9.1 Branches

`main` is always deployable. Work happens on `phase/<n>-<slug>` for phase work and `fix/<slug>` for defects. Branches are short-lived; a branch open longer than a week is a scope problem.

### 9.2 Commits

Conventional commits, imperative mood, scoped to the domain app:

```
feat(resumes): add draft snapshot ring for undo/redo
fix(applications): prevent duplicate live application under concurrency
refactor(matching): extract retrieval strategy selection
test(companies): add negative cases for membership revocation
chore(ci): add migration drift check
docs(adr): record A31 CSP decision
```

One logical change per commit. A commit that touches a migration touches only that migration and the model it belongs to. Never mix a refactor with a behaviour change — the review cannot separate them.

### 9.3 Dependencies

`requirements.in` and `requirements-dev.in` hold **intent** and are the only
files a human edits. The `.txt` files are **generated**: fully pinned, with
hashes, and installed with `--require-hashes` (ADR-A36).

```bash
uv pip compile requirements.in -o requirements.txt --generate-hashes
uv pip compile requirements-dev.in -o requirements-dev.txt --generate-hashes
pip install --require-hashes -r requirements-dev.txt
```

Rules:

- **Never edit a `.txt` file by hand.** CI recompiles and diffs; a hand edit
  fails the build.
- Adding a dependency is: edit the `.in`, recompile, run the suite, commit both.
  The friction is deliberate — §1.3 rule 10 still applies, and most additions
  should not survive the questions in §28 of the review checklist.
- `uv` resolves against the interpreter it discovers, which on a machine with
  several Pythons is not necessarily the one `manage.py` uses. Install with
  `pip` into the project interpreter, or point `uv` at it explicitly.
- A recompile that moves a version is a real change: read the diff and run the
  suite before committing it.

### 9.4 Migration policy in git

- A migration is never edited after it has run outside your machine.
- A branch that adds a migration rebases rather than merges, so migration order stays linear.
- Two branches adding migrations to the same app is a merge conflict to resolve deliberately, never with `--merge` renumbering after the fact.

### 9.5 Code review checklist

Reviewer confirms, explicitly:

- [ ] Layer contract respected — no logic in views, models, templates or signals
- [ ] Every tenant query is scoped; no `objects.all()` in a portal
- [ ] Every new permission rule has a **negative** test
- [ ] Services are keyword-only, actor-first, atomic, returning domain objects
- [ ] Side effects go through the outbox, not inline calls or signals
- [ ] New events have a schema, a version, an owner and a preference mapping
- [ ] Migrations follow expand → backfill → contract; no `NOT NULL` plus backfill in one step
- [ ] New indexes name the query they serve
- [ ] JSONB columns have a schema version and a validator
- [ ] Soft delete, if added, ships with a partial unique index and a purge job
- [ ] No hardcoded IDs, no duplicated business rules, no secrets
- [ ] External calls go through an adapter
- [ ] AI calls go through `ai.run` with a schema and a quota check
- [ ] All four UI states handled; no dead UI; no inline styles
- [ ] Accessibility not regressed; focus preserved across fragment swaps
- [ ] Tests: service, permission negative, regression if fixing a bug
- [ ] Nothing contradicts the SRS or an ADR — and if it does, it was escalated, not decided in the PR

### 9.6 Definition of Done

A change is done when **all** of the following are true:

1. It does what the SRS says, and nothing the SRS does not say.
2. Services, policies and events are tested, with negative cases.
3. `assertNumQueries` holds on any hot path it touches.
4. Migrations run forward on realistic data and are reversible or documented as not.
5. All four UI states exist for any surface it touches.
6. Accessibility is unchanged or better.
7. No new `TODO` in a path a user can reach.
8. No placeholder logic, no fake data, no dead controls.
9. Feature flag flipped only when the surface genuinely works end to end.
10. The mock module it replaces is **deleted**, not left beside it.
11. CI green: lint, types, tests, coverage gate, migration drift, prompt eval, dependency audit.
12. Reviewed by someone who checked the list in §9.5.

A phase is done when every change in it is done, the phase report is written (SRS §20), and the "done when" column for that phase is demonstrably true.

---

## 9.6a Cover letters and the assistant

Both live under §5.10c's rule: a model may draft, a person must choose.

**Cover letters** (`applications.services.letters`). The letter library is the
proposal surface — a draft lands there, the candidate edits it, and applying is
a separate act. Three things the service enforces rather than asking for:

- A **sent** letter cannot be edited or deleted. It is part of what an employer
  read, which is ADR-A8's reasoning applied to a letter; the answer is a
  duplicate, not a silent copy-and-edit.
- `source` moves `ai_draft → ai_edited` on the first edit, and never back.
- The library is bounded (`MAX_LETTERS`). Without it, "draft" is an accidental
  denial-of-service on the candidate's own list.

**The assistant builder** (`resumes.services.assistant`). Records the
candidate's turn *first*, then calls the model; bullets become
`ResumeSuggestion` rows aimed at the most recent experience entry. With no entry
to aim at it proposes nothing and the reply stands alone — a suggestion with
nowhere to go is a button that cannot work.

**Not streamed, and that is a presentation decision.** SRS §9.5's ASGI exception
is still open and still unused. Do not add a second serving path to make a reply
type; do notice that the screen was silent from P5 to P14 and that answering at
all was the part worth building.

---

## 9.5b Analytics standards

Four rules, and the first two are the ones that get broken:

1. **A dashboard reads a rollup.** Never `AnalyticsEvent`, never a live aggregate over applications. The day a posting goes viral is the day somebody opens the dashboard.
2. **A rollup is computed from the domain tables**, not from the event table. Events are lossy by design (§5.15); a funnel counted from them drifts from the applications, and the applications are what an employer acts on.
3. **Every rollup is idempotent per (date, dimension).** `update_or_create`, never `create`. A retry that double-counted would end the dashboard's credibility on the first incident.
4. **Capture never raises and never carries prose.** `capture.record` swallows its own failures — a counter must not break the product — and `_clean` drops anything that looks like personal data before it reaches a table with a different retention from the record it came from.

**Which of the four event tables** (§5.15): a fact worth *counting* is `AnalyticsEvent`; a change a user must see is `ApplicationEvent`; one a regulator must see is `AuditLog`; a side effect that must happen is `OutboxEvent`. Writing to several is normal. Writing to the wrong one is what the rule prevents.

**A hire is a stage key, not a stage kind.** `hired` and `declined` are both `terminal` — correct modelling, since both end the application — so any count of hires filters on `key`. P17 found this the expensive way.

---

## 9.5c Billing and entitlement standards

Five rules. The first is the phase's exit criterion and the second is the one that will get broken first.

1. **A plan limit is enforced in a service, never in a template.** `entitlements.require(owner, key)` raises; a view may call `entitlements.allows(...)` to decide whether to draw an upgrade prompt, but hiding a button is not a limit. The builder posts a template slug and the export endpoint takes a format — both are reachable without ever loading the page that was supposed to enforce anything.
2. **A plan limit is not a rate limit.** `apps/common/ratelimit.py` refuses abuse and answers 429 ("slow down"). `PlanLimitExceeded` answers 402 ("this costs money"). Only one of them is fixed by waiting, and telling a free user to wait for something that will never become free is a support ticket the code wrote itself.
3. **Every limit key is declared in `apps/billing/limits.py`**, with its kind and the sentence somebody reads when they hit it. A plan naming an undeclared key is refused when the plan is written. Never read `plan.limits[...]` directly — an unknown key silently reads as "unlimited", which is how a paid feature stays free for a year.
4. **Consume after the work succeeds, never before.** `entitlements.consume` follows the write it paid for. A candidate whose analysis died on a provider outage has not used their monthly analysis, and charging them for the platform's own failure is the sort of billing bug people leave over.
5. **Money records are read-only outside the services.** Subscriptions, invoices and payments are read-only in the admin; the operations staff need (assign a plan, record a transfer, void an invoice) are admin *actions* that call services, so they audit and emit like every other write.

**Which owner.** A subscription belongs to a `CandidateProfile` or a `Company`, never both — a database `CHECK`, plus one partial unique constraint per side so "which plan am I on" cannot have two answers. Pass either to `entitlements`; a `User` is resolved to their profile.

**An entitlement is not withdrawn as a reflex.** `past_due` still confers the plan; a cancellation sets a date and the plan runs to the end of the period somebody paid for. Withdrawal happens in `subscriptions.expire`, on a schedule, and nowhere else.

**Adding a paid feature.** Declare the limit in `limits.py`, add it to the plan documents in `seeds.py`, call `require()` in the service that does the thing, and — if it is metered — `consume()` after it succeeds. The pricing page picks it up automatically, because the page is generated from the same documents. That last part is the point: P18 found the marketing page advertising three limits nothing enforced and a template count that was twice the truth, which is INV-6 read backwards.

---

## 9.5d Retention, erasure and staff standards

Five rules. The first two are the ones that would do real harm if broken.

1. **`erasure_started_at` is set before the first row is removed, and read from the database.** The AI runner refuses every task for a subject being erased (ADR-A56); analysis carries its own check because its deterministic half writes findings that quote resume lines. Never answer "is this being erased" from an in-memory instance: a task loads its subject at the start of a job and writes at the end, and the row it holds is stale by then.
2. **A retention promise without a scheduled job is not a promise.** Every duration lives in `apps/common/retention.py`, every rule names the task that keeps it, and a test fails if one does not. Adding a retention rule means adding a task in the same change.
3. **Every scheduled task must be in `beat_schedule`, and a test asserts it.** P17's rollups and P18's billing lifecycle were both written and both left unscheduled — a dashboard reading numbers nothing computed, and a subscription lifecycle nothing advanced. Neither failed a test, because no test looked at the schedule. One does now.
4. **Erasure walks stores by hand, never by cascade.** The list in `apps/accounts/services/erasure.py` and the table in SRS §19.4 must agree; the suite asserts it. A table added next year is a decision about erasure in either direction, never an accident.
5. **Staff authority is `StaffRole` and nothing else.** Not `is_staff`, not a company role. Levels are ordered — `at_least(user, "moderator")`, never a set — and a person with no staff role gets a 404 rather than a 403, because a staff screen should not exist as far as a candidate is concerned.

**Where an erasure gate goes.** At the one mandatory entry point, not in each task. A check in twelve tasks is eleven chances to add a thirteenth without one.

---

## 9.5e Frontend standards under CSP

`script-src` is `'self'` plus a per-request nonce. No `'unsafe-inline'`, no `'unsafe-eval'` (ADR-A31, closed in P19). Three consequences for anybody writing a template:

1. **An Alpine expression must be an expression.** The `@alpinejs/csp` build parses into an AST and walks it: property access, member access, calls with arguments, ternaries, comparisons, `&&`/`||`, assignments and `++` all work. A semicolon, an `if`, an arrow function, a regular expression or a template literal throws at runtime. Put those in a component method in `static/js/app.js` — `closeEverything()` and `searchShortcut()` exist for exactly that reason.
2. **Everything the page loads comes from this origin.** No CDN for fonts, images, or scripts. This is not a preference: the policy blocks them, and the launch audit found a site that had been silently falling back to system fonts and rendering no photography because nobody had loaded a page against the real policy.
3. **Any inline `<script>` carries `{{ csp_nonce }}`.** There is exactly one, and it is the theme script that runs before first paint.

**A template comment is `{% comment %}`, not `{# … #}`, whenever it spans lines.** Django's `{# #}` is single-line only; a multi-line one renders as visible text on the page. Three had shipped.

---

## 9.6a Interview standards

Two engines, one session model, one outcome shape (§11.1). The rules, in the order they get broken:

1. **`application IS NULL` means practice, and that is the privacy mechanism.** Not a permission check — `sessions_for_company` filters `application__isnull=False`, so no employer queryset reaches a practice session through any join. Never add an employer-side query that starts from `InterviewSession` without that filter.
2. **Never move an application from an interview.** The outcome recommends; a person decides (ADR-A9). The consumer records the completion on the timeline and stops.
3. **`integrity_meta` is context, never a score input** (FR-10.4). It is not passed to the evaluator, and a test asserts the paste count is absent from what was sent. Typing behaviour is not job performance.
4. **A scorecard is invisible until `submitted_at`** (FR-10.8). Fetch through `evaluation.visible_evaluations`, never `session.evaluations.all()` on an employer screen — anchoring is prevented in the selector so a template edit cannot undo it.
5. **The candidate never waits for scoring.** Evaluation is queued on commit, per competency, after the session closes. A spinner while a model reads an answer is a closed tab.
6. **Questions are sampled, not sequenced** (FR-10.3). A fixed bank asked in a fixed order is on a forum within a month. `random`, not a seeded shuffle: reproducible sampling is a bank with extra steps.
7. **Overlap is the database's job.** Never check-then-insert a slot. Catch the `IntegrityError` and turn it into "somebody just took that time", which is what happened.

**Degradation is the same rule as everywhere.** With AI off the interview still runs from the template's own pools; only follow-ups and scoring are missing. A template with no pools and no job to plan from is refused outright rather than invented.

---

## 9.6b Talent search standards

`apps/candidates/search.py` ranks; `apps/candidates/services/talent.py` decides who is in the queryset. **The split is the control.** A search module that also enforced visibility would be a second home for the rule, and the second home is where it drifts.

**The rules, in the order they get broken:**

1. **`search()` takes `base` with no default.** There is no safe default queryset over people. A caller that forgot to scope would otherwise get the whole corpus, and the mistake would look like working code.
2. **Discoverable ≠ readable.** Appearing in results is not being read. Opening a profile creates the grant *and* writes the `ProfileView`, in one transaction — a read that was authorised but not recorded is exactly the read a candidate is entitled to know about (INV-4, FR-9.3).
3. **`NotFound`, never `NotAllowed`, for a candidate outside the queryset.** A 403 confirms a person with that id is on the platform, which is the fact the visibility setting exists to withhold.
4. **`NotAllowed` propagates to a 403.** A view that catches it and renders an inline message turns "you may not do this" into "your query was wrong", which is recoverable by editing the form. Catch `InvalidState` inline; let `NotAllowed` through.
5. **Export is a different act with a different grant**, an allow-listed field set, a cap, and a logged view per row. An export that did not appear on the candidate's screen would be a way to read a hundred profiles invisibly.
6. **Anomaly detection reports; it never blocks.** Blocking rules teach an attacker where the threshold is. Hard limits are business rules in the service.
7. **Never name the individual who looked.** The company is accountable for the read. Naming the recruiter exposes one person's browsing to another and is not what the candidate asked.

**Derived columns are set on the recompute path, never by a fixture before it.** `completeness`, `is_searchable` and `experience_months` are recomputed together; setting one before calling `recompute` has it silently overwritten, and the failure surfaces somewhere else entirely.

---

## 9.7 Matching standards

`apps/matching` is services, a scorer, an extractor and two subscribers. **It owns no tables**, and a test asserts that (ADR-A21). If you are about to add one, read A21 first: the thing it refuses is a candidate×job score table, and "just a small cache table" is how that arrives.

**The rules, in the order they get broken:**

1. **Nothing but the extractor reads a model.** `extract.py` turns a profile and a job into two dictionaries; everything downstream is arithmetic on those. This is not tidiness — it is what makes the scorer a `SimpleTestCase` and what makes the fairness allow-list enforceable at one point instead of everywhere.
2. **Every feature goes on the allow-list, with a written justification.** `apps/matching/features.py`. A feature not there raises; adding one is a diff a reviewer reads for exactly that reason. Never add a feature you would not defend to the candidate it ranks (FR-8.9, SRS §10.4).
3. **The model explains, it never scores.** An explanation is given the breakdown and never the inputs, so it cannot form a second opinion that contradicts the number beside it (ADR-A6).
4. **Bump `ALGORITHM_VERSION` for any change to a weight, a threshold or a component.** It is in every cache key and on every frozen snapshot; without the bump, two incomparable scores are compared silently.
5. **Diversity and exposure adjust *position*, never `match.score`.** A candidate is told a number and an employer sees the same number; moving a posting down a list must not change what it is worth (FR-8.8).
6. **A frozen snapshot is read, never recomputed** (FR-8.5). Six months on, the profile has changed and the scorer has been retuned. Recomputing would rewrite what the employer saw when they decided.
7. **Every ranked list ends with the same tiebreaker** — `(-score, -id)`. Without it two equal scores swap places between requests, and a candidate sees one posting twice while never seeing another (SRS §10.5).

**Neutral, not zero.** Where an input is missing — no industry history, no published pay range, no stated location, a posting that lists no skills — the component scores neutral. An unknown is not a mismatch, and scoring it as one penalises candidates for what employers left blank and postings for what candidates have not filled in.

**The cache key is the invalidation** (SRS §10.1). Never write a sweep. If a new staleness class appears, it becomes a key component; a sweep that has to guess which keys exist is the alternative, and it is worse.

---

## 9.8 CV templates and CV improvement

**A template is data plus one structure; sample content is not a CV.**

- A template is a row seeded from `apps/resumes/seeds.py` (`TEMPLATES`: slug,
  name, layout, accent, font, density, tier, suits, notes). Its `layout` names a
  structure in `apps/resumes/rendering/theme.py` (`_THEMES`), which the PDF, Word
  and browser renderers all read. **Adding a template** that uses an existing
  structure is one tuple in `TEMPLATES` and `python manage.py
  seed_resume_templates`; the gallery, detail page, editor, compare view, tier
  gate and exports pick it up with no other change. **Adding a structure** is a
  new `_THEMES` entry plus support in each renderer — a decision, not a data edit.
- The gallery, a template's page and the comparison always draw the worked
  example (`presentation.sample_content()`, a fresh deep copy of
  `examples.EXAMPLE_CONTENT` per render). Never pass a candidate's draft to a
  gallery view. A candidate's own CV appears in a template only when they ask
  (`TemplateDetailView`, `?cv=<document>`), and only their own document.
- Free/Pro is decided by `ResumeTemplate.tier` and enforced by
  `documents.require_template_allowed`; the page may only *describe* the lock
  (`_locked_templates`), never be the enforcement.
- A fragment endpoint reached by an ordinary form post must answer with a page
  (`apps/common/fragments.py`), never with its region alone.

**Improvement flows from the analysis, through suggestions, never around them.**

- `apps/resumes/improvement.py` turns the latest analysis's findings into plan
  items (what was found, why it matters, where, what to add) and classifies each
  as `reword` (drafting allowed: only existing words are rephrased),
  `needs_input` (a question; drafted only from the candidate's answer) or `edit`.
  Add guidance for a new finding code in `GUIDANCE`; an unknown code defaults to
  `needs_input`, the safe side.
- Anything that waits on a model runs as an `ImprovementJob`
  (`services/improvements.py`, task `run_improvement_job` on the `ai` queue): one
  active job per CV (database constraint), a one-time form token, staged
  progress read from the row, stall recovery after `STALL_AFTER`, retry after
  failure, and a plain failure message — never the exception.
- Nothing in either flow writes the draft. Drafts are `ResumeSuggestion` rows,
  applied only by `suggestions.accept` (INV-3).

## 9.9 CV uploads, payment gateways, branding and the admin (2026-10-01)

**CV uploads** (`apps/resumes/services/validation.py`):
- A CV is accepted or refused before anything is stored. Add a new refusal as
  a `CvRejected(reason)`; the headline sentence is added for you.
- Don't move a check after `files.services.store`: a refused file must leave
  no attachment, row or job.
- Guest uploads get `read_content=False` (ADR-A32).
- The parse reuses the validation's text through the cache
  (`upload.handoff_key`). Bumping `ENGINE_VERSION` invalidates it.

**Payment gateways** (ADR-A58, `apps/billing/gateways/`):
- Sandbox only. Configuration lives in the environment:
  - `PAYMENTS_ENVIRONMENT=test`;
  - `ESEWA_ENABLED`, `ESEWA_PRODUCT_CODE`, `ESEWA_SECRET_KEY`. The UAT values
    are published on eSewa's developer site; copy them into `.env`, never
    into code;
  - `KHALTI_ENABLED`, `KHALTI_SECRET_KEY`. This is the per-merchant sandbox
    key from test-admin.khalti.com.
- `manage.py check` refuses a live environment, an enabled gateway without
  credentials, and live hosts.
- Tests never reach a gateway: `PAYMENTS_BLOCK_NETWORK` in test settings.
  Answer as the gateway with `gateways.base.set_transport(httpx.MockTransport)`.
- An outcome comes only from `Gateway.lookup` (server to server). A browser
  return proves only which attempt it is about.
- To try eSewa locally:
  1. Enable it in `.env` and restart the server.
  2. Open an NPR invoice on the plan page and choose "Pay with eSewa".
  3. Sign in on eSewa's page **yourself** with the test wallet from eSewa's
     documentation.

**Branding:** the site name comes from `settings.SITE_NAME`. Use
`{{ site.name }}` in templates and `apps.core.branding.site_name()` in
Python, never the literal name (`docs/BRANDING.md`,
`apps/core/tests/test_branding.py`).

**Admin:** a new model must be registered or inlined
(`test_admin_inventory.py`). Records use `apps.common.admin_tools.RecordAdmin`
with `private_fields` for anything a person wrote or anything that is a
credential (`docs/ADMIN.md`).

## 9.10 Allowances, CV lifecycle, Assisted CV, job filter (2026-10-02)

**Allowances** (ADR-A59, `docs/billing/Pricing-and-Quotas.md`):
- A new metered AI feature gets a `Limit` in `apps/billing/limits.py` and a
  number in each plan in `seeds.py`, then `seed_plans`. Never a number in a
  view.
- `entitlements.claim(owner, key)` before the work; `release` on every path
  where the platform failed. Do not claim again for a repeated form or a job
  already running.
- Catch `PlanLimitExceeded` in the view and show it with
  `restrictions.flash` or the restriction fragment; it already carries the
  used/allowed numbers and the reset date.
- Prices: `billing.money.format_money` / `|money`. No currency symbols in
  templates.

**CV lifecycle** (`docs/CV-Lifecycle.md`):
- "Saved" is `ResumeDocument.saved_at`, set only by `versions.finalize`. Lists
  of CVs use `selectors.saved_documents_for` and `drafts_for`.
- Never create a `ResumeVersion` outside `finalize`.
- `documents.delete` hard-deletes a document with no version; anything that
  can point at a document must point at a version.

**Assisted CV** (`docs/Assisted-CV.md`): the start screen is `resume:ai`;
answers run as `ImprovementJob` turns on the `ai` queue. Keep the reuse box
unticked by default.

**Job search** (`docs/Job-Search.md`): a new filter is a field on
`SearchQueryInput` with `from_mapping`/`as_mapping`, so saved searches and
alerts keep it. Facet counts for a filter exclude that filter.

**Measuring the pipeline:** stage timestamps are on the rows
(`ResumeUpload.created_at`, `Attachment.scanned_at`, `ResumeParse`,
`ResumeAnalysis`, `AIRun.latency_ms`) and the worker log has "received" and
"succeeded in" per task. Time with `time.perf_counter()`; `monotonic()` ticks
at about 16 ms on Windows.

**Accessibility scan for these pages:** `python scripts/kaarya_a11y.py --email
<candidate> --out var/benchmark/kaarya/a11y.json` (320, 375, 768, 1280 px,
light and dark; axe, overflow, console errors, failed requests).

## 10. When implementation contradicts the architecture

It will happen. The procedure is fixed:

1. **Stop.** Do not work around it.
2. **Write down the contradiction**: what the SRS says, what the code needs, and the concrete failure if you follow the SRS.
3. **Propose the smallest change** that resolves it, and name the ADR it amends.
4. **Wait for approval.**

A silent workaround is worse than the original flaw, because the document stops describing the system — and from that moment nobody can trust it about anything else.
