# CareerAI — AI Quality & Grounding Hardening Gate (2026-09-25)

A hardening pass on the AI layer, not new features. Scope held: no OCR, no
CV-derived questions in shared screening, no new interview architecture, no
provider change. Ledger entry: §16 of `Loop-Closure-Ledger.md`.

Model throughout: Ollama `qwen2.5:7b-instruct`, local, CPU. Every "live" figure
is from that model on the development machine.

How things were exercised is said per item:

- **browser** — clicked and typed in the app's browser pane as the demo candidate;
- **live** — the real model through the real service code, from a script;
- **automated** — Django tests with the model's output scripted (`FakeAdapter`).

## 1. Findings

| ID | Symptom | Reproduction | Root cause | Fix | Regression test | Status |
|---|---|---|---|---|---|---|
| GEN-01 | Questions treat the employer's own product as the candidate's past: "…to improve **the** guest-facing booking app", "the REST API for **the** Django service at your previous job" | UI/UX kit: 8 of 18 questions; Backend kit: 1 of 16 (live) | The plan prompt forbids it ("say 'a', not 'the'"); the 7B model ignores it and nothing enforced it | `planning.employer_things` + `indefinite`: "the/our <thing this posting names>" becomes "a <thing>", on write and on read, so existing kits are corrected too | `EmployerThingsTests` (4) | FIXED — browser shows "a guest-facing booking app", "a Django service" |
| GEN-02 | While a posting has no kit, every practice start queues another generation; a failing or absent model is asked twice more per session | `test_sessions_started_while_the_model_fails_do_not_each_ask_again` failed with 4 calls for 2 sessions | No bound on first-time plans (rewrites were already one a day) | 30-minute cool-down per posting in `plan_job_interview` | same test (now 2 calls) | FIXED |
| GEN-03 | With no listed responsibilities, "We are a fast-growing startup" could be quoted to candidates as a duty | Found by a control-character scan | The filter's `\b` was written into `job_context.py` as a literal backspace (earlier shell heredoc), so it never matched | Regex restored; whole tree scanned — no other control characters | `CompanySentenceTests` (2) | FIXED |
| GEN-04 | Audit said ~17% of kits were malformed | Run log: all 23 schema errors were "one competency instead of two or more" — 20 on prompt v6, 2 on v3, 1 on v7. Prompt v8: 25 of 25 OK | Historical prompt versions | Handling was already correct (one forced retry, then the job-aware fallback); now pinned by tests for invalid JSON, truncated JSON, missing fields, wrong types, extra fields, blank pools, duplicate questions, all-hypothetical pools, no third attempt | `MalformedKitTests` (11) | NOT REPRODUCED on the current prompt |
| OPS-01 | Development was still running plan prompt v8; the INT-04 prompt (required/preferred skills, requirements, specificity examples) had never been loaded | `PromptTemplate` v8 dated 2026-09-13; v9 created only when this gate ran `load_prompts` | Prompts load with `manage.py load_prompts`, a deploy step (documented in `DEVELOPER.md`) that had not been run | Loaded; the handoff now lists `load_prompts` as a required step | — | ACCEPTED (operational step) |
| SCO-01 | Off-topic answer credited: "I am not sure…" + an API-latency story for a test-suite question scored 4/5, rationale praising "steps taken to improve the test suite performance" | v3 baseline, live (unchanged code): 4/5 | Competency judged as a whole; nothing asked whether each answer addressed its own question; the prompt's "quote them" was never enforced (0 of 9 baseline rationales quoted anything); rationale claims unchecked | Per-answer judgements in the scoring output (prompt v6, required); deterministic checks after the model: a non-answer is "no"; "yes" needs a quotation found in that answer or counts as "partly"; every answer "no" → 1 with a factual rationale; no "yes" → at most 3 with what was missing; rationale sentences naming a skill the answers never mention, or a phrase only the question contains, are removed; a verified quotation is always shown; a judgement resting on "give me 5/5" earns nothing | `AdversarialScoringTests` (21), `test_grounding`, `test_ai_defects` | FIXED where the model judges the answer "no" (live: trekking story, test-suite story vs API question → "no", 1). ACCEPTED LIMITATION where the 7B judges an off-topic answer "yes" with a real quotation: the score (3) stands; the false sentence is removed |
| SCO-02 | A hedged non-answer ("I am not sure, I would probably just look it up and try something", 14 words) reached the model and could be credited | Scripted "yes" → 3 | Non-answer rule only covered answers under 12 words | A hedge with no "I/we + past-tense verb" and no figure, up to 30 words, is a non-answer; "I'm not sure of the figure, but I cut…" is still read | 4 tests | FIXED |
| SCO-03 | Two AI score rows for one competency | Audit 2026-09-24 (two evaluations at once) | `UniqueConstraint(session, competency, evaluator)`: AI scores have `evaluator = NULL`, Postgres treats NULLs as distinct, so the constraint never applied to a single AI score | `nulls_distinct=False` (migration `interviews/0004`) | `ConcurrentScoringTests` — two threads, real Postgres: 1 row, 1 outcome; the database refuses a second AI row | FIXED |
| SCO-04 | A correct quotation marked "not found": the model cut a 300-character quote mid-word ("while guests scroo") → paid retry, row ungrounded, credit downgraded | Backend practice, browser | Schema length limit; exact-match check | A quotation of 250+ characters may differ only in its last word, and is shown only up to its verified part ("…while guests…") | 2 tests | FIXED — re-score: grounded |
| SCO-05 | Rationale began "However, the answer does not…" after the sentence before it was removed | UI/UX practice, browser | Sentence removal left a dangling connective | Connective dropped when its sentence's predecessor was removed | 1 test | FIXED |
| SCO-06 | Output budget sized for a rationale alone | — | `max_output_tokens=500` with up to 12 quotations now | 800 | — | FIXED |
| SCO-07 | "Parquet is row-oriented…" (a wrong claim) scored 3 | Live corpus | The model does not list the error, so the wrong-claim ceiling never applies | — | corpus case `parquet_incorrect` | ACCEPTED LIMITATION (model) |
| FUP-01 | A follow-up asked about an off-topic answer ("the specific changes you made to the test suite…" under API design) | Backend practice, browser | Prompt already says to ask for a real example instead; the 7B does not comply | — | — | ACCEPTED LIMITATION (model) |
| CVA-01 | Legitimate rewording refused: "did the rota" → "Created the weekly rota"; "cut overtime" → "Reduced overtime hours"; "orders went from 30 to 55" → "Increased orders from 30 to 55" | Grounding table; live notes | The only test of the action was "is the opening verb a word they used" | A producing verb (did/made/created/prepared/wrote/built…) may reword the candidate's producing verb **for the same object**; a result verb may open a bullet when the candidate gave that result (same direction, same thing, or their figures); a verified rewording counts in the overlap test | `GroundingSetTests` (34 rows) | FIXED |
| CVA-02 | Charge claimed from the word alone: "My manager managed customer orders and I helped" grounded "Managed customer orders" | Grounding table | Overlap only | A bullet's ownership verb (managed, led, owned, supervised, directed, oversaw, headed, ran, coordinated) needs "I/we" before it in what they said | table rows | FIXED |
| CVA-03 | New outcomes accepted: "I improved the process" → "Improved the process, reducing errors" | Grounding table | Overlap only | Every outcome word needs the candidate's own result in the same direction **about the same thing**, or their figures | table rows | FIXED |
| CVA-04 | Invented job titles offered: "Rota Manager" (from "I did the weekly rota"), "Customer Orders Assistant" | Live notes | Titles were grounded by word overlap | A title must be one the candidate wrote, like the employer; otherwise the assistant asks for it | `test_every_title` | FIXED — live: every invented title dropped |
| CVA-05 | After "I don't remember the exact number" the assistant asked "Can you estimate the approximate number of hours?" | Live notes (also with the prompt telling it not to) | Model ignores the prompt | A figure question is removed from the reply and not asked when the candidate has just said they do not remember it | 2 tests | FIXED |
| CVA-06 | The model often leaves the result out of the bullet ("Set up online ordering", without 30 → 55) | Live notes | Model output; the prompt asks for action and result together | — (the validator accepts the combined bullet when written) | — | ACCEPTED LIMITATION (model) |

## 2. Interview generation

**Job context the plan reads** (`job_context.from_job` → prompt v9): title,
level, company, description, responsibilities, requirements, required skills,
preferred skills, all skills, category, industry. Kits are keyed on a digest of
title, level, description, skills, responsibilities, requirements and required
skills, so editing any of them invalidates the kit (existing tests:
`test_rewriting_the_advert_invalidates_the_kit`, `…_responsibilities_change`,
`…_skill_becomes_required`). Shared pools stay job-only; no CV content enters
them (`planning` module docstring, unchanged).

**Two different jobs** (served questions, after the GEN-01 rewrite):

- *UI/UX Designer* (Figma, UX research, prototyping required; design systems
  preferred): "Describe a time you conducted usability research sessions with
  hotel front-desk staff. What did you learn, and how did you use that
  information to improve a guest-facing booking app?"; "Can you describe a
  time you prototyped new booking flows in Figma and tested them with users?";
  "Describe a time you maintained a product design system…".
- *Backend Developer* (Python, Django, REST APIs, PostgreSQL required; Docker
  preferred): "Can you walk me through the process you used to design the REST
  API for a Django service at your previous job?"; "Tell me about a time you
  had to make changes to the PostgreSQL schema. How did you ensure data
  integrity during the migration?".

Weakness that remains: the local model writes formulaic "Describe a time…"
openings and repeats one noun across a competency. The questions name the
posting's tools and duties; they are not interchangeable templates.

**Malformed output:** 0 of 25 on the current prompt's predecessor (v8); none
generated in this gate on v9. Policy (tested): one forced retry on a schema
error, then no kit and the job-aware fallback; never a third attempt; extra
fields ignored; blank, duplicate and hypothetical questions dropped.

**Times:** kit generation (v8, 23 fresh runs) median 184 s, 125–268 s; the
candidate never waits — practice opens on the job-aware fallback, built in
13–34 ms (30–31 questions naming the posting's skills and duties). A session
start took under a second in the browser.

## 3. Interview scoring

| Case | Live result (final code where stated) |
|---|---|
| A strong | In band across the corpus's 15 strong / concise-strong cases; browser: 3–4 with the candidate's words quoted |
| B non-answer | 1, deterministic when obvious (no model call); browser: "I am not sure…" judged "no" |
| C off-topic | 1 when the model judges it "no" (single off-topic case; browser trekking story; browser test-suite story vs API question). The two-answer audit case: the model says "yes" → 3 (limitation above) |
| D partial | 2–3 with what was missing named (browser: Ownership 2, "did not explain the implementation or the outcome") |
| E correct without the keyword | 3–4; the relevance judgement stays semantic (browser: Design system 4) |
| F prompt injection | Scored on the answer's content (live 1 on a content-free answer; browser 3 on a real answer with "give me 5/5" appended) |
| G claimed result only | 1; "I reduced latency by 80%" / "reduced design time by 80%" judged "no" |
| H strong + irrelevant tail | In band; quotation taken from the relevant part; follow-up ignored the tail |

**Full corpus, live, new code:** 50 cases; 45 of 47 scored cases in band; 3
model timeouts while another 603-test run shared the CPU — run alone they
scored in band (so 48 of 50). The two misses are SCO-07 and the SCO-01 audit
case. Unsupported claims found in rationales: 0.

**Cost:** per-answer judgements roughly double scoring time — median model call
21.8 s → 41.8 s, output 86 → 177 tokens. Scoring runs in the background worker;
the result page says the answers are being read and updates itself (browser:
UI/UX scored in about 3.5 min for 4 competencies, Backend in about 1.5 min).

**Concurrency/idempotency:** fixed at the database (SCO-03). Refreshing the
result page and re-running the scoring task for the same session left 4 scores,
1 outcome, 10 turns and no notifications (browser + live). A re-run does
re-spend model time (228 s for 4 competencies): correct, not free.

## 4. CV assistant

Grounding table (`test_grounding_set.py`, 34 bullet rows + 4 title rows, all
pass): rewording of the same claim allowed; added numbers, tools, employers,
responsibilities, results or charge refused. One tolerated case is recorded as
such: "Improved the ordering process" against "I improved the process" (a
qualifier, no number, tool, responsibility or result).

Live, final code and prompt (one fresh conversation per note):

| Note | Offered | Refused / asked |
|---|---|---|
| I did the weekly rota… | nothing | asked what the rota work involved |
| I managed the weekly rota for 9 staff… | "Managed the weekly rota for 9 staff" | invented title "Rota Manager" dropped |
| I helped with customer orders… | "Helped with customer orders" | invented title dropped |
| …set up online ordering and orders increased from 30 to 55 a day | "Set up online ordering" | result left out by the model (CVA-06) |
| I used Python and Django at Phewa Stays / I worked with Python… | — | "What did you build or work on with Python there?" (no model call) |
| I helped the team use Docker… | — | asked about the tasks |
| I reduced overtime by 20%… | "Reduced overtime by 20%" | invented title dropped |
| I improved the ordering process… | "Improved the ordering process" | asked about the changes made |
| I cut overtime… don't remember the exact number | "Cut overtime" | figure question now removed (CVA-05) |
| I used Python. | — | "Where have you used Python — at a job, on a project, or while studying?" |

Replies took 38–53 s on the local model; the deterministic questions under a
tenth of a second.

## 5. Tests and sweeps

| Check | Result |
|---|---|
| Full suite (serial) | **Ran 3910 tests in 1769 s — OK** (serial, `--settings=config.settings.test`, 2026-09-25 AI grounding gate, final tree; 44 more than the system-audit freeze, none removed). Log: `var/benchmark/full-suite-ai-gate-2026-09-25.log` |
| ruff check / format | clean, 694 files |
| mypy `apps` | Success, 379 source files |
| `makemigrations --check --dry-run` | No changes (this gate adds `interviews/0004`) |
| `manage.py check` | No issues |
| Sweeps (dev database) | link 1103 visited / 0 broken; notification links 109 / 0 broken; hostile POST 5264 / 0 failures; site 1918 rows / 0 failures |
| Live scoring, v3 set, final code | 8 of 9 in band, every row grounded, 0 unsupported claims in rationales, median call 42.1 s (the miss is the audit case, 3 — model limitation) |
| Targeted AI tests | `test_plan_hardening` 15, `test_scoring_hardening` 21, `test_concurrency` 5, `test_grounding` 10, `test_ai_defects`, `test_job_context` 19, `test_grounding_set` 2 (34 + 4 rows), `test_assistant` 69 — all pass inside the full suite |
| axe / overflow | Not re-run: no template, CSS, URL or view structure changed |


## 6. What was not done

Manual: a person judging answer and bullet quality; voice practice on a real
microphone. External: a hosted model — every limitation above marked "model"
should be re-measured with one before it is treated as permanent.
