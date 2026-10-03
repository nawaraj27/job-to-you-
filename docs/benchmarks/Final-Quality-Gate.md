# CareerAI — Final Product Quality Gate (2026-09-24)

Scope was frozen: no features, only defects. This page records what was
exercised, how, what it produced and how long it took, and the defects the gate
fixed. The ledger entry is §14 of `Loop-Closure-Ledger.md`.

How each row was tested is stated in its own column:

- **browser** — clicked through in the app's browser pane as the demo persona;
- **automated** — Django test client or a sweep script (not a person in a browser);
- **untested** — needs a person (see *Manual tests still required*).

Machine: Windows 11, local Postgres/Redis in Docker, Celery workers (default,
files, render, email, ai), Ollama `qwen2.5:7b-instruct` on the same machine.
Times are wall clock on that machine, from the app's own responses or the
job records.

## 1. Feature walkthrough — inputs, results, times

### CV upload (browser, demo candidate)

| Step | Input | Result | Time |
|---|---|---|---|
| Upload | `maya-walkthrough-cv.pdf` (synthetic, one page, made for the test) | Scanned, parsed; 2 roles, 6 skills, 1 education found | 2.37 s upload to parsed (parse itself 203 ms) |
| Progress chip | "Continue browsing jobs" while it ran | Chip followed to the jobs page and turned into "View results" | — |
| Refresh | Reload the results | One list, no duplicate proposals | — |
| Proposals | — | Offers only; nothing written to the profile until accepted | — |
| Edit as a CV | "Edit as a CV" | A document was made from the upload | — |

Observed, not a defect: the parser did not pick "Pokhara, Nepal" out of the
contact line, so the assistant later asked for a location.

### Analysis against a job (browser)

| Target | Result | Time |
|---|---|---|
| Backend Developer | 84/100, 80% of the advert covered; readiness **Complete** (Docker shown as preferred advice only); ATS **Passing** | 124 ms report; background AI reviews 50.3 s, 74.1 s, 102.9 s |
| UI/UX Designer | Gaps: Figma, Prototyping, UX research (required), Design systems (preferred) | 789 ms |

### Guided CV builder (browser)

| Input | Result |
|---|---|
| Opened the guided builder on the uploaded CV | "5 of 6 done"; same readiness as the analysis report |
| Summary: appended "Looking for a backend role in Pokhara or remote." | Saved; still there after a reload |

### CV assistant — "prompt-making" the CV (browser, UI/UX gaps)

| Input typed | Answer time | What the assistant did |
|---|---|---|
| "Yes. I used Figma at Phewa Stays and designed a new booking form that reduced abandoned bookings by 15%." | 123.5 s (first model call, cold Ollama) | Suggested the bullet "Designed a new booking form in Figma"; asked about Prototyping next |
| "No, I haven't done prototyping." | 299 ms | Recorded as declined; asked about UX research |
| "No." | 231 ms | Declined; asked about Design systems |
| "skip" | 218 ms (after SKIP-01 was fixed) | Skipped; "That is every skill…"; moved on to the location question |

Figma reached the CV only after **Use this** was clicked; the existing bullets
were kept. Quality note: the suggested bullet dropped the candidate's "15%"
result — nothing was invented, but a useful number was lost.

### Interview practice (browser, Marketing Specialist, AI kit tailored to the job)

| Question | Answer given | Result |
|---|---|---|
| Q1: analysing a campaign's data | Facebook off-season campaign measured with Google Analytics and a discount code; 42 bookings for about 18,000 rupees against Instagram's 6 | Accepted |
| Q2: a decision made from data | 60% of bookings were on mobile at half the conversion; simplified the form; 1.2% → 2.1% | Accepted |
| Q3: AI follow-up on the booking-report analysis | (finished here) | Follow-up generated in 25.2 s |
| Finish | 2 answers | Evaluation in 57.5 s: Data analysis 3/5 with reasons. Title now reads "Marketing Specialist" (MIS-02) |

Earlier `interview.plan` runs on this machine took 157–219 s; the local model
is the bottleneck, not the app.

### Employer and staff (browser)

| Screen | Result |
|---|---|
| Employer overview | Summit Digital Labs, Verified |
| Applicants | Maya's application to Backend Developer |
| Interviews | Found a past interview still "Scheduled" (DEMO-06); after the fix it reads Sunday 27 September, 10:00 · Scheduled, and the past unbooked time no longer shows as "Open" |
| Candidate interviews | Coming up: Backend Developer, 27 Sep 2026, 10:00; one "Interview moved" notice each side (after the NOTIF-07 fix) |
| Staff verification queue | 3 waiting; Gandaki Traders shows one certificate and the missing PAN/VAT line |

The five demo passwords were checked with `authenticate()`, not typed into the
browser; sessions were started from the server.

## 2. Defects found and fixed in this gate

| ID | Defect | Fix | Test |
|---|---|---|---|
| MIS-01 | After any edit, readiness said "ATS: Incomplete — fix what the checks point at" although no check had failed; the analysis was just out of date | `Requirement.unchecked`; the line reads "Not checked since your last edit — run the analysis again, nothing has failed" | `UncheckedAtsTests` |
| SKIP-01 | "skip" at a target-gap question went to the model (up to two minutes) instead of moving on | Stop words at a pending gap question decline it directly | `SkipGapTests` |
| MIS-02 | A practice for a job was titled "General practice interview" on the result and the hub | The job title is shown when the practice was for a job | `PracticeTitleTests` |
| DEMO-06 | The seeded live interview was booked once and never moved; three days later the demo showed a "Scheduled" interview in the past | Re-seeding moves a past (or under-a-day) booking through the real reschedule path | `test_a_live_interview_that_has_passed_is_moved_to_an_upcoming_day` |
| CAL-01 | The employer's published times listed past unbooked times as "Open" for ever, oldest first, behind a 40-row cap that eventually hid upcoming times | Only times that have not ended are listed | `test_a_time_that_has_passed_leaves_the_published_list` |
| NOTIF-07 | Rescheduling told both sides twice: "Interview booked" and "Interview moved" for one change | `book(announce=False)` from `reschedule` | `test_a_move_reaches_both_sides` (now also asserts no second "booked") |
| MIS-03 | The candidate's "Interviews you have done" list included booked and moved interviews | Heading now "Interviews for your applications" (each row already shows its state) | wording only |

Company follow was audited, not extended: posting needs `company.edit`,
withdrawing is limited to your own company, only followers who followed before
the update are told, blocked companies are excluded, and one update gives one
notice per follower. A company verified again after its first notice was read
gives a new notice — intended.

## 3. Demo data after the gate

Walkthrough upload deleted through the upload service; its document archived;
the two duplicate "Interview booked" notices from the DEMO-06 move removed. The
finished Marketing practice is kept so the tester sees a completed result.
`seed_demo --personas-only` run twice afterwards: nothing changed on the second
run; no duplicate applications, follows or notifications.

## 4. Final checks

| Check | Result |
|---|---|
| Full suite (serial, `config.settings.test`) | **Ran 3863 tests in 1376 s — OK** (serial, `--settings=config.settings.test`, 2026-09-24 quality gate, final tree; 6 more than the earlier 2026-09-24 freeze, none removed). Log: `var/benchmark/full-suite-quality-gate-2026-09-24.log` |
| ruff check / format | clean, 689 files |
| mypy `apps` | Success, 379 source files |
| `makemigrations --check --dry-run` | No changes |
| `manage.py check` | No issues |
| Sweeps (dev database) | link 1105 visited / 0 broken; notification links 107 / 0 broken; hostile POST 5264 / 0 failures; site 1918 rows / 0 failures |
| axe + overflow | loop screens 40, employer/staff 36, whole site 72, CV builders/ops 16, CV templates/verification 28 — 192 scans, 0 serious/critical, 0 overflow; UI polish audit 40 renders, 0 stray decorations / clipping / sideways scroll |

## 5. Manual tests still required

Nothing below was done by a person; the browser pane is not a substitute.

- Signing in by typing each demo password (the gate started sessions from the server).
- Uploading a real CV of your own (DOCX and a scanned PDF) and judging the parse.
- The CV assistant and interview practice for answer *quality*: the model is
  local and slow (first call about two minutes cold, then 25–60 s per AI step).
- Speaking answers aloud and on camera; any real phone, microphone or camera.
- A live interview end to end with two people (join window, Jitsi room).
- Email delivery to a real inbox (the dev mail backend was used).
- Screen readers, Firefox and Safari; keyboard-only use beyond axe's checks.
- Posting a company update as the employer and reading it as the follower.

## 6. External blockers

Unchanged by this gate; the handoff lists the rows each one blocks.

- Real email provider and inboxes (the dev mail backend was used).
- Payment gateways: adapters deferred by SRS §21; plans are granted by manual payment.
- 8x8 JaaS account for hosted video interviews.
- Hosted AI key: only local Ollama was used; the timings above are for it.
