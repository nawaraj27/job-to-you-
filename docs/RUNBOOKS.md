# Runbooks

Written for the person on call at 2am who did not build this. Every entry has
the same shape: **what you will see**, **what it means**, **what to do**, and
**what not to do**. The last one is there because most incidents are made worse
by a confident action taken without understanding the mechanism.

Two rules that apply to every runbook here:

1. **Prefer the switch over the deploy.** The AI kill switch, feature flags and
   the rate-limit toggle are all runtime. Reaching for a deploy during an
   incident adds ten minutes and a second variable.
2. **Never edit the database by hand to fix a symptom.** Every state change on
   this platform emits events and writes audit rows; a manual `UPDATE` produces
   neither, and the next person cannot tell what happened. Use the service, the
   staff screen, or the Django shell calling a service.

Screens referenced below: `/staff/operations/` (the board), `/staff/anomalies/`,
`/staff/verification/`, and `/admin/` for record inspection.

---

## 1. The outbox has stopped draining

**What you will see.** `Outbox waiting` climbing on `/staff/operations/`, and
the oldest row aging past a few minutes. Alerting on "oldest unprocessed outbox
row" (§17) fires before users notice.

**What it means.** Every side effect in the platform is queued through the
outbox (ADR-A14) — notification fan-out, webhooks, analytics capture, the
pipeline consumers. If it is not draining, **nothing that should happen after a
state change is happening**, while the state changes themselves keep succeeding.
That is the most dangerous shape of failure this system has: it looks healthy
from the user's side for about twenty minutes.

**What to do.**

1. Is the worker alive? `celery -A config inspect ping`. No reply means the
   worker process is gone — restart it. A `default`-queue worker is the one
   that matters here.
2. Is Redis reachable? `/readyz` checks it. A broker outage stops dispatch but
   loses nothing: the rows are in PostgreSQL and drain when it returns.
3. Are rows failing rather than waiting? Look at `attempts` and
   `dead_lettered_at` in `/admin/` on `OutboxEvent`. A high `attempts` on one
   `type` is a broken consumer, not a broken queue.
4. A single poisonous event blocking a batch is dead-lettered automatically
   after `OUTBOX_MAX_ATTEMPTS`. To take it out of the way sooner, dead-letter it
   from the admin — do not delete it.

**What not to do.** Do not clear the outbox table. Those rows are the only
record that the side effects are owed; deleting them means the emails, webhooks
and analytics for that window never happen and nobody can reconstruct which.

---

## 2. AI is failing, slow, or expensive

**What you will see.** Rising `provider_error` / `timeout` on `AIRun`, or the
AI spend alert, or the `AI cost yesterday` panel showing a task's cost jumping.

**What it means.** Depends on the status, and the statuses are deliberately
distinct (DEVELOPER.md §5.9):

| Status | Meaning | Retryable |
|---|---|---|
| `provider_error`, `timeout` | The provider is having a bad time | Yes, elsewhere in the chain |
| `schema_error` | The model returned the wrong shape | No — same prompt, same result |
| `quota_exceeded` | An owner is out of budget | No — a plan or budget decision |
| `disabled` | The kill switch is off | No — deliberate |
| `erased` | The subject is being erased (§19.4) | Never |

**What to do.**

1. **A provider outage needs no action.** The runner falls back down the chain
   and every feature degrades to its deterministic half (§9.3.6): analysis still
   produces a report, matching still ranks, interviews still run.
2. **To stop all AI spend now**: turn off the `ai` feature flag in `/admin/`
   (Core → Feature flags). It takes effect within 30 seconds — the flag cache
   TTL — with no deploy. Runs then record `disabled`, which is deliberately not
   counted as a provider failure.
3. **A single expensive task**: `AICostDaily` per `task_key` on the operations
   board names it. Lower the per-owner ceiling through the plan
   (`Plan.limits.ai_cost_monthly`) rather than by editing `config/ai.py`.
4. **A schema error storm** after a prompt change: revert the prompt file and
   redeploy. Prompts are version-controlled (ADR-A20) and the eval harness
   should have caught it — check why it did not.

**What not to do.** Do not raise a quota to "unblock" a user during a cost
incident; that is the mechanism working. Do not delete `AIRun` rows to reduce
noise — they are the cost ledger and the eval corpus.

---

## 3. A candidate says their data was not deleted

**What you will see.** A support message, usually about something they can
still see — an application in an employer's list, or a CV they had sent.

**What it means.** Almost always one of the two limits the platform states
plainly and the person did not read (§19.4, and the privacy page):

- **A CV already delivered to an employer stays with the employer.** We cannot
  reach into their records. The `ResumeVersion` behind it is redacted to a
  tombstone, so the content is gone; what survives is the fact of the
  submission and its hash.
- **Applications are anonymised, not deleted.** The employer keeps that
  somebody applied and how it ended, with nothing of the candidate in it.

**What to do.**

1. Find the account. `User.erased_at` set means the walk finished;
   `erasure_started_at` set with `erased_at` null means it is mid-walk or was
   interrupted — the hourly `resume_interrupted_erasures` finishes those.
2. The audit row `account.erased` carries per-store counts. That is the record
   of what was removed and is the answer to "did it run".
3. If the request is genuinely unfulfilled — a store that was not walked — that
   is a defect, not a support issue. `apps/accounts/services/erasure.py` lists
   the stores; the test suite asserts the list matches §19.4.

**What not to do.** Do not run `erase()` by hand against a live account to
"make sure". It is idempotent, but it also anonymises the account row, and doing
that to somebody who only asked a question is not recoverable.

---

## 4. An employer is scraping candidate data

**What you will see.** Rows on `/staff/anomalies/` — large exports, a spike in
profile views, or many exports in a day (§15.4).

**What it means.** A signal, not a verdict. A recruiter closing a role can
legitimately view forty profiles in an afternoon. The pattern that matters is
volume without corresponding hiring activity: exports with no applications
moving, or views spread across every candidate rather than a shortlist.

**What to do.**

1. Read the audit trail for that company: `candidate.exported`,
   `candidate.searched`, `candidate.viewed`. `AttachmentAccess` shows what files
   were opened.
2. Talk to them first. Suspension is the last step, not the first.
3. If it is abuse: `company.suspend_company` through the service — it cascades
   correctly (§2.6) and is audited. Removing `candidate.export` from the
   membership is the smaller intervention and is often enough.

**What not to do.** Do not tighten the global rate limits in response to one
company; that punishes everybody for one account's behaviour. The per-membership
search limit is already there for exactly this.

---

## 5. Database restore

**What you will see.** Hopefully a rehearsal. Otherwise: data loss, corruption,
or a bad migration that cannot be rolled forward.

**What it means.** RPO 15 minutes via WAL archiving, RTO 4 hours (NFR-16). The
restore is only real if it has been rehearsed — see the gap noted at the end of
this file.

**What to do.**

1. Stop the web and worker processes. A restore with writers attached produces
   a database nobody can reason about.
2. Restore the base backup, then replay WAL to the target time:
   `pg_basebackup` output plus `recovery_target_time`.
3. `python manage.py migrate --check` — the restored schema must match the
   deployed code. If it does not, you have restored to a point before a
   migration and must deploy the matching revision, not migrate forward.
4. Verify before opening the doors: row counts on `User`, `Application`,
   `ResumeVersion`; `/readyz`; one signed file download.
5. Re-enable writers. The outbox drains whatever was in flight.

**What not to do.** Do not run `migrate` to "fix" a schema mismatch after a
point-in-time restore. Deploy the code that matches the restored schema.

---

## 6. Every AI feature is off and nobody switched it off

**What you will see.** `AIRun` rows with `disabled`, and users reporting
missing suggestions while everything else works.

**What it means.** Either the flag is off, or the flag *row* is off. Check
`/admin/` → Feature flags → `ai`. Remember expired flags are ignored and fall
back to the code default (`True`), so an expired `ai` flag switches AI **on**,
not off.

**What to do.** Set the flag, or delete the row to fall back to the code
default. Then ask who turned it off and why — the change is audited.

---

## 7. A deploy needs rolling back

**What to do.**

1. Roll back the application image. **Do not roll back migrations.** Every
   migration in this project is expand/contract (§5.16 rule 1), which means the
   previous release runs against the new schema. That is the property that makes
   a rollback safe, and it is why no migration both adds a `NOT NULL` column and
   backfills it.
2. If the bad release included a data migration, stop and read it before doing
   anything. Rolling back code is safe; reversing a backfill usually is not.
3. Check the outbox after rollback — consumers from the newer release may have
   left events whose types the older one does not know. Unknown types are
   dead-lettered, not lost.

---

## 8. Retention: proving what was deleted

**What you will see.** A question from a customer, a candidate, or counsel:
"what do you keep, and for how long?"

**Where the answer is.** `/staff/operations/` lists every promise in §15.3 with
the job that keeps it, and `apps/common/retention.py` is the single definition
those rows come from. A rule with no scheduled job is a promise nothing
enforces; the test suite fails if one exists.

**To prove a sweep ran**: the task logs a count per run
(`purged soft-deleted rows`, `purged interview transcript turns`, and so on),
and erasures write an `account.erased` audit row per account.

---

## Known gap, stated rather than implied

**The restore drill has not been rehearsed on this deployment, and the load test
has not been run against production-shaped infrastructure.** Both are P19 exit
criteria (§20) and neither is satisfied by code: they need a staging environment
with a seeded corpus and a real backup target. The harness for the second is
`scripts/loadtest.py`, and the procedure for the first is §5 above. Until both
have been done and their numbers recorded here, this platform has an untested
backup — which, as NFR-16 says, is not a backup.
