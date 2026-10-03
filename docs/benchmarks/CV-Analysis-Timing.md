# CV analysis: stage timings (2026-10-01, end to end 2026-10-02)

Measured on the development machine, through the real code path in one process
(`scratchpad/profile_analysis.py`). Every stage ran against the dev PostgreSQL
and the local model, inside a transaction that was rolled back, and stored
files were removed afterwards.

**Machine:**
- Windows 11 laptop, CPU only.
- Ollama `qwen2.5:7b-instruct` Q4_K_M. `/api/ps` reports `size_vram: 0`.
- Scanner `AllowAllScanner` (dev).

## Stages

| | Stage | Where |
|---|---|---|
| A | Validate the upload (type, structure, text, CV gate) | `services.validation.check`, on the request |
| B | Store the file, create the upload row | `services.upload` |
| C | Virus scan | `files.services.scan_attachment` (Celery `files`) |
| D | Parse (text → structured CV) | `services.parse.parse_upload` (Celery `default`) |
| E | Create the editable document | `services.documents.create_from_upload` |
| F | Deterministic analysis (score, ATS checks, findings) | `services.analysis.analyse_version` |
| G | Render the report | `resume/_analysis_report.html` |
| H | **LLM review** (verdict, judgement findings) | `services.analysis.enrich` (Celery `ai`) |

## Results, milliseconds

Before means before this change. After means with it. A to G are non-LLM
stages and H is the LLM. The "before" figures for D re-read and re-extract the
file, which is what the parse used to do.

| Case | A validate | D parse before → after | E | F | G | H LLM before | H LLM after |
|---|---|---|---|---|---|---|---|
| Small PDF (1 page) | 208–242 | 668–773 → 34–35 | 50 | 95 | 247–291 (cold) | 102 s, ok (658/359 tokens) | 94 s, ok (658/258) |
| Small DOCX | 85–115 | 100–127 → 35–49 | 45–73 | 57–63 | 88–104 | cache hit, 0.05 s | cache hit |
| Small DOC (Word 97-2003) | 22–24 | 44–50 → 42–46 | 45–54 | 54 | 85–97 | cache hit | cache hit |
| Medium PDF (corpus "standard-professional") | 29–43 | 83–123 → 37–47 | 45 | 62–65 | 79–109 | **122 s, timed out** | 82–92 s, ok (674/448–571) |
| Complex two-column PDF | 10–29 | 75–96 → 19–46 | 45 | 55–60 | 90–97 | 85 s, ok | 46 s, ok (592/247) |
| Large, 4-page PDF | 76–235 | 316–383 → 20–52 | 47–51 | 68–76 | 75–83 | **122 s, timed out** | **fails**: `schema_error` at the output cap after 217–268 s |
| Large, 4-page DOCX | 126–391 | 258–386 → 21–59 | 51 | 68–75 | 87–89 | **122 s, timed out** | 94 s, ok (2,366/539) |
| Scanned PDF (no text layer) | 3–11 | n/a | n/a | n/a | n/a | was accepted, parsed to an empty CV | **refused at upload** with the reason |

"Cache hit" means `ai.runner` returned the stored answer for identical input
(the same CV text as the small PDF), which is intended.

## Bottleneck

**H, the LLM review: 46–102 s against under 1.2 s for A–G combined.** On this
machine the model runs on the CPU at about 3.5–5 output tokens a second, plus
the prompt read (2,366 input tokens for a four-page CV).

A–G were never the user-visible problem. F and G are the "instant" half of the
report and stay under 0.3 s. The largest non-LLM cost was D reading the file a
second time.

## What changed, and why

1. **The parse reuses the validation's text and page count (D).**
   - Validation already extracts the text to decide the file is a CV. The parse
     extracted it again and ran pdfminer a third time for the page count.
   - The text now passes to the parse through the cache (15 minutes, keyed by
     content hash and engine version). The page count is stored on the upload.
   - Result: 316–773 ms became 20–59 ms. If the cache entry is gone, the parse
     reads the file as before.
   - Tests: `ParseReuseTests`.
2. **Ollama is given a context window (`num_ctx`, default 8192, env
   `OLLAMA_NUM_CTX`).**
   - Ollama's default is 4,096 tokens, and it truncates longer prompts from the
     front without an error. A four-page CV with its instructions and answer
     goes past that, so the model was reviewing part of the CV.
   - `/api/ps` showed `context_length: 4096` while `config/ai.py` claimed
     32,000.
   - One fixed value, because changing `num_ctx` reloads the model.
3. **Review timeout 120 s → 300 s; output cap 3,000 → 1,200 tokens.**
   - The review always runs in the background, and the deterministic report is
     already on screen, so a longer timeout costs the user nothing. It turned
     every medium and large CV from "timed out" into a finished review.
   - Measured answers are 250–570 tokens, so the cap bounds a runaway
     generation instead of letting it run to the timeout.
   - The report keeps polling for 6 minutes (it was 3) to match.
4. **The prompt states the six-finding limit the code already applied
   (`MAX_AI_FINDINGS`) and asks for short quotes.**
5. **Scans are refused at upload instead of being analysed as an empty CV**
   (workstream 1).

## Tried and reverted

Bounding the output schema (`maxItems`, `maxLength`) made things worse.
Ollama's grammar-constrained decoding filled strings up to the limit, and the
medium CV failed at 1,200 tokens. With the bounds removed it succeeded again
(571 tokens).

## Not fixed

- **Large repetitive CVs can still loop.** The synthetic four-page PDF, with 24
  near-identical roles, makes `qwen2.5:7b` loop inside a quoted string in each
  attempt measured here. The same text as DOCX succeeded once, so the failure
  depends on the sample.
  - The deterministic report is complete either way.
  - The review fails cleanly: it shows "the AI review did not run", and
    "Try the review again" costs no allowance.
  - Real fixes are outside the code: a GPU, a larger or hosted model
    (`OPENROUTER_API_KEY`, already first in the chain), or a smaller input.
- **CPU-only inference** is the floor for every review on this machine,
  roughly 45–100 s per CV.

## Truthful progress

These are stored states, not timers, so they survive a reload, another tab and
a new session:
- Upload screen: "checking the file is safe", then "checked, waiting to be
  read" (queued), then "reading it" (a worker has it), then results, or a
  failure with its reason.
- Report: the score and checks first; "The written review is being prepared now; it usually takes one to two minutes" while
  the review is pending, for at most 6 minutes; then the review, or the reason
  it did not run.

No percentages are shown.

## End to end, with the real queue and worker (2026-10-02)

The figures above were measured in one process. These were measured through
the whole system: Chromium pressing the buttons, the dev web server, Redis,
the Celery worker (`--pool=solo`), the outbox worker and Ollama on the CPU.
Script: `measure_pipeline.py` (session scratchpad), a fresh test candidate per
file, removed afterwards through the account erasure pipeline. Times come from
the browser, the database rows and the worker log (task received, run time),
so queue wait and work are separate. Worker log kept as
`var/benchmark/kaarya/celery-worker-pf-run.log`.

Milliseconds from pressing **Upload** (rows 1–7) or **Analyse this CV**
(rows 8–10):

| Stage | Clean DOCX | Two-column PDF | Skills DOCX, after the fixes |
|---|---|---|---|
| 1. Upload accepted, row stored | 117 | 211 | 112 |
| 2. Scan task received by the worker (queue wait) | 214 | 286 | 195 |
| 3. Scan done (work: 31 ms) | 231 | 304 | 222 |
| 4. Parse task received (outbox hop, see below) | 1,852 | 2,078 | 1,412 |
| 5. Parse stored (work: 94–203 ms in the task, parse itself 8 ms) | 1,889 | 2,110 | 1,452 |
| 6. "View results" on the progress page | **5,825** | **5,877** | **2,756** |
| 7. Editor open after "Edit as a CV" | 6,682 | 6,710 | 3,580 |
| 8. Score and checks on screen | 196 | 272 | 273 |
| 9. Model review task received (queue wait) | 123 | 173 | 105 |
| 10. Model review on the page (model 83.4 s / 58.8 s / 66.0 s) | 85,829 | 61,554 | 67,835 |

### What the gaps were, and what changed

1. **Progress page poll (stage 6).** The page asked every 5 s, so a CV parsed
   at 1.9 s was shown at 5.8 s. Both waiting screens (`resume/processing.html`,
   `resume/_upload_history.html`) now ask every 2 s, and still stop asking
   once nothing is moving. Result: 2.8 s.
2. **Parse duration recorded as 0 ms.** `services.parse` timed itself with
   `time.monotonic()`, which ticks about every 16 ms on Windows, and the parse
   (text reused from validation) is faster than one tick. Now
   `time.perf_counter()`: 8 ms recorded.
3. **Outbox hop (stage 4), not changed.** Parsing is queued by a subscriber
   to the `file.scanned` outbox event (`resumes/consumers.py`), and the outbox
   worker sleeps `OUTBOX_POLL_SECONDS` (default 2) when idle, so the hop costs
   0–2 s (1.2–1.8 s measured). That is the documented ordering guarantee of
   the outbox (DEVELOPER.md §4.5), and the setting is per environment; it was
   left as designed.
4. **Queues are not the bottleneck.** Every task was picked up within 125–290
   ms of being queued. The model review (59–83 s on this CPU) is the whole of
   the remaining wait, as in the in-process measurements above.

No duplicate tasks: one scan, one parse and one review per upload in every
run (PF-3). `analysis.enrich` is idempotent by state: a report already
reviewed, or with a review in progress, is not reviewed again.
