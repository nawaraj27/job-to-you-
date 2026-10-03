# Deployment

The procedure for putting this on a server, and the checklist that says whether
it is safe to. Topology, environments and the pipeline are specified in SRS
§17; this document is the operational half — what to run, in what order, and
what must be true before and after.

**Read the gaps section at the bottom before scheduling a launch.** Two P19
exit criteria are not satisfied by code and have not been satisfied here.

---

## 1. What runs

Six processes, from one codebase:

| Process | Command | Notes |
|---|---|---|
| web | `gunicorn config.wsgi --workers 3 --timeout 30` | Stateless. Scale horizontally. |
| worker (default) | `celery -A config worker -Q default,email,files -c 4` | Everything that is not CPU-heavy. |
| worker (heavy) | `celery -A config worker -Q ai,render,match -c 2` | **Its own container** (§17.5): a burst of PDF renders must not push page renders past the p95 budget. |
| beat | `celery -A config beat` | Exactly one. Two beats double every scheduled job. |
| **outbox** | `python manage.py outbox_worker` | **Not optional, and not a Celery task.** Every side effect in the platform — notification fan-out, webhooks, analytics capture, the pipeline consumers — is queued through the transactional outbox (ADR-A14) and dispatched only by this process. Without it the application looks completely healthy: requests succeed, state changes commit, `/readyz` is green, and *nothing that should happen afterwards happens*. Watch the oldest unprocessed row (§5). |
| — | PostgreSQL 16 + pgvector, Redis 7 | Managed Postgres with PITR; Redis needs no persistence (ADR-A35). |

There is **no Dockerfile in this repository** — see the gaps section.

---

## 2. First deploy

```bash
# 1. Configuration. Every variable is listed in .env.example; production
#    refuses to start without SECRET_KEY, ALLOWED_HOSTS and POSTGRES_PASSWORD.
export DJANGO_SETTINGS_MODULE=config.settings.prod

# 2. Schema.
python manage.py migrate

# 3. Reference data. Each is idempotent and each is required: without them the
#    product cannot answer basic questions about itself.
python manage.py seed_resume_templates   # the template catalogue (P5)
python manage.py seed_plans              # plans and entitlements (P18)
python manage.py seed_legal              # terms and privacy (P19)
python manage.py seed_interview_templates  # practice and live interview templates (P16).
                                         # Without it every interview booking is a 404.
python manage.py import_taxonomy --source esco   # skills (P2)
python manage.py load_prompts            # AI prompts (P11). Without it every AI feature
                                         # fails with PromptMissing.

# 4. The first platform superadmin. Every later staff role is proposed by one
#    superadmin and approved by a second (/staff/team/); this is the one grant
#    with nobody to approve it, and it refuses once a superadmin exists. That
#    person must set up two-factor authentication within 14 days.
python manage.py bootstrap_superadmin you@yourcompany.com

# 5. Static files.
npm ci && npm run build          # Tailwind + vendored JS
python manage.py collectstatic --noinput

# 6. Verify before traffic.
python manage.py check --deploy  # must report zero issues
curl -fsS https://…/readyz       # database, cache and storage
```

**Order matters in one place**: `seed_plans` must run before the first
registration. Every entitlement question resolves through a default plan, and
`entitlements.plan_for` raises `LookupError` when none exists — deliberately,
because a system that silently allowed everything would be worse.

---

## 3. Routine deploy

```bash
python manage.py migrate         # expand/contract, so this is safe before the swap
# deploy the new image / restart processes
python manage.py load_prompts    # after the swap: a changed prompt file becomes a new
                                 # version, and new prompts may expect the new code
python manage.py check --deploy
```

`load_prompts` is idempotent — an unchanged file makes no new version — and it
is the only way an edited prompt reaches the database. Skipping it does not
fail: the previous prompt versions keep running, which is why it is in every
deploy rather than only in the ones that touched `prompts/`.


Migrations run **before** the code that needs them, because every migration is
expand/contract (§5.16 rule 1) and the previous release therefore runs against
the new schema. That property is what makes rollback safe; it is also why no
migration may both add a `NOT NULL` column and backfill it.

Rollback: roll back the image, not the migrations. See RUNBOOKS §7.

---

## 4. Pre-launch checklist

Everything here is checkable, and the ones that are not are marked.

### Configuration

- [ ] `DJANGO_SETTINGS_MODULE=config.settings.prod`
- [ ] `DJANGO_SECRET_KEY` — 50+ characters, from a secret store, never in an image
- [ ] `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` set to the real domain
- [ ] `DJANGO_SECURE_SSL_REDIRECT=1` behind the TLS-terminating proxy
- [ ] `FIELD_ENCRYPTION_KEYS` set and **backed up separately from the database** (ADR-A45) — phone numbers, MFA secrets and webhook signing secrets are encrypted, not hashed, and losing every key loses them
- [ ] `FILES_SCANNER` is **not** `AllowAllScanner` (prod settings refuse it) and ClamAV is reachable
- [ ] `DJANGO_EMAIL_BACKEND` set to a delivering backend (for example `django.core.mail.backends.smtp.EmailBackend`) **and** `EMAIL_HOST` set — the default is the console backend, which prints every verification link and password reset to the log and sends nothing; `check --deploy` refuses it (`common.E003`) and SMTP with no host (`common.E004`)
- [ ] `RATELIMIT_ENABLED=1`
- [ ] `CSP_REPORT_ONLY=0`
- [ ] `python manage.py check --deploy` reports zero issues

### Data

- [ ] `migrate` clean; `makemigrations --check --dry-run` reports no drift
- [ ] `python manage.py load_prompts --check` passes — it names every prompt file whose text or schema differs from the loaded version and exits non-zero; prompts reach the database only through `load_prompts`, and without it the previous versions keep running
- [ ] All four seed commands run
- [ ] Taxonomy imported (~5k skills); "JS" resolves to JavaScript
- [ ] At least one `StaffRole` exists, or nobody can reach `/staff/`

### Processes

- [ ] Both worker containers running, on the queues listed in §1
- [ ] **Exactly one** beat process
- [ ] **The outbox worker is running** — `manage.py outbox_worker`. Verify by emitting an event and watching `OutboxEvent.processed_at` fill, not by assuming: this is the one missing process that leaves a green health check and a dead platform
- [ ] `/healthz` and `/readyz` wired to the platform's health checks
- [ ] Alerts on: 5xx rate, queue depth, **oldest unprocessed outbox row**, AI error rate, AI daily spend, database connections, disk (§17.4)

### Backups

- [ ] Nightly `pg_dump` **and** WAL archiving configured
- [ ] Object storage for private files backed up or replicated
- [ ] Encryption keys backed up separately
- [ ] **Restore rehearsed** against a real dump — *not done; see gaps*

### Legal and product

- [ ] Terms and privacy reviewed by counsel and published at 1.0 with `is_draft=False` — *not done; the shipped documents are drafts and say so*
- [ ] Pricing on the public page matches the seeded plans (it is generated from them, so this is automatic)
- [ ] Feature flags match what is actually built (audited in P19; `career_tools` is the only one off, and it is off because the roadmap is not built)

---

## 5. Monitoring

**Logs** are structured JSON with a request id propagated into Celery (§17.4,
ADR-A37). `LOG_LEVEL=INFO` in production; `DEBUG` will log query-level noise.

**The four numbers worth a dashboard**, in the order they predict user pain:

1. Oldest unprocessed outbox row — every side effect flows through it
2. Web p95 per path — NFR-1's 300 ms budget
3. AI daily spend and error rate — cost and degradation
4. Queue depth per queue — `render` and `ai` are the ones that back up

`/staff/operations/` shows the first, the third and the retention schedule
without needing a dashboard, which is deliberate: the person who needs it during
an incident may not have access to the metrics stack.

---

## 6. Gaps, stated rather than implied

Three things a reader would reasonably assume exist and do not:

1. **No Dockerfile or deployment manifest.** The application runs from a
   checkout with the commands in §1. Containerising it is straightforward — the
   web tier is stateless and every dependency is hash-locked (ADR-A36) — but it
   has not been done, and §17.3's pipeline step "build image" therefore has
   nothing to build.

2. **The restore drill has not been rehearsed.** NFR-16 requires a quarterly
   rehearsal against a real dump, and RUNBOOKS §5 is the procedure. Until it has
   been run once, this deployment has an untested backup.

3. **The load test has not been run against production-shaped
   infrastructure.** `scripts/loadtest.py` exists and works; running it on a
   development server measures the development server. The P19 exit criterion —
   10× expected traffic within the p95 budget — needs staging with a seeded
   corpus, and the number it produces should be recorded here.

All three need infrastructure rather than code, which is why they are listed
here as blockers rather than quietly marked done.

## Live interviews (JaaS)

Live interviews use Jitsi as a Service (8x8). Without it, `check --deploy` fails
with `interviews.E001` / `E002`.

1. Create a JaaS account and app. Note the **AppID** (`vpaas-magic-cookie-…`).
2. Generate an RSA key pair; upload the public key under *API Keys*; note its
   **key id** (`vpaas-magic-cookie-…/abc123`).
3. In the JaaS console, **require authentication**, so a room URL without a
   signed token is refused. This is what makes a forwarded link useless.
4. Set `VIDEO_ROOM_PROVIDER=apps.interviews.providers.jitsi.JitsiRoomProvider`,
   `JAAS_APP_ID`, `JAAS_API_KEY_ID`, and `JAAS_PRIVATE_KEY_PATH` (or
   `JAAS_PRIVATE_KEY` as PEM with `\n` escapes).
5. Verify: book an interview inside its window, join as the interviewer and the
   candidate from two browsers, confirm audio/video both ways, leave and rejoin,
   and confirm the bare room URL (without `?jwt=`) is refused.

Tokens are minted per person at join time: exact room, moderator only for the
interviewer, expiry at the end of the join window, recording and livestreaming
disabled.

## Spoken practice interviews

Transcription runs locally with faster-whisper on CPU. Pre-download the model in
the image build (`python -c "from faster_whisper import WhisperModel; WhisperModel('base.en')"`)
so the first candidate does not wait for a 145 MB download. Audio is transcribed
within the request and deleted; allow request timeouts of at least 60 s for
four-minute answers on slow CPUs.

