# Interview Ecosystem — Verification Report (Final Closure Loop)

Updated 2026-09-13. This supersedes the continuation report; earlier loops are summarised in §12.

Manual checks that cannot be done here: [`Interview-Manual-Verification.md`](Interview-Manual-Verification.md).

## Verdict: **NOT READY**

| Blocker | Status | Why |
|---|---|---|
| Real JaaS call (two people, audio/video, auth, expiry, rejoin) | **BLOCKED** | No JaaS account or credentials exist in this environment. Signed tokens, the join flow and authorisation are implemented and tested locally. A real call has not happened. |
| Real browser recording with a real microphone | **NOT VERIFIED** | The in-app browser blocks devices. Playwright Chromium with a *fake* microphone passes 15/15 checks. Playwright Firefox crashes on this machine. Playwright WebKit has no media capture, which is not the same as Safari. |
| Screen-reader journeys | **NOT VERIFIED** | Only Windows Narrator is installed, and it cannot be operated or heard here. axe finds 0 violations, but that is automated checking only. |

Everything else that could be solved locally has been solved and measured. Details follow.

---

## 1. Status legend

| Term | Meaning |
|---|---|
| **Implemented** | Code exists and is wired into the product. |
| **Tested** | Exercised by automated tests or benchmark runs whose results are recorded here. |
| **Not verified** | Could be done in principle, but not in this environment (devices, browsers, assistive technology). |
| **Blocked** | Needs something that does not exist here (credentials, an external account). |

## 2. What this loop changed

| Area | Change | Status |
|---|---|---|
| Job-specific questions | New rule in `services/relevance.py`. Posting terms are matched on stems, and two-word phrases borrowed from *other* postings are rejected. Served pools keep ≥3 questions per competency, specific questions first, so candidates rotate. Kits are judged as stored, and rewritten when mostly generic or when they contain borrowed phrases. | Implemented, tested |
| Kit generation | The prompt receives the posting's statements as a numbered list, and every statement must be covered. | Implemented, tested in §4.3 |
| Embeddings for "equivalent wording" | Built and calibrated. **Not adopted**: no accuracy gain, high cost (§4.2). | Tested, rejected |
| Latency | Ollama gets the output schema natively (no schema text in the prompt). The model stays loaded (`keep_alive` 30 m). Prompts put fixed instructions first so the prefix cache applies. The unused follow-up `reason` field is gone. Output caps: 120 tokens for follow-ups, 500 for evaluation. | Implemented, tested (§5) |
| Evaluation quality | Wrong claims are checked in any field (law, accounting, data formats, science). Vague answers with no action, number or outcome are capped at 1–2. | Implemented, tested (§5) |
| Follow-ups | A follow-up with two questions is cut to its first question. | Implemented, tested |
| Voice | The sealed timing token survives a reload. Whisper's words written into silence are dropped (no-speech probability ≥0.5). | Implemented, tested in browser |
| Benchmark corpus | 41 evaluation cases (was 11) and 16 follow-up cases (was 5), across technical and non-technical roles and intern → lead. `--repeat` gives run-to-run variance. 81 hand-labelled questions for relevance. 10 representative non-demo postings. | Implemented |
| Browser tests | `scripts/voice_e2e.py` (Playwright, fake devices), `scripts/voice_webkit_fallback.py`, `scripts/interview_a11y.py` (axe-core). | Implemented, run |

## 3. Defects found and fixed in this loop

| # | Defect | How found | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| E1 | A reload after transcription dropped the delivery timings; the answer was submitted without them | Chromium E2E run 1 | The sealed token lived only in a hidden field | Kept per question in `localStorage`, restored on load, cleared on submit | `voice_e2e.py` "refresh keeps sealed timings" (pass in run 2) |
| E2 | A silent recording produced a transcript ("You") instead of "No speech was heard" | Chromium E2E run 1 | Whisper writes words into silence (no-speech probability 0.62) | Drop segments with no-speech probability ≥0.5 | `test_voice.SilenceTests` (3); E2E pass |
| E3 | A kit containing questions from another posting was never rewritten | New test failed | Relevance was judged after serving had filtered the borrowed questions out | Judge the stored kit | `RelevanceServingTests.test_a_question_borrowed…` |
| E4 | "project." got past the stop-word list | New test failed | Stop check ran before stripping punctuation | Strip first | `test_relevance.StemTests` |
| E5 | Embedding from a command or Celery task crashed | Calibration run crashed | `runner.embed` emits an event, and events are refused outside a transaction | Wrap in `transaction.atomic()` | `EmbedderTransactionTests` |
| E6 | The anchoring rule was wrong in both directions and blind to cross-posting leaks (0.765 accuracy) | 81 hand labels | Four-letter word overlap | Stem + borrowed-phrase rule (0.877) | `test_relevance` (17), `RelevanceServingTests` (7) |
| E7 | Wrong claims outside software scored 3; vague answers scored 3 | Baseline benchmark | The error check was framed as technical; the rubric allowed confident generalities | Prompt: errors in any field, with examples; vagueness ceiling | Benchmark run B (§5) |
| E8 | Follow-ups sometimes asked two questions | Baseline + run A benchmark | Model ignores "one sentence" | `engine.one_question()` | `OneQuestionTests` (4) |
| E9 | Evaluation calls took 35 s median, p90 61 s | Ollama timing breakdown | CPU prompt processing (~30 tok/s): schema pasted into the prompt, variable text first (no prefix cache), unused output field | See §2 Latency | Benchmark run A/B (§5) |

## 4. Job-specific questions

### 4.1 What counts as job-specific (the rule)

A question is **specific** when:
- it names a term from the posting's title, description or skills, compared on stems so "stores"/"store" and "reconciling"/"reconciliation" match; **and**
- it does **not** use a two-word phrase that appears in another posting while *neither* word appears anywhere in this one. That is how "the nightly close" asked of a freight analyst is caught.

A kit counts as relevant when ≥⅔ of its stored questions are specific and none are borrowed. A communication competency may contain a generic question; the kit as a whole may not be mostly generic.

### 4.2 Rule accuracy — 81 hand-labelled stored questions (63 specific, 16 generic, 2 wrong-job; one labeller)

| Rule | Accuracy | Precision | Recall | F1 |
|---|---|---|---|---|
| Old: shared 4+-letter word | 0.765 | 0.879 | 0.810 | 0.843 |
| Stem match | 0.864 | 0.906 | 0.921 | 0.913 |
| Single-word "borrowed term" check | 0.519 | — | — | 0.581 (flagged 55.6% of all questions) |
| **Stem + wholly-foreign phrase (adopted)** | **0.877** | **0.921** | **0.921** | **0.921** |
| Embeddings only, threshold 0.62 | 0.506 | 0.871 | 0.429 | 0.574 |
| Embeddings only, threshold chosen per held-out fold | 0.728 | 0.781 | 0.905 | 0.838 |
| Stem + embeddings, held out (2-fold by job) | 0.778 | 0.817 | 0.921 | 0.866 |

- Wrong-job questions caught: 2/2. There was 1 debatable flag: "nightly close" asked for Point of Sale.
- Embeddings over 81 questions took 247 s on CPU and added nothing above threshold 0.575. They are not used to decide specificity.
- **Limits:** one labeller, who also wrote the rule; 81 questions; mostly technical demo postings. The labels are in `apps/interviews/relevance_labels.json` for anyone to re-label.

### 4.3 Kit quality — stored kits and representative postings

**10 representative postings written for the benchmark** (not in the database), each generated once on the local model with the new plan prompt and measured as served: nurse, junior accountant, FMCG sales, support team lead, primary teacher, marketing manager, data analyst intern, civil site engineer, restaurant manager, graphic designer. The seniority range is intern → lead.

| Measure | Result | What it means |
|---|---|---|
| Relevance (served questions specific to the posting) | **97.3%** mean; 8 of 10 at 100%; lowest 85.7% (FMCG sales) | The job-specificity rule in §4.1 |
| Specific before any filtering (raw model output) | 93.5% | The prompt change alone, before serving filters |
| Coverage (posting statements addressed by ≥1 question) | **91.8%** mean; lowest 75% (data analyst intern) | Uses embeddings, benchmark only |
| Clarity (8–45 words, ≤2 question marks, no "e.g.") | 99.4% | 1 unclear question in 166 |
| Redundancy (near-duplicate pairs within a competency) | **0** | |
| Borrowed from another posting, served | **0** | |
| Non-behavioural in raw output | 3 of ~170 (filtered before serving) | |
| Variety: expected share of questions two candidates both get | **0.50** mean (0.40–0.64) | Lower means more rotation |
| Smallest served pool | 2 questions (some competencies) | Two candidates would get the same pair there |
| Generation time | median 200.6 s per kit | Off-request, on the `ai` queue; measured while the test suite shared the CPU |

**Stored demo kits (26)** under the new rule:
- **Served questions:** 298 of 411 stored.
- **Naming the posting:** 79.5% of served questions, by the benchmark's own vocabulary; 74.7% of competencies meet the ⅔ rule.
- **Duplicates and level mismatch:** 1 near-duplicate pair; 0 level mismatches.
- **Thin competency:** Operations Analyst / problem solving had 1 servable question, because 2 were correctly refused as borrowed from payments postings ("nightly close", "reconciliation service"). That kit was regenerated: rewritten in 135.6 s; all 11 stored questions specific and none borrowed; served pools 3, 5 and 3 questions; kit judged relevant.

**What this does not show:**
- **Author and ranges:** the representative postings were written by the same person who wrote the rule. Coverage depends on a meaning threshold that was not validated against human judgement.
- **Sample:** one generation per posting, so run-to-run variation in kit quality is not measured.
- **Sameness risk:** an expected overlap of 0.50 means two candidates share about half their questions. That is a real limit on rotation, not a pass.

## 5. AI evaluation and follow-ups — before and after

Same corpus in every run: 41 evaluation cases and 16 follow-up cases, each run twice. The model is local qwen2.5:7b-instruct on CPU (Ryzen 7 5800HS). Bands are the rubric ranges written before any run.

### 5.1 Evaluation

| Metric | Baseline (old prompts) | Run A (latency changes only) | Run B (A + quality fixes) |
|---|---|---|---|
| Band agreement | 70/82 = 85.4% | 76/82 = **92.7%** | 80/82 = **97.6%** |
| Mean distance outside band | 0.159 | 0.073 | 0.024 |
| Ungrounded (a quotation not found) | 8.5% | 3.7% | **0.0%** |
| Rationale asserting withdrawn or invented claims | 0 | 0 | 0 |
| Cases whose pass/fail flipped between runs | 2 | 1 | **0** |
| Largest score change between runs | 2 | 0 | 0 |
| Incorrect claims | 4/10 | 4/10 | **8/10** (parquet_incorrect still 3 in both runs) |
| Ambiguous | 2/6 | 6/6 | 6/6 |
| Long but weak | 5/6 | 6/6 | 6/6 |
| Strong / concise-strong | 19/20 · 8/8 | 20/20 · 8/8 | 20/20 · 8/8 |
| Self-correction / retraction / contradiction | 4/4 · 6/6 · 4/4 | 4/4 · 6/6 · 4/4 | 4/4 · 6/6 · 4/4 |
| Technical / non-technical | 34/40 · 36/42 | 38/40 · 38/42 | 38/40 · 42/42 |
| Intern / entry / mid / senior / lead | 6/6 · 12/14 · 38/42 · 12/16 · 2/4 | 6/6 · 12/14 · 40/42 · 14/16 · 4/4 | 6/6 · 14/14 · 42/42 · 14/16 · 4/4 |
| **Call latency median / p90** | 35.2 s / 60.8 s | **17.3 s / 24.1 s** | 21.0 s / 30.1 s (longer prompt than A) |
| Tokens in / out (median) | 840.5 / 122 | 713 / 79.5 | 862 / 86 |

### 5.2 Follow-ups

| Metric | Baseline | Run A | Run B |
|---|---|---|---|
| Pass rate | 28/32 = 87.5% | 29/32 = 90.6% | 31/32 = **96.9%** |
| Corrections and retractions | 80% | 90% | 90% |
| Asked two questions | 2 | 2 | **0** |
| Flipped between runs | 2 | 1 | 1 (retraction_to_be_clear reused "you designed" once) |
| **Call latency median / p90** | 10.1 s / 25.2 s | 10.35 s / **12.2 s** | **7.6 s / 8.9 s** |
| Tokens in / out (median) | 436.5 / 42.5 | 373.5 / 25 | 373.5 / 26 |

### 5.3 Where the time went (Ollama timings, before)

- **CPU speed:** prompt processing runs at about 30 tokens/s and generation at about 5–6 tokens/s.
- **Cold evaluation:** 871 input tokens, 27.2 s processing the prompt plus 16.1 s generating, 43.3 s in total.
- **Identical repeat:** prompt processing 0.22 s (prefix cache). A cache hit is only possible when the variable text comes last, so the prompts were restructured.
- **Follow-up output:** about half of it was the `reason` field, which nothing reads.
- **Schema text:** passing the schema natively instead of pasting it in cut the follow-up prompt from 448 to 395 tokens.

What was **not** done, and why:
- **Parallel evaluation calls:** CPU-bound and one model instance, so no gain expected.
- **Response caching:** answers are unique.
- **A smaller model:** would trade quality this benchmark cannot yet afford.

## 6. Voice interview

### 6.1 Browser tests actually run

| Engine | Device | Result |
|---|---|---|
| Chromium 149, headless (Playwright) | **Fake** microphone fed from WAV, fake camera | **15/15** after fixes (13/16 before; 2 real defects fixed, 1 test flaw): permission granted → ready; record → stop → transcribe in 9.4 s; refresh keeps draft and timings; edited transcript submitted with timings; camera preview local; silence → "No speech was heard"; noisy audio transcribed; network failure and server 503 → "Record again" + typing; keyboard reaches and operates the recorder with a visible focus ring; 375 px with no overflow; no console errors; no audio temp files left |
| WebKit (Playwright Windows build; **not Safari**) | No media capture in this build | **5/5**: says recording is unavailable on load; offers no record button that would fail; typed answer submits; no console errors |
| Firefox (Playwright) | — | **NOT VERIFIED**: crashes on page creation, headless and headed, on this machine |
| In-app Browser pane (Chromium), earlier loop | Real devices blocked by the pane | Denied path shows "Your browser blocked the microphone" with recovery steps (verified earlier) |
| Chrome/Edge with a real microphone; Safari macOS/iOS; Android | — | **NOT VERIFIED** — manual checklist §B |

### 6.2 Speech recognition (earlier loop, unchanged engine)
- **Accuracy:** word error rate 2.41% on WebM/Opus and 4.83% on WAV, over 10 synthetic clips (290 words).
- **Long pauses:** 3/3 detected.
- **Speed:** about 1.2 s per answer when the model is already loaded.
- **Caveat:** synthetic voices only.

## 7. Accessibility

| Check | Result | Status |
|---|---|---|
| axe-core (Playwright Chromium): hub, text question, voice question, result, booked result, join, book a time, employer list, employer detail — light and dark | **0 violations** on 18 scans; 36–40 rules passing per page | Tested (automated) |
| Contrast (earlier loop, computed OKLCH) | 0 failures | Tested (automated) |
| Keyboard operation of the recorder, visible focus | Pass | Tested (browser) |
| Screen reader: NVDA, VoiceOver, TalkBack | Not performed | **NOT VERIFIED** — checklist §C |

## 8. Real JaaS verification — **BLOCKED**

- **Credentials checked:** `.env` and the process environment have no `JAAS_APP_ID`, `JAAS_API_KEY_ID` or private key, and `VIDEO_ROOM_PROVIDER` is unset. No JaaS account exists.
- **Implemented and tested locally** (earlier loop; unchanged here):
  - RS256 join tokens with the correct `kid`, `aud`, `iss`, `sub`, exact room and join-window expiry;
  - moderator only for interviewers;
  - recording and livestreaming disabled;
  - no key means no link;
  - non-panel members refused;
  - deploy checks `interviews.E001`/`E002` verified with real `check --deploy`.
- **Not proven:** generated tokens and passing tests do not show that a real call works.
- **To unblock:** manual checklist §A (environment variables, JaaS console, 12-step two-participant test).

## 9. Tests and checks

| Check | Result |
|---|---|
| Full project suite (final code) | **3034 tests, OK** (1149 s) |
| Interview-related modules at start of loop | 759 OK (report claims verified) |
| Interview app test functions | **382** (354 before this loop) |
| New this loop | `test_relevance` 18, `RelevanceServingTests` 7, `OneQuestionTests` 4, `SilenceTests` 3, `EmbedderTransactionTests` 1 |
| mypy | clean, 357 files |
| Migrations | no drift |
| Ruff | clean on all changed files; 6 pre-existing warnings in `seed_demo.py` (untouched) |
| Format | 138 files formatted |
| Browser: voice E2E Chromium (fake devices) | 15/15 |
| Browser: WebKit unsupported-browser fallback | 5/5 |
| Browser: axe-core, 9 pages × 2 schemes | 0 violations |
| Live AI benchmark | 3 runs × (41 eval + 16 follow-up) × 2 repeats = 342 model-scored attempts |
| Relevance rule | 81 hand labels, 2-fold held-out by job |

## 10. Scores

| Category | Weight | Score | Evidence |
|---|---|---|---|
| Core functionality | 15% | 8.5 | Text, voice and live flows work end to end; voice verified in a real browser engine with fake devices; notifications delivered |
| AI question quality | 10% | 8.0 | Representative postings (10 industries): 97.3% relevant, 91.8% coverage, 0 duplicates, 0 borrowed; old demo kits 79.5% by benchmark vocabulary; rotation limited (overlap 0.50, some pools of 2); one labeller |
| AI adaptiveness | 10% | 8.5 | Follow-ups 31/32 on 16 cases × 2; 0 two-question follow-ups; corrections 90%; p90 8.9 s locally |
| Evaluation / grounding | 10% | 8.5 | Band agreement 97.6% on 41 cases × 2, 0 flips, 0 ungrounded; still misses one technical error (Parquet); one labeller wrote the bands; local model only |
| Candidate UX | 10% | 8.5 | Every voice failure state verified in a browser with a way forward; follow-up p90 now 12 s locally |
| Video/speech interview | 10% | 7.0 | Chromium (fake devices) 15/15, unsupported-browser fallback 5/5; Firefox, Safari, mobile and real microphones not verified |
| Scheduled interviews | 10% | 8.5 | Lifecycle, concurrency and delivery tested (unchanged) |
| Real-time reliability | 8% | 3.5 | JaaS blocked; only token and authorisation logic tested |
| Security / privacy | 7% | 8.5 | Signed tokens, panel-only rooms, audio deletion, 23/23 probes (unchanged) |
| Accessibility | 5% | 8.0 | axe 0 violations on 18 scans, contrast clean, keyboard verified; no screen reader |
| Performance | 3% | 7.5 | Evaluation calls halved (35 → 17 s median); still seconds per call on CPU |
| Integration | 2% | 8.5 | Unchanged |

**Weighted total: 7.85 / 10.**

## 11. Remaining issues

**Blocked**
- Real JaaS call.

**Not verified**
- Real microphone and camera.
- Firefox, Safari and mobile browsers.
- Screen reader.
- Hosted-model quality and latency.

**Known limitations**
- **Wrong technical claims:** the evaluator still misses one ("Parquet is row-oriented"), scoring 3 when 1–2 is expected, in both run-B attempts. The cap only works when the model spots the error.
- **Benchmark provenance:** one person wrote the benchmark ranges, the relevance labels and the representative postings. No second rater.
- **Model and hardware:** every AI figure comes from local qwen2.5:7b on CPU. On this machine an evaluation takes 21 s median (p90 30 s) per competency, a follow-up 7.6 s (p90 8.9 s), and generating a kit about 200 s.
- **Question variety:** two candidates share about half their questions on average, and some competencies serve only 2 questions.
- **Speech:** the recognition corpus is synthetic speech; filler words are undercounted.
- **Firefox:** Playwright Firefox crashes on this machine, so Firefox remains unverified by any means.
- **Carried from the CV loop:** non-numeric retraction grounding and a duplicate-role splitting edge case.

## 12. Earlier loops (summary)

- **First pass:**
  - 23 defects (employer join 404, live interviews stuck, fabricated quotations, …).
  - Score 7.15 excluding video, 6.44 counting video as 0.
- **Continuation:**
  - Voice interview built; JaaS signed tokens; notification delivery fixed; 12 defects.
  - Full suite 3010 OK; score 7.56; NOT READY.
