# Local Decision Model / Jev-Like Benchmark Report

2026-09-26. Every figure below was measured on this machine by
`scripts/decision_benchmark.py` and `manage.py interview_benchmark`; raw
per-case results are in `var/benchmark/decision/`. Nothing is estimated.

## 1. Executive Summary

**Question.** Can a free, fully local, zero-cost "Jev-like" decision model make
CareerAI more accurate, faster or more reliable than what it does today?

**Official Jev cannot be used under that requirement.** TypeSafe's own
documentation (docs.typesafe.ai/models) lists Jev 1.13 (`jev-1.13.0`, aliases
`jev-latest`, `jev-preview`) as served only by the hosted endpoint
`POST /v1/systemone`, at $0.042 per million input tokens (output free), with no
free tier listed, no downloadable weights and no self-hosting. The docs index
(docs.typesafe.ai/llms.txt) has no page on self-hosting, on-premises or open
weights. It is neither local nor zero-cost. Nothing in this report is Jev.

**Tested instead** (all open, local, Ollama, CPU): Flow-Judge-v0.1 (a
purpose-built rubric evaluator — the closest open analogue to a decision
model), Microsoft phi4-mini 3.8B, and Qwen2.5 1.5B, against the current
`qwen2.5:7b-instruct` and CareerAI's existing deterministic logic. The
installed `nomic-embed-text` was also tried as an embedding relevance signal.

**Use cases benchmarked:** interview answer relevance / non-answer / evidence /
follow-up (39 labelled answers, adversarial categories A–M); interview scoring
(the existing 50-case corpus); CV ↔ job requirement classification (24
labelled cases). Readiness/gap is derived from requirement coverage and was
judged on that result; recommendation ranking has no model decision step.

**Outcome: nothing was integrated.** No candidate beat the current
implementation on any task where the difference mattered; the fastest ones were
markedly worse, and the one purpose-built judge was the least accurate scorer.
Production code is unchanged. A clean "do not integrate" is the result.

## 2. Environment (detected)

| Item | Value |
|---|---|
| OS | Windows 11 Home 10.0.26200 |
| CPU | AMD Ryzen 7 5800HS, 8 cores / 16 threads, 3.2 GHz base |
| RAM | 15.4 GB (4.0 GB median free during the runs; minimum observed 60 MB) |
| GPU | AMD Radeon integrated graphics only (WMI reports 512 MB shared); no discrete GPU, no CUDA — Ollama ran on the CPU |
| VRAM | none dedicated |
| Disk | 16.3 GB free on C: before downloads |
| Ollama | 0.34.4 (runner process `llama-server.exe`) |
| Existing models | `qwen2.5:7b-instruct` (4.7 GB), `nomic-embed-text` (274 MB) |

## 3. Models Tested

| Model (Ollama tag) | Publisher | Source | License | Params / quant | Download | Loaded RAM | Role here |
|---|---|---|---|---|---|---|---|
| `qwen2.5:7b-instruct` | Alibaba Qwen | ollama.com (already installed) | Apache 2.0 | 7.6B / Q4_K_M | 4.7 GB | 4.8 GB | **current** |
| `phi4-mini:3.8b` | Microsoft | ollama.com; license from HF `microsoft/Phi-4-mini-instruct` | MIT | 3.8B / Q4_K_M | 2.5 GB | 3.0 GB | small general model as classifier |
| `qwen2.5:1.5b` | Alibaba Qwen | ollama.com | Apache 2.0 | 1.5B / Q4_K_M | 986 MB | 1.1 GB | tiny classifier |
| `hf.co/flowaicom/Flow-Judge-v0.1-GGUF:Q4_K_M` | Flow AI | Hugging Face `flowaicom/Flow-Judge-v0.1-GGUF` | Apache 2.0 | 3.82B (Phi-3.5-mini base) / Q4_K_M | 2.39 GB | 3.9 GB | purpose-built rubric evaluator |
| `nomic-embed-text` | Nomic | already installed | — | — | — | 0.36 GB | embedding relevance signal |

Considered and not downloaded: **Bespoke-MiniCheck-7B** (grounding checker,
in the Ollama library) — CC BY-NC 4.0, non-commercial; **Qwen2.5 3B** — Qwen
license, not Apache. **Official Jev** — hosted and paid (§1).

## 4. Benchmark Dataset

`scripts/decision_benchmark_cases.json`:

- **Relevance — 39 answers**, 3 per category: A correct, B non-answer, C
  uncertain, D long but irrelevant, E persuasive off-topic, F partial, G short
  but correct, H verbose but incomplete, I with evidence, J unsupported claim, K
  contradictory, L ambiguous, M should trigger a follow-up. Labels: addresses
  (yes 9 / partly 18 / no 12), evidence (sufficient 15 / insufficient 24),
  follow-up (yes 18 / no 21). Varied roles (backend, design, nursing, finance,
  marketing, logistics, teaching).
- **Requirements — 24 (CV excerpt, required skill)**: satisfied 8, partial 8
  (listed only, coursework, "learning"), missing 8 (including near-misses:
  Java vs JavaScript, MySQL vs PostgreSQL).
- **Scoring — 50 cases** of `apps/interviews/benchmark_cases.json` with score
  bands (existing corpus; bands written in earlier loops before those runs).

**Who labelled:** the AI assistant that built the benchmark, before running any
model, against criteria written into the file. **Not human-annotated.**
Agreement means agreement with those criteria. Small sets: one wrong label moves
relevance accuracy by 2.6 points and requirement accuracy by 4.2.

## 5. Accuracy Results

### Answer relevance (39)

"Current" is the production scoring path (`evaluation._score_competency`:
qwen2.5:7b, prompt `interview.evaluate` v6, deterministic rules), reading its
per-answer judgement after the deterministic checks. The classifiers all get
one identical narrow prompt with an Ollama JSON schema.

| System | addresses accuracy | macro-F1 | off-topic/non-answer called good (of 12) | good answer called off-topic (of 9) | evidence accuracy | follow-up accuracy |
|---|---|---|---|---|---|---|
| **Current (production)** | 0.615 | 0.600 | **1** (E1) | 0 | **0.923** ¹ | 0.462 ² |
| qwen2.5:7b, narrow prompt | 0.692 | 0.696 | **1** (E1) | 0 | 0.821 | 0.821 |
| phi4-mini | 0.692 | 0.698 | 2 (E1, E3) | 0 | 0.872 | 0.821 |
| qwen2.5:1.5b | 0.462 | 0.431 | 3 (D3, E1, E3) | 0 | 0.615 | 0.487 |
| Flow-Judge (3-point rubric) | 0.692 | 0.697 | 3 (C1, C3, E1) | 0 | — | — |
| nomic-embed cosine, on-topic vs not | AUC 0.929; best-threshold accuracy 0.872 (optimistic: threshold chosen on the same data) | | | | — | — |

¹ Read off the production score (3+ = concrete own work); the production path
has no separate evidence decision. ² The production path has no follow-up
decision — it asks one whenever the competency has budget — so this is the
accuracy of "always yes".

By category (addresses correct / 3):

| Category | Current | qwen7b | phi4-mini | qwen1.5b | Flow-Judge |
|---|---|---|---|---|---|
| B non-answer | 3 | 3 | 3 | 3 | 3 |
| C uncertain | 3 | 3 | 3 | 3 | **1** |
| D long irrelevant | 3 | 3 | 3 | 2 | 3 |
| E persuasive off-topic | 2 | 2 | **1** | **1** | 2 |
| F partial | 1 | 2 | 0 | 0 | 2 |
| H verbose incomplete | 0 | 0 | 3 | 0 | 2 |
| J unsupported claim | 0 | 1 | 1 | 0 | 2 |
| L ambiguous | 0 | 0 | 2 | 0 | 1 |
| M needs follow-up | 0 | 1 | 0 | 0 | 0 |
| A, G, I (good answers) | 9/9 | 9/9 | 9/9 | 9/9 | 8/9 |

Current's "misses" in H/J/L are answers it scored **1 ("no")** where the label
is "partly": conservative, not generous — the failure that matters for
CareerAI (crediting a non-answer) is 1 case for current and the 7B, 2–3 for the
candidates.

### Interview scoring (50, score bands)

Same production pipeline for the three Ollama models (prompt v6, deterministic
bounds unchanged; only `OLLAMA_MODEL` differs). Flow-Judge uses its own
template with the same 5-level rubric, after the production non-answer rule.

| System | In band | Worst misses |
|---|---|---|
| **qwen2.5:7b (current)** | **48/50 (96%)** | parquet_incorrect 3 (band ≤2); v3_unsure_then_off_topic 3 (≤2) |
| phi4-mini | 42/50 (84%) | two unsupported-claim cases 3 (≤2); two jokes 2 (=1); two strong senior answers 3 (≥4) |
| qwen2.5:1.5b | 39/50 (78%) | **android_unsupported_claim 5, marketing_unsupported 5** (≤2); two strong concise answers 1 (≥3) |
| Flow-Judge | 37/50 (74%) | **v3_unsure_then_off_topic 5** (≤2); six factually wrong answers 4 (≤2) |

### CV ↔ job requirement (24)

| System | Accuracy | macro-F1 | Pattern |
|---|---|---|---|
| **Current (deterministic taxonomy match)** | 0.542 | 0.439 | Cannot say "partial": all 8 listed-only skills → satisfied; 3 skills described but not named (UX research, REST APIs, content marketing) → missing; 8/8 missing correct |
| qwen2.5:7b, narrow prompt | 0.792 | 0.788 | 3 of 8 missing → partial (over-credit); 2 partial → satisfied |
| phi4-mini | 0.500 | 0.400 | All 8 missing → partial |
| qwen2.5:1.5b | 0.375 | 0.246 | **All 8 missing → satisfied** |

## 6. Performance Results

Per case, warm model, one model loaded at a time, nothing else running.
"Classifier" rows are the narrow prompt; "pipeline" rows are the full
production scoring call.

| Task | System | Median | P95 | Min | Max |
|---|---|---|---|---|---|
| Relevance | qwen2.5:7b classifier | 8.75 s | 12.2 s | 6.6 s | 34.5 s |
| Relevance | phi4-mini classifier | 6.52 s | 8.08 s | 5.7 s | 21.1 s |
| Relevance | qwen2.5:1.5b classifier | 4.05 s | 4.71 s | 3.8 s | 10.4 s |
| Relevance | Flow-Judge | 28.9 s | 37.9 s | 21.1 s | 40.8 s |
| Relevance | Current (full scoring call; obvious non-answers take 0 s) | 28.0 s | 88.3 s | 0.0 s | 95.7 s |
| Relevance | nomic-embed (two embeddings) | 4.17 s | 4.21 s | 4.1 s | 5.1 s |
| Requirements | Current (deterministic) | < 1 ms | < 1 ms | | |
| Requirements | qwen2.5:7b | 4.05 s | 4.38 s | | |
| Requirements | phi4-mini | 3.34 s | 3.66 s | | |
| Requirements | qwen2.5:1.5b | 2.64 s | 2.69 s | | |
| Scoring (model call) | qwen2.5:7b pipeline | 34.6 s | p90 43.3 s | | |
| Scoring (model call) | phi4-mini pipeline | 21.4 s | p90 31.0 s | | |
| Scoring (model call) | qwen2.5:1.5b pipeline | 11.5 s | p90 15.5 s | | |
| Scoring | Flow-Judge | 44.0 s | 55.1 s | 27.4 s | 63.7 s |

Where the time goes (relevance classifier medians, Ollama's own counters):
generation 4.03 s / 2.97 s / 1.27 s and prompt evaluation 2.43 s / 1.50 s /
0.68 s for 7B / phi4-mini / 1.5B; the rest is HTTP and model scheduling. The
first call after a model loads is the slowest (the 34.5 s and 21.1 s maxima).

**Cold start** (model unloaded, three trials; first call total, of which load):
qwen2.5:7b 15.1–20.6 s (load 11.3–16.7 s); phi4-mini 7.5–9.8 s (5.0–7.3 s);
Flow-Judge 6.5–9.5 s (3.8–6.9 s); qwen2.5:1.5b 5.1–6.4 s (2.7–3.9 s). A warm
one-word reply took 2.15–2.45 s on every model — the floor on this CPU.

**Resources** (`llama-server.exe`, sampled every 0.5 s during classification
calls): resident memory equals the loaded size above (7B 4.8 GB, Flow-Judge 3.9
GB, phi4-mini 3.0 GB, 1.5B 1.1 GB); CPU while generating ≈ 47–50 % of 16 threads
on every model, i.e. all 8 physical cores — smaller models finish sooner, they
do not use less CPU. Loading a second model next to the 7B would need 7–9 GB
together on a 15.4 GB machine whose free memory fell to 60 MB during the runs.

## 7. Reliability

- Structured output: 189 schema-constrained classifier calls (39 × 3
  relevance + 24 × 3 requirements), **0 invalid**. Flow-Judge: 86 calls (39 relevance + 47 scoring), 0
  missing or out-of-range `<score>`. Scoring pipelines: qwen2.5:1.5b 1 schema
  error in 57 calls (handled by the existing retry); 7B and phi4-mini none.
- Deliberate failure probes (harness client): unknown model → HTTP 404, typed
  error; Ollama unreachable → typed "unavailable"; 10 ms timeout → typed
  "timeout". Validator: malformed JSON, empty output, missing field, invalid
  enum, wrong type, array, prose and truncated Flow-Judge output all rejected;
  an extra field is ignored and only validated fields are kept.
- The production path's own failure handling (schema error → one retry → unscored;
  provider down/timeout → unscored, interview not stranded; duplicate scoring →
  one row) is unchanged and covered by the existing suite (`test_scoring_hardening`,
  `test_evaluation`, `test_concurrency`, `apps/ai` runner tests).

## 8. Failure Analysis

**Candidates.** phi4-mini credits persuasive off-topic answers more often than
the 7B (E3: a design-system success story accepted as UX research) and, in the
scoring pipeline, lets unsupported claims reach 3 and under-scores strong
senior answers. Qwen2.5 1.5B calls every partial answer good, gives 5/5 to
bare claims, and marks every missing requirement satisfied. Flow-Judge treats
"I'm not sure, maybe I'd…" as a partial answer, gives 4 to confidently wrong
technical answers (it does not check facts), and scored the audit's off-topic
case 5 — the exact failure the last gate closed.

**Current system.** It still credits one persuasive off-topic answer (E1 — the
API-latency story for a test-suite question), as every model tested does; it
scores vague, unsupported and ambiguous answers 1 where the labels say
"partly" (conservative); it misses one wrong claim (Parquet). Its requirement
matching cannot express "listed but never used" and misses skills that are
described without being named.

## 9. Regression Results

| Check | Result |
|---|---|
| Baseline (last freeze, same production code) | 3910 tests OK |
| Final full suite | **Ran 3910 tests in 897 s — OK** (serial, `config.settings.test`; log `var/benchmark/full-suite-decision-2026-09-26.log`); no new or removed tests, no failures |
| Production code changed | none (tree diff against `freeze-manifest-2026-09-25`: 0 changed files; added `scripts/decision_benchmark.py`, `scripts/decision_benchmark_cases.json`) |
| ruff check / format | clean, 696 files |
| mypy `apps` | Success, 379 source files |
| `makemigrations --check --dry-run` | No changes |
| `manage.py check` | No issues |

## 10. Per-Use-Case Decision

### Interview evaluation (scoring and per-answer relevance) — **DO NOT INTEGRATE**

The current 7B pipeline is the most accurate scorer (48/50 against 42, 39 and
37) and ties the best candidate on relevance while making fewer of the costly
errors (1 off-topic answer credited against 2–3). The faster candidates buy
their speed with exactly the failures the product must not have (5/5 for bare
claims). Scoring runs in the background worker, so its latency is not on the
candidate's path.

### CV ↔ job matching — **DO NOT INTEGRATE** (candidate models)

phi4-mini (0.50) and Qwen 1.5B (0.38) are no better than the deterministic
matcher (0.54) and fail in the dangerous direction (the 1.5B calls every
missing skill satisfied). *Observation, not integrated:* the existing 7B, used as
a classifier, scored 0.79 — it recognises "listed but never used" and described
skills — but it also calls 3 of 8 missing skills "partial", costs about 4 s per
requirement, and the deterministic matcher is model-free by design
(`resumes/analysis/target.py`: reproducible, explicable, free). Replacing it is
a product decision, not a benchmark result.

### Readiness / gap classification — **DO NOT INTEGRATE**

Readiness is computed from the requirement coverage above plus deterministic
section and ATS rules; the same evidence applies, and the rules it enforces are
not model decisions.

### Recommendation / routing — **DO NOT INTEGRATE**

Recommendation ranking has no model decision step (embeddings plus
deterministic scoring), so there was nothing to replace. Follow-up routing: the
classifiers predicted "follow-up needed" at 0.82 against 0.46 for the current
"always ask", but on 39 assistant-labelled cases, with phi4-mini no better than
the 7B, and at the price of either a second resident model (not viable next to
the 7B in 15.4 GB) or an extra 7B call per answer. The evidence does not justify
the change; it is a candidate for a future experiment on human-labelled data.
The embedding signal (AUC 0.93) is promising as an off-topic flag but its
accuracy figure is optimistic, and it was not integrated.

## 11. Final Architecture

Unchanged. `config/ai.py` still routes every task through the same chain
(`capable` → `cheap` → `local` = `OLLAMA_MODEL`, `qwen2.5:7b-instruct` in
development); interview scoring keeps its per-answer judgements and
deterministic bounds; requirement matching and readiness stay deterministic;
permissions, billing, privacy and ownership never touched a model. The only
additions are the benchmark script and its corpus.

## 12. Known Limitations

- Labels are the assistant's, not human; sets are small (39 / 24 / 50).
- One run per case at temperature 0; run-to-run variance was not measured here
  (the 7B scoring corpus gave 48 of 50 in band with the same two misses as the previous day's run).
- CPU-only laptop; a GPU would change every latency figure and possibly the
  trade-off.
- Flow-Judge was used with its published template and a CareerAI rubric; a
  different rubric wording might score differently.
- The "current" relevance row reads the production scoring call's judgement,
  which also scores the answer, so its latency is not a like-for-like
  classification time.
- A hosted model (external dependency) was not tested.
- The downloaded candidate models remain in Ollama (5.9 GB) so the benchmark
  can be reproduced; remove them with the commands below.

## 13. Reproduction Commands

```bash
ollama pull qwen2.5:1.5b
ollama pull phi4-mini:3.8b
ollama pull hf.co/flowaicom/Flow-Judge-v0.1-GGUF:Q4_K_M

python scripts/decision_benchmark.py coldstart --system phi4-mini:3.8b
python scripts/decision_benchmark.py relevance --system current
python scripts/decision_benchmark.py relevance --system classifier:phi4-mini:3.8b
python scripts/decision_benchmark.py relevance --system flowjudge
python scripts/decision_benchmark.py relevance --system embed
python scripts/decision_benchmark.py requirements --system current
python scripts/decision_benchmark.py requirements --system classifier:qwen2.5:7b-instruct
python scripts/decision_benchmark.py score --system flowjudge

# The production scoring pipeline with another local model (bash syntax):
OLLAMA_MODEL=phi4-mini:3.8b python manage.py interview_benchmark --live --only grounding --out var/benchmark/decision/score-pipeline-phi4-mini.json

# Tests
python manage.py test --settings=config.settings.test

# Remove the candidate models
ollama rm qwen2.5:1.5b phi4-mini:3.8b hf.co/flowaicom/Flow-Judge-v0.1-GGUF:Q4_K_M
```

Nothing was integrated, so there is nothing to enable or disable. The local
model the application uses is chosen by `OLLAMA_MODEL`, as before.

Sources: TypeSafe docs — https://docs.typesafe.ai/models,
https://docs.typesafe.ai/llms.txt; https://ollama.com/library/phi4-mini;
https://huggingface.co/microsoft/Phi-4-mini-instruct;
https://ollama.com/library/qwen2.5; https://ollama.com/library/bespoke-minicheck;
https://huggingface.co/bespokelabs/Bespoke-MiniCheck-7B;
https://huggingface.co/flowaicom/Flow-Judge-v0.1;
https://huggingface.co/flowaicom/Flow-Judge-v0.1-GGUF.
