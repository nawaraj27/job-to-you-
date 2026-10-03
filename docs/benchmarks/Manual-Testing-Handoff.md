# CareerAI — Manual Testing Handoff (implementation freeze, 2026-09-24)

State: **IMPLEMENTATION COMPLETE → LOCAL VERIFICATION COMPLETE → PRODUCT DECISIONS COMPLETE → MANUAL/EXTERNAL VERIFICATION ONLY.**

Development is frozen. Nothing under "B" has been performed by a human tester yet. Everything under "A" was verified on this machine, by automation and by driving the in-app browser as the demo personas (see "What was and was not clicked" below). Everything under "B" is **Pending** or **Blocked** until a tester records evidence in the evidence log of [`Manual-Verification-Consolidated.md`](Manual-Verification-Consolidated.md). That file is the source of truth for row status; this handoff says how to run the rows.

> **2026-09-26, final autonomous production-readiness gate (ledger §18, `Autonomous-Release-Gate.md`) — this freeze replaces the decision-benchmark freeze.** Ten defects fixed (email deploy check, stalled interview scoring recovery and time limits, an open redirect, profile toggle names, email addresses in logs, prompt loading, deploy docs, contact-line location, undocumented settings). OCR measured and still not integrated. **After pulling: `python manage.py migrate` (no new migration), `python manage.py load_prompts` then `python manage.py load_prompts --check`, `python manage.py reparse_resume_uploads` (parser engine 1.4), then restart the Celery workers and beat (beat gains `recover-unscored-interviews`).** New manual row: stop the AI worker after a practice, wait, and confirm the result page moves from "being read" to "could not be read" after 24 hours (or check the sweep re-queues scoring after 20 minutes).
>
> **2026-09-26, local decision model benchmark (ledger §17, `Local-Decision-Model-Benchmark.md`) — this freeze replaces the AI grounding gate freeze.** Benchmark only: no candidate local model beat the current setup, nothing was integrated, and production code is unchanged. Adds `scripts/decision_benchmark.py`.
>
> **2026-09-25, AI quality and grounding gate (ledger §16, `AI-Grounding-Gate.md`) — this freeze replaces the system-audit freeze.** Scoring judges each answer against its own question and cannot praise what was not said; AI scores can no longer be duplicated; the CV assistant allows rewording but refuses added charge, results and invented titles; interview questions no longer treat the employer's product as the candidate's past. **After pulling: `python manage.py migrate` (adds `interviews/0004`), `python manage.py load_prompts` (scoring prompt v6, assistant prompt, plan prompt v9 — without it the old prompts keep running), then restart the Celery workers.**
>
> **2026-09-24, measured system audit (ledger §15, `System-Audit-2026-09-24.md`) — this freeze replaces the quality-gate freeze.** Parser fixes: bulleted projects and bulleted skill lists in Word files were lost; parser engine is now 1.3 — after deploying, run `python manage.py reparse_resume_uploads` so older uploads are read again. The assistant no longer repeats its question. No migration.
>
> **2026-09-24, final quality gate (ledger §14, `Final-Quality-Gate.md`) — this freeze replaces the earlier 2026-09-24 one.** No features; seven defects fixed: readiness said "ATS: Incomplete" when the analysis was only out of date; "skip" at a gap question waited on the model; job practice titled "General practice"; the demo interview stayed "Scheduled" after its time; past unbooked interview times stayed "Open"; a reschedule notified twice; the candidate's interview history heading. No migration.
>
> **Earlier 2026-09-24 freeze:** this freeze replaced the 2026-09-21 second-pass freeze. A manual-testing gap loop (ledger §13) added: a CV-progress chip on every page with Close / Minimize / Continue browsing and a "CV read" notification; guided refusals (Upgrade plan + View plans, verification benefits, readiness with ATS line and Improve CV); solid warning chips instead of dashed ones; company updates (announcements, events, verified) for followers with a "Companies you follow" preference; skill-specific interview questions; CV-gap questions in the assistant; ✓/⚠ reasons on recommended jobs; a verified demo employer and a complete demo profile. Two migrations: `companies/0006`, `notifications/0004` — run `python manage.py migrate`.
>
> **Earlier (2026-09-21, second pass):** An adversarial verification pass (ledger §12) re-tested §11 against the product's behaviour rather than its tests and found 19 defects (EVD-01..06 counted once) that §11 had reported as verified or had not reached — among them duplicate upload lists, a report that re-rendered under the pointer, multi-word skills that could never count toward readiness, preferred skills failing readiness, a probe that asked nothing for "I used Python at Phewa Stays", an invented skill passing the grounding check, a crashing embedding task, red error boxes for permission refusals, lost tasks leaving work "pending" for ever, and the A11Y-01 contrast failure (which §11 wrongly said nothing used). All are fixed, with tests. The first 2026-09-21 manifest no longer describes the tree.

---

## A. Implementation freeze

| Item | Value |
|---|---|
| Version control | The project folder is **not its own git repository**, so there is no commit hash. The frozen tree is identified by a SHA-256 manifest |
| Tree fingerprint | `var/benchmark/freeze-manifest-2026-09-27.sha256`: 976 files, same scope as 2026-09-26b. Its own SHA-256 is `de846e072ac02ac93453c9f3c4a51738e902fb031419b4576d100867b5c7c2f7`; metadata in `freeze-manifest-2026-09-27.json`. This handoff was written after the manifest and is the only file not listed in it |
| Last code change | 2026-09-27, ledger §19 (manual-testing pack: demo seed and verification scripts only; no product change) |
| **Final full suite** | **Ran 3949 tests in 1153 s — OK** (serial, `--settings=config.settings.test`, 2026-09-27 manual-testing pack, final tree; 10 more than 2026-09-26, none removed). Log: `var/benchmark/full-suite-manual-pack-2026-09-27.log` |
| ruff check / format | clean, 706 files |
| mypy `apps` | Success, 379 source files |
| `makemigrations --check --dry-run` | No changes (this loop adds `companies/0006_company_updates`, `notifications/0004_company_updates`) |
| `manage.py check` | No issues (1 silenced) |
| Sweeps (dev database, 2026-09-27) | link 1098 visited / 0 broken; notification links 121 / 0 broken; hostile POST 5264 / 0 failures; site 1918 rows / 0 failures; IDOR 6 pairs, 328 GET + 554 POST replays / 0 leaks, 0 writes |
| axe + overflow (2026-09-24 gate) | loop screens 40, employer/staff 36, whole site 72, CV builders/ops 16, CV templates/verification 28 — 192 scans, 0 serious/critical, 0 overflow; UI polish audit 40 renders, 0 stray decorations / clipping / sideways scroll; screenshots in `var/benchmark/screens/` |
| Contrast | every status badge ≥ 4.5:1 on its own tint in both themes, measured in the browser (`badge-caution` 4.44 → 5.13:1) and held by `StatusContrastTests` |
| Product decisions | D-01..D-13 unchanged. No decision added |
| Open local issues | **None.** A11Y-01 is fixed |

### Demo accounts (local development only)

Created by the real services — no bypass, two-factor unchanged. Every account signs in with **`CareerAI!Demo24`** (a printed fixture password on the reserved `example.com` domain, which cannot receive mail; it is not a production secret). Checked with `authenticate()` for all five on 2026-09-21.

```bash
python manage.py migrate
python manage.py seed_demo --personas-only
```

Idempotent: a second run changes nothing. Two deliberate exceptions, so the demo can be repeated: if a tester has **published the Data Analyst draft**, the next run writes a fresh draft beside it (the published one stays); and a demo certificate whose virus scan never ran is queued again. `--personas-only --reset` is refused on purpose: finalized CV versions are immutable at the database and verification evidence is protected.

| Role | Email | What is already there |
|---|---|---|
| Candidate | `demo.candidate@example.com` (Maya Karki) | Pro plan (manual payment) so target-job analyses can be repeated; a **100% complete profile** (Pokhara, Python/Django/PostgreSQL/Git, work history, education, availability, salary, a link); CV "Maya Karki CV"; one analysis; an application to **Backend Developer** at Summit Digital Labs; a **live interview booked** three days ahead (re-running `seed_demo --personas-only` moves it forward once its time has passed, and each side gets one "Interview moved" notice); a **practice interview for Backend Developer** open with job-aware questions; **follows Summit Digital Labs**, not Lakeside Logistics |
| Employer (owner) | `demo.employer@example.com` (Rohan Adhikari) | Summit Digital Labs, **verified** (both certificates, decided by the demo moderator); published **Backend Developer**, **UI/UX Designer**, **Marketing Specialist**; one announcement and one upcoming event under **Updates for followers**; a **Data Analyst draft** — publish it to watch Maya's follower notice arrive; Maya's application; the booked interview |
| Employer (owner) | `demo.employer2@example.com` (Sunita Rai) | Lakeside Logistics with one published job — the company Maya does **not** follow |
| Employer (owner) | `demo.verify@example.com` (Kiran Thapa) | Gandaki Traders, verification **submitted with the registration certificate only** — the queue shows "Not sent: PAN or VAT certificate" |
| Staff (moderator) | `demo.staff@example.com` (Asha Pokharel) | A `StaffRole` of moderator; reaches the verification queue and can open the certificate |
| Candidate (interview ready) | `demo.interview@example.com` (Arjun Shrestha) | CV; application to **UI/UX Designer** moved by the employer to the **Interview** stage, contact details unlocked; three open times five days out to **book**; a job-aware **practice interview for UI/UX Designer** open |
| Employer (Company admin) | `demo.admin@example.com` (Bina Tamang) | Admin at Summit Digital Labs (joined by invitation): company-wide applicant read, billing view-only (D-11) |
| Employer (Recruiter) | `demo.recruiter@example.com` (Nabin Gurung) | Recruiter at Summit Digital Labs, **assigned to Backend Developer only** — sees Maya, not Arjun (UI/UX) or Sita (Junior Data Scientist): D-13 |
| Candidate (second) | `demo.candidate2@example.com` (Sita Magar) | Own CV; applications to **Junior Data Scientist** (Summit) and **Operations Coordinator** (Lakeside); follows Lakeside, not Summit — the other side of every cross-user and cross-company check |
| Candidate (no CV) | `demo.newcandidate@example.com` (Ramesh Bhatta) | Signed up and confirmed, nothing else: empty states and the CV upload pack |

**Manual-testing pack (2026-09-27, ledger §19).** Summit also publishes **Junior Data Scientist**
(required Python, SQL, Machine learning; preferred pandas, Tableau). Local-only files, not in
the freeze and git-ignored under `var/manual-testing/`: `MANUAL-TEST-CREDENTIALS.local.md`
(every account, its data and the tests it is for), `MANUAL-TEST-PLAN.local.md` (M-01…M-28,
with what is already automated), `cvs/` and `CV-TEST-PACK.local.md` (nine synthetic CVs with
expected extraction). Rebuild the CV pack with `python scripts/build_manual_cv_pack.py`; check
every account with `python scripts/manual_env_check.py` (51 checks). Sign-in limits are 5 per
account and 20 per address per 15 minutes; the credentials file has the local reset command.
Owners, admins and staff must set up MFA after the 14-day grace (C, F, H, I from 2026-10-05;
D from 2026-10-11).

**Background processes the flows need** (the first handoff listed only one worker, which is not enough — a CV is read only after the outbox delivers "file scanned"):

```bash
python manage.py outbox_worker
celery -A config worker --queues=default,files,render,email --pool=solo
celery -A config worker --queues=ai --pool=solo -n ai@%h
celery -A config beat
```

With Ollama running locally (`OLLAMA_BASE_URL`), the assistant drafts and the analysis review is written; without a model the questions still work and drafting says it is unavailable.

### What was and was not clicked (2026-09-21, second pass)

Driven in the in-app browser, one persona per origin: upload → progress → away through the rail and typed URLs → back → same CV → analysis → refresh; the late-review state and "Check again"; target-job analysis for Backend Developer and UI/UX Designer; "Answer questions about these" and a full question loop answered by typing, ending in a drafted suggestion accepted with "Use this"; re-analysis showing the updated gaps; practice interviews for UI/UX Designer and Marketing Specialist started and discarded; follow/unfollow/follow/unfollow on a company page; the employer publishing the draft; the follower's notification opened; a free candidate's plan refusal; a Viewer's permission refusal; the staff queue; keyboard focus on a job page.

**Not** done by a person or not done in the browser: typing the password into the sign-in form (sessions were minted after `authenticate()` checked it); screenshots (the pane would not draw while the app window was in the background); Enter-to-submit on the assistant (the tool's key event did not submit; clicking did — recorded as unverified, not as a defect); downloading the certificate (checked with a fetch: 200, `application/pdf`); screen readers; real devices. Those remain rows under "B".

### What this freeze changed since the first 2026-09-21 freeze

Reproduction, root cause and test for each item: [`Loop-Closure-Ledger.md`](Loop-Closure-Ledger.md) §12.

1. **CV progress (UPL-01, UPL-02, REF-01, POLL-01, STALL-01, ENR-01).** One uploads list that refreshes itself and brings "Found in your CV" with it; the progress page stops asking when nothing is moving; "Latest analysis" and a refresh reopen the report you were on; the report no longer re-renders under the pointer while the review is written; a lost scan, read or review is recovered by a ten-minute sweep instead of waiting for ever; a review is written once however many tasks try.
2. **Readiness (TGT-01, CVR-02, SCR-01).** Multi-word skills are found; readiness is judged on the posting's *required* skills and names preferred ones as advice; the skills-score note says it reads the skills list.
3. **Assisted CV (EVD-01..06, GAP-01, FAB-01).** The probe asks only for the missing piece in the cases the brief listed; a "no" moves to the next gap without a model; required gaps come first; a bullet may name only skills the candidate named.
4. **Interviews (INT-04b).** Discipline practice questions (engineering, design, marketing, data, people); no debugging question for designers; company introductions are not duties; the banner says the questions are built from the posting; editing responsibilities rewrites the kit.
5. **Restrictions (RES-03, RES-04).** View-caught refusals keep their real status; every redirected refusal is a warning (not a red alert) with who can help; a Company Admin is shown the plan they can see.
6. **Platform (EMB-01).** Embedding tasks no longer crash outside a test transaction.
7. **Contrast (A11Y-01).** `caution-700` darkened within its hue.
8. **Demo (DEMO-02, DEMO-03).** The draft-and-notice demo can be repeated; the demo certificate becomes openable.

### Known gaps that are not defects (stated so nobody assumes them done)

- **FR-11.3 bounce/complaint ingestion is not built** (row 1.3y).
- **Payment gateway adapters (eSewa/Khalti/Fonepay) are deferred** by SRS §21 (row 1.3l).
- **Terms and privacy are `0.1-draft`** until counsel reviews them (row 1.6).
- **Interview benchmark**: INT-02 blocked on a hosted model (row 3.5); single-rater labels (row 3.6).
- **An incomplete verification submission is not refused** (VER-01, by design).
- **The job-aware fallback is templated.** It names the posting's own skills and duties and asks discipline-specific practice questions; a model's kit is still richer.
- **Readiness and the skills score read different things on purpose**: readiness reads the whole CV, the score reads the skills list. A skill shown only in a bullet counts for readiness and is "not listed" in the score.

---

## B. Manual test matrix

**Record for every row:** date, tester, environment (dev, staging or production), device, OS, browser and version (plus the screen reader for P2), pass/fail, and notes. Add screenshots where a row asks for them.

**Tag legend (Needs):** EMAIL = real SMTP provider plus Gmail/Outlook inboxes · PAY = real payment gateway · DEV = real phone/microphone/camera · SR = screen reader · JAAS = 8x8 JaaS account · AI = hosted AI key · CLAM = ClamAV daemon · BEAT = Celery worker + Beat · STG = staging stack · BKP = backup/alerting infrastructure · LEGAL = counsel · HUMAN = a second person · 2B = two browsers/profiles.

**Standard prerequisite "DEMO":** the dev server running on port 8137 (or staging), `docker compose up -d`, a worker running `celery -A config worker --queues=default,files --pool=solo`, demo data from `seed_demo`, and the demo and sweep accounts (`ops@careerai.example.com` superadmin; `sweep.*@example.test` personas; fonepay and kora companies). For EMAIL rows, `EMAIL_HOST` must point at the real provider, not the console backend.

### Priority 1 — would stop launch

| ID | Feature / workflow | Prerequisite | Steps | Expected result | Evidence | Needs |
|---|---|---|---|---|---|---|
| 1.1 | Live video interview | JaaS app, `JAAS_APP_ID`, `JAAS_API_KEY_ID`, RSA key, "authentication required" on, `check --deploy` without `interviews.E001/E002`; two devices | [`Interview-Manual-Verification.md`](Interview-Manual-Verification.md) §A1 setup, then §A2 steps 1–12 | Every "Pass when" in §A2: same room, moderator only for the interviewer, a bare URL and an expired link refused, a non-panel member refused, rejoin within 30 s, recovery after a 20 s drop, no recording | Per-step pass/fail; join times; screenshots of the refusals | JAAS, DEV, 2B |
| 1.2 | Voice practice | DEMO candidate; real microphone and camera | Interview checklist §B steps 1–16 in Chrome, Edge and Firefox (Windows), Safari (macOS, iOS) and Chrome (Android) | Every "Pass when" in §B; no audio files left in the server temp directory (step 16) | Matrix of step × browser; transcript samples; the temp-directory listing | DEV |
| 1.3 | Application emails | EMAIL; DEMO company with owner, admin, an assigned recruiter and an **unassigned** recruiter; a candidate inbox | Apply; move through a stage marked private; reject with a reason; hire; send an offer; accept one and decline another; withdraw; book, move and cancel an interview | Each email arrives once, shows no internal stage name or raw codes, and uses correct role names. Team emails reach the owner, admins and the recruiter or hiring manager **assigned** to the job only (D-13) | Screenshot of each email in Gmail, Outlook and a phone mail app; list of recipients | EMAIL |
| 1.3b | Message notifications | EMAIL; candidate and employer in a thread | Send a message each way; mute the thread and send again | Email and in-app notice show the sender and their role; the link opens the right thread in each portal; a muted thread sends nothing | Screenshots; list of what was received after muting | EMAIL, 2B |
| 1.3c | Talent CSV export | Owner with `candidate.export`; a candidate whose headline is `=HYPERLINK("http://x","y")` and one with a Nepali or accented name | Export; open the file in Excel (Windows), LibreOffice and Google Sheets | The formula shows as text; names are not garbled | Screenshot per application | — |
| 1.3d | Privacy narrowing and blocking | Public candidate; recruiter with access | Recruiter opens the profile. The candidate switches to "Employers you've applied to" and the recruiter reloads. Separately, the candidate blocks the company from a conversation and the recruiter reloads | 404 on reload; the candidate is gone from search both times | Screenshots before and after | 2B |
| 1.3e | Download my data (JSON) | Candidate with applications and messages, at a stage marked private | Your data → download the JSON | Applications, messages, profile views and access are present; only the public stage name appears | The JSON with private details redacted | — |
| 1.3f | Team invitation round trip | EMAIL; owner; an invitee address with no account | Invite; open the email; sign up from its link; see "Check your email"; confirm from the real email; sign in. Withdraw another invitation and open its link. Open a link while signed in as a different address; the owner withdraws and re-sends to the address actually used | Link survives the mail client; the invitee lands back on the invitation and accepts; the withdrawn link says it is no longer valid; the mismatch page offers Sign out (D-03); the re-sent invitation accepts | Screenshots of each page; the email source showing link integrity | EMAIL |
| 1.3g | Verification decision notices | EMAIL; staff reviewer; company with owner, admin and recruiter | Staff decides "needs more information", then "not verified", then "verified" (separate companies if needed) | Owners, admins and `company.edit` holders get email and in-app notices, the recruiter gets nothing, the reason is shown, and the link opens the verification screen | Recipients per decision; screenshots | EMAIL |
| 1.3h | Virus scanning of verification files | `FILES_SCANNER=ClamAVScanner`, clamd reachable | Upload a real certificate; upload the EICAR test file | The certificate opens on the staff queue; EICAR is quarantined and shows "cannot be opened" | Screenshots; clamd log line | CLAM |
| 1.3i | Verification claim lock | Two staff moderators | A claims; B tries to decide and release; wait 45 min (or use a staging clock) | B is refused with a message; after 45 min the item returns to the queue | Screenshots; time of expiry | 2B |
| 1.3j | Weekly digest and quiet hours | EMAIL, BEAT on the `email` queue | Let Monday morning (Asia/Kathmandu) run; start a second worker or a retry. Set quiet hours around now and trigger a message | One digest listing only unread items, rendered in Gmail and Outlook, no duplicate; during quiet hours no email but the in-app notice appears | Email timestamps; worker logs | EMAIL, BEAT |
| 1.3k | Unpaid expiry and bank transfer | STG copy with BEAT (or tasks run with a `day` argument); staff with Django admin | Candidate A moves to Pro and never pays. Candidate B pays by bank transfer recorded in admin. Candidate C pays only the newer of two invoices | A loses Pro 14 days after the period ends and has one invoice; B stays active; C stays past due; the wording reads correctly on candidate and company screens | Screenshots per candidate; invoice list | STG, BEAT |
| 1.3l | Gateway callback | Gateway adapters built (deferred, SRS §21); merchant credentials | Pay via eSewa, Khalti and Fonepay; replay the callback | Signature, amount **and currency** verified; a replay records one payment | Callback logs; payment rows | PAY (blocked, adapters not built) |
| 1.3m | Domain proof | EMAIL; company website on its own domain; a mailbox on that domain | Verification → "Your domain" → send → click the link → "Confirm <domain>" | Link survives the mail client; the screen shows "Proven"; the staff queue shows "domain … proven by work email" | Screenshots | EMAIL |
| 1.3n | Scanning of offer letters and message attachments | CLAM | Send an offer letter and a message attachment; send EICAR | "Being checked for viruses" until scanned, then only the recipient can open it; EICAR never opens | Screenshots | CLAM |
| 1.3o | Ownership nomination | Owner and a member | Owner nominates; member sees the banner and accepts; old owner reloads | Old owner is now admin; both receive the notification | Screenshots | 2B |
| 1.3p | Data zip export | Candidate with uploaded CVs | Download the zip in Chrome, Firefox and Safari; open it with the OS unzip tool | Archive opens; CVs open; `careerai-export.json` lists `original_files` | Screenshot per browser | — |
| 1.3q | Operations under load and Redis outage | STG with real Redis, workers, Beat and a load-balancer health check; staff account | Queue a burst of renders and analyses; stop the worker; restart it; stop Redis for 2 min; restore it | Queue depth rises and drains; outbox "Oldest" grows and recovers; pages stay up (slower); the board shows the cache error and broker unreachable; `/readyz` 503 and the node is taken out; everything recovers and nobody is signed out | Board screenshots at each phase; LB health log | STG, BEAT |
| 1.3r | AI cost tracking | `OPENROUTER_API_KEY`; one real day of traffic | Compare "AI today" with the provider dashboard; next morning compare "AI cost yesterday" | Match within rounding | Both dashboards' screenshots | AI |
| 1.3s | Password reset | EMAIL; two browsers signed in | "Forgot your password?"; use the link; click it again; check the other browser; try an unknown address | One email; the password is set once; the second click says expired; other sessions signed out; the unknown address shows the same message and receives nothing | Screenshots; inbox evidence | EMAIL, 2B |
| 1.3t | Two-factor | Phones with Google Authenticator, Microsoft Authenticator and 1Password; staff account; owner past the 14-day grace period (or a staging date shift) | Set up by typing the key; sign in with a code; use a recovery code twice; regenerate codes; try to turn it off as staff or owner; sign in as the staff account and the owner | Codes work; a recovery code works once; turning off is refused for staff and owners/admins; both are forced to Security first | Screenshots | DEV |
| 1.3u | Staff role approval | Two superadmins and a third account; 3 browsers | A proposes a moderator; A tries to approve; B approves; the new moderator signs in; a superadmin removes the role while the moderator's browser is open | A cannot approve; the moderator is asked to set up 2FA within the grace period and reaches the queue; after removal, their next click signs them out | Screenshots | 2B |
| 1.3v | Impersonation | Support staff account; candidate; Django admin access | Impersonate (including on a phone); open Security and Your data; save a job; Stop; leave a second tab open for over an hour | Banner on every page; Security and Your data refuse; the audit log shows the agent as actor and the person as on-behalf-of; Stop returns to the staff page; the stale tab is back to the agent | Screenshots; audit row | DEV |
| 1.3w | Company suspension | EMAIL; staging company with a booked interview | Suspend with a reason; the candidate tries to reply; check the public postings; reinstate | Candidate gets cancellation email and in-app notice; owner gets the suspension email with the reason; reply refused; postings gone; the reinstatement notice opens the overview | Screenshots; emails | EMAIL, STG |
| 1.3x | Sign-up without disclosure (D-12) | EMAIL; a new Gmail address and a registered Outlook address | Sign up with each in separate browsers | Identical "Check your email" page, not signed in; new address gets "Confirm your email address"; existing one gets "You already have a CareerAI account" with working links; both render in Gmail, Outlook and phone mail | Screenshots of both pages and emails | EMAIL |
| 1.3y | Bounce and complaint suppression (FR-11.3) | **Not built.** Needs a provider with bounce/complaint webhooks, then an adapter calling `channels.suppress()` | After the adapter exists: send to a hard-bounce test address; file a complaint; send again | Address suppressed; next email not sent | Provider event and suppression row | EMAIL (blocked) |
| 1.4 | Production deploy smoke | STG with real secrets; DEPLOYMENT.md checklist | `check --deploy`; sign up a candidate and an employer; apply; move; offer; accept | Zero deploy issues; journey completes | Command output; screenshots | STG |
| 1.5 | Backups and alerts | Backup target, restore host, alerting service | Configure nightly `pg_dump` + WAL; rehearse a restore; fire each alert (5xx, queue depth, oldest outbox row, AI errors, AI spend, DB connections, disk) | Restore succeeds (NFR-16); every alert fires | Restore log; alert screenshots | BKP |
| 1.6 | Terms and privacy | Counsel | Counsel reviews `0.1-draft`; publish 1.0 with `is_draft=False` | Draft banner gone; text approved | Counsel sign-off | LEGAL |
| 1.7 | CV template journey | DEMO; a candidate account with **no** CV (register a fresh one); a browser | Open `/cv/templates/`; read a card; open one template; press "Start a CV from this example"; in the editor replace the name, the summary and the first role with your own; use "Try another template" and pick a second; switch again from the editor's template select; switch back to the first; export PDF and Word | Every card shows the same complete example in a different layout; the editor arrives populated with a notice saying so and the candidate's own name and email at the top; after editing, every template shows the edited words; "Your CVs" still lists exactly one CV; both exports match the template on screen | Screenshots of the gallery, the editor on arrival, the same CV in three templates, and the two exports | — |
| 1.8 | Verification completeness | DEMO; a new company registered through `/employer/new/`; a moderator account | As the owner: read the Documents checklist before uploading anything; upload only the registration certificate; read the warning; submit. As the moderator: open `/staff/verification/`, read the "Not sent" line, claim, choose "More information needed" with a reason. As the owner: read the reason, upload the PAN certificate, submit again. As the moderator: claim and verify | The company is "Not submitted", then "In review", then "More information needed", then "In review", then "Verified" — never verified before the approval; the resubmission is still attempt 1; "Earlier decisions" keeps the first reason; the verified badge appears on the company page only at the end | Screenshots of each state on both sides; the queue row before and after | 2B |
| 1.9 | CV progress and return | Demo accounts; worker running | As `demo.candidate`: Consolidated row 1.9 | Nothing restarts or duplicates; closing never cancels; result reachable from Uploads and Overview | Screenshots: progress, Overview note, Uploads after reading | — |
| 1.10 | Company follow and notice | Demo accounts; worker running | Follow on Summit Digital Labs as `demo.candidate`; publish **Data Analyst** as `demo.employer`; read the notice; unfollow; publish again | One notice, opens the job; none for others; none after unfollow; none with "New job matches" off | Screenshots of the notice and the settings switch | 2B |
| 1.11 | Interview relevance | Demo accounts | Open the ready Backend Developer practice; start UI/UX Designer and Marketing Specialist practice | Each names its posting's own skills and duties | The three question lists | AI (optional) |
| 1.12 | Assisted enhancement | Demo accounts | Consolidated row 1.12 | Readiness Incomplete with the gap; evidence questions once each; nothing written until accepted; nothing invented | Transcript; the CV before/after | AI for drafting |
| 1.13 | Restriction messages | Demo accounts + a free candidate | Consolidated row 1.13 | Warning with next step, site shell intact; a real outage still shows as an error | Screenshots | — |
| 1.14 | Demo seed | Fresh local database | `migrate`, then `seed_demo --personas-only` twice; sign in as each persona | Second run silent; every account works | Command output | — |

### Priority 2 — screen readers

Prerequisite for all rows: DEMO data; NVDA with Firefox or Chrome (Windows), VoiceOver with Safari (macOS/iOS), TalkBack with Chrome (Android). **Steps:** complete the journey using only the screen reader and keyboard (or touch on mobile). **Expected:** every item listed in [`Manual-Verification-Consolidated.md`](Manual-Verification-Consolidated.md) Priority 2 for that row is announced as described. **Evidence:** screen reader and version, browser, pass/fail per item, and a short transcript or recording of any failure. **Needs:** SR (TalkBack also DEV).

| ID | Journey (persona) |
|---|---|
| 2.1 | Candidate apply form |
| 2.2 | Employer applicant board and bulk move |
| 2.3 | Employer applicant detail, notes, offer |
| 2.4 | Pipeline settings table |
| 2.5 | Job search, save toggle, saved searches |
| 2.5b | Both inboxes, mute, report/block |
| 2.5c | Talent search and export; candidate privacy and data screens |
| 2.5d | Employer verification screen |
| 2.5e | Team screen and ownership transfer |
| 2.5f | Staff verification queue and anomalies |
| 2.5g | Notification feed and preferences |
| 2.5h | Candidate plan and company billing |
| 2.5i | Closure-run screens (blocked companies, attachments, nomination, domain, skills, analytics, offer, screening answers) |
| 2.5j | Operations board; guided CV builder step |
| 2.5k | Security settings, reset, sign-up sent, 2FA prompt, staff Companies/People/Staff roles, impersonation banner, Viewer and Interviewer views |
| 2.5l | D-01..D-10 screens (invite mismatch, reviewer conflict, attempt 2, short-term refusal, privacy consequences, scheduled downgrade, employer settings rail) |
| 2.6 | Interview screens (Interview checklist §C 1–9) |
| 2.5m | CV "Start from" choice, the worked-example notice in all three builders, template detail "example CV" / "your CV" sentence and "Try another template"; employer verification document checklist and incomplete-submission notice; staff queue "Not sent" line |
| 2.5n | Uploads/progress, Follow button, CV readiness, target-job select, assistant evidence questions, restriction boxes |
| 2.7 | CV upload, analysis, builder modal focus trap, export |

### Priority 3 — real browsers, devices and people

| ID | Feature | Prerequisite | Steps | Expected | Evidence | Needs |
|---|---|---|---|---|---|---|
| 3.1 | Keyboard-only apply and bulk move | Firefox and Safari (Playwright Firefox crashes on this machine) | Apply to a job and bulk move applicants without a mouse | Every control reachable, visible focus | Pass/fail per browser | — |
| 3.3 | Job edit conflict in a real browser | Two recruiters on one posting | Both open edit; A saves; B saves | B refused with a message **and B's typed edits stay in the form** (the refusal itself is covered by tests) | Screenshot of B | 2B |
| 3.4 | Real phones | Small Android and an iPhone | Apply, applications list, employer board | No sideways scroll; controls reachable with a thumb | Screenshots | DEV |
| 3.4b | Two-person thread | Candidate and employer | Exchange replies and reload; candidate blocks; employer sends | Replies and unread badges update on reload; the send after the block is refused with a message | Screenshots | 2B |
| 3.5 | Hosted model quality | `OPENROUTER_API_KEY` | `python manage.py interview_benchmark --live --only grounding,followup --repeat 2` | Band agreement, correction handling and latency compared with the local results in `Interview-Ecosystem.md`; INT-02 re-scored | Benchmark output | AI |
| 3.6 | Second rater | A second person | Independently label the interview benchmark ranges and relevance | Disagreements recorded against the single-rater labels | Label sheet; comparison | HUMAN |

---

## C. Test order (grouped by setup)

1. **Session 1: local dev and ordinary devices, no external accounts.** Set up DEMO once and run `seed_demo --personas-only`. Run 1.14, then 1.9–1.13 with the demo accounts (the newest code), then 1.7 and 1.8, then 1.3c, 1.3e, 1.3p, then the two-browser rows 1.3d, 1.3i, 1.3o, 3.3, 3.4b; then the three-browser row 1.3u; then 1.3v and 1.3t (phones with authenticator apps); then 3.1 and 3.4.
2. **Session 2: real devices and microphone.** Run 1.2 across all six browser/OS combinations in one sitting per device.
3. **Session 3: screen readers.** Run 2.1–2.7 with NVDA first, then VoiceOver, then TalkBack. Work through one persona at a time: candidate (2.1, 2.5, 2.5b, 2.5c, 2.5g, 2.5h, 2.6, 2.7), then employer (2.2–2.4, 2.5d, 2.5e, 2.5i), then staff (2.5f, 2.5j, 2.5k), then 2.5l.
4. **Session 4: real email provider** (configure `EMAIL_HOST` once; prepare Gmail, Outlook and a phone mail app). Run 1.3x and 1.3s (accounts), 1.3f and 1.3m (invitation and domain), 1.3g (verification decisions), 1.3, 1.3b, 1.3w (application, message and suspension), then 1.3j with BEAT (the digest needs a Monday morning).
5. **Session 5: ClamAV** (install clamd, set `FILES_SCANNER=ClamAVScanner`). Run 1.3h, then 1.3n.
6. **Session 6: staging stack.** Run 1.4, then 1.3k (BEAT or `day` simulation), then 1.3q (Redis outage last, since it disrupts the stack).
7. **External accounts, when they exist:** 1.1 (JaaS), 1.3r and 3.5 (hosted AI), 1.3l (after gateway adapters are built), 1.3y (after a provider is chosen and the adapter is built).
8. **Infrastructure and people:** 1.5 (backup and alerting), 1.6 (counsel), 3.6 (second rater). These can run in parallel with everything above.

---

## D. Readiness and blocked dependencies

| Category | Rows |
|---|---|
| **Ready now** (DEMO plus ordinary browsers, phones and screen readers) | 1.2, 1.3c, 1.3d, 1.3e, 1.3i, 1.3o, 1.3p, 1.3t, 1.3u, 1.3v, 1.7–1.11, 1.13, 1.14, 2.1–2.7 (all 20), 3.1, 3.3, 3.4, 3.4b |
| **Needs an external account or provider** | Real email provider: 1.3, 1.3b, 1.3f, 1.3g, 1.3j, 1.3m, 1.3s, 1.3w, 1.3x · JaaS: 1.1 · Hosted AI key: 1.3r, 3.5 · Payment gateways (adapters deferred, SRS §21): 1.3l · Email provider with a bounce feed (adapter not built): 1.3y |
| **Needs staging or infrastructure** | ClamAV daemon: 1.3h, 1.3n · Staging stack / Celery Beat / load balancer: 1.3j (Beat), 1.3k, 1.3q, 1.3w, 1.4 · Backup target, restore host, alerting: 1.5 |
| **Needs legal or counsel review** | 1.6 (terms and privacy from `0.1-draft` to 1.0). AI video interview scoring (SRS A16/P20) remains gated behind a legal review and is not part of v1 |
| **Needs another person** | 3.6 (second rater) |

---

## E. What has and has not been verified

| Verified locally by automation (done) | Not verified: manual or external (not done) |
|---|---|
| 3,758 unit/integration tests, including 2 concurrency tests on two real connections | Every row in section B: **zero have been performed** |
| ruff, mypy and migration drift checks | Real email delivery or rendering in any mail client |
| GET sweep (14 personas), hostile POST sweep, link/form sweep, notification-link sweep (re-run 2026-09-21) | Any real payment, JaaS call, hosted AI model, ClamAV scan |
| axe + overflow scans in headless Chromium at desktop, 375 px and 320 px (re-run 2026-09-21 across the whole site) | Any screen reader; any real phone, microphone or camera; Firefox/Safari interaction |
| Voice pipeline with a fake microphone fed from WAV files | Staging, load, Redis outage behind a real load balancer, backups, restores, alerts |
| Local AI benchmark on qwen2.5:7b (CPU) | Counsel review; second-rater labelling |

A row moves to **Passed** only when the evidence log in `Manual-Verification-Consolidated.md` has a dated entry from the tester.
