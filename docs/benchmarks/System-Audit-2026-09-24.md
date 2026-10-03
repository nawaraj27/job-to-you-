# CareerAI — System Performance & Quality Audit (2026-09-24)

Every number on this page was **measured on the development machine today**;
nothing is simulated or estimated. Where a feature does not exist, the page
says so instead of scoring it as passed.

- Machine: Windows 11, CPU only; Postgres + Redis in Docker; Celery workers;
  Ollama `qwen2.5:7b-instruct` (local); faster-whisper `base.en` (local).
- How each thing was exercised is stated per row: **HTTP** (real requests to
  the running dev server with the demo candidate's session), **service** (the
  same service call the view makes, from a script), **benchmark** (the
  project's own corpus runners), **run log** (every AI call recorded in
  `AIRun` on this database). Nothing here is a person clicking.
- Test inputs were synthetic CVs made for this audit (`audit-cv.docx` and an
  image-only `audit-cv-scanned.pdf`, same content); they were removed
  afterwards.

## 1. Context check and interview quality

**Candidate context parsed** (from `audit-cv.docx`): Maya Karki, Pokhara;
backend developer since Jun 2022 (Junior Developer, Annapurna Treks → Backend
Developer, Phewa Stays, current). Claims with figures: Flask → Django REST
Framework migration, p95 900 ms → 250 ms; Celery + Redis confirmation emails,
3,000 a day; PostgreSQL index work removing a nightly timeout; Redis
availability cache, 70% fewer database reads; trek-permit tracker used by 8
guides; GitHub Actions running pytest. Skills listed: Python, Django, DRF,
PostgreSQL, Redis, Celery, Docker, Git, pytest, GitHub Actions.

**Role requirements** (Summit Digital Labs, Backend Developer, mid): required
Python, Django, REST APIs, PostgreSQL; preferred Docker. Duties: design and
maintain REST APIs for booking apps, own PostgreSQL schema changes, review pull
requests and keep the test suite fast.

**Context match: High.** Every required skill is on the CV with a described use.
Docker is listed but no use of it is described — the one claim an interviewer
should probe.

**What the product actually uses.** The interview kit is built from the **job
only**, by design (`apps/interviews/services/planning.py`): one pool per
posting, so every applicant faces comparable questions and no applicant's CV
enters a pool other people are asked from. The individual is reached through
**follow-up questions** written from the answer just given. So the product
does not generate CV-specific questions, and this audit did not pretend it
does.

**Questions tailored to this CV and role** (written by the auditor for
comparison — *not* product output):

1. *Technical, from a CV claim:* "You moved the booking API to Django REST
   Framework and p95 fell from 900 ms to 250 ms. Where did the 650 ms go —
   what did you measure, and which change was worth the most?"
2. *Project, from a CV claim:* "Your Celery queue sends 3,000 confirmation
   emails a day. When a worker dies mid-task, how do you make sure a guest gets
   exactly one confirmation — not zero, not two?"
3. *Scenario, joining a CV claim to the job:* "Your Redis cache says a room is
   free; two guests book it within the same second. Walk me through what stops
   a double booking, and where the source of truth lives."

**What the product generated** (Backend Developer kit, 224.9 s, one model call,
16 questions in api_design / code_review / technology_stack /
database_management). On-topic but formulaic, e.g. "Describe a time you
designed a REST API for a project. What challenges did you face…"; one
question wrongly presumes the candidate worked on "the Django service at your
previous job". The deterministic fallback used before the kit existed (30
questions) is sharper and names the posting's own duties.

**Improvement over generic questions.** Kit questions are role-specific (API
design, schema migrations, test-suite speed) rather than generic HR. The
follow-up generated in the audit session was the one question that engaged
this candidate: *"How did you decide on the caching strategy for availability
in Redis, and how did you ensure it didn't cause data inconsistencies?"* —
built from the answer, 30.0 s. Questions that test CV claims before an answer
is given (like 1–3 above) would need the CV in the plan: a product decision
(it is safe for private practice, not for a shared screening pool).

## 2. Feature-by-feature breakdown

| Feature | Test input / scenario | Time (measured) | Status | Score |
|---|---|---|---|---|
| CV upload (DOCX) | `audit-cv.docx`, HTTP upload | 300 ms accept; 1.15 s upload → parsed (parse 406 ms) | Works; 2 defects found and fixed | 8/10 |
| CV upload (scanned PDF) | Image-only PDF, HTTP upload | 124 ms accept; 1.27 s to a clear "this is a picture of a CV" message | **No OCR — nothing extracted** | 3/10 |
| Contextual interview questions | Backend Developer kit + practice session | Kit 224.9 s (history: median 273 s, 17% schema errors); session opens 226 ms; follow-up 30.0 s | Job-tailored, not CV-tailored (by design) | 6/10 |
| Interview answer evaluation (text) | 5 answers: 3 strong, 1 non-answer, 1 off-topic | 132.8 s for 2 competencies (history: median 35.5 s per call, p90 378 s) | Works; too lenient | 5/10 |
| Spoken answers (voice) | Speech benchmark, 10 recorded answers | Median 2.32 s per answer; WER 2.4% | Works | 8/10 |
| Live video interview | Two people in a room | — | **Not tested** (needs people and devices) | — |
| CV generation | 3 templates × PDF + DOCX | PDF 31–765 ms, DOCX 171–688 ms (first render pays warm-up); cached re-export 27–66 ms | Works; 1 page; ATS round trip clean after fix | 9/10 |
| Prompt-based CV builder | 4 raw notes typed to the assistant | 38.3–84.3 s per reply (history: median 43.2 s) | No invention; loses results | 5/10 |

## 3. Deep dive

### A. CV upload and parsing

- **DOCX:** name, email, phone, both roles with employer and dates, all five
  bullets, the degree and all ten skills extracted; nine import proposals
  offered, nothing written until accepted. Misses: the location on the contact
  line ("Pokhara, Nepal") is not extracted, and the employer keeps its
  location suffix ("Phewa Stays - Pokhara").
- **Defects found and fixed during the audit:**
  - **PARSE-01** — a Projects section written as bullets with no project-name
    line was dropped entirely (the Redis cache project vanished). A bullet with
    no name above it is now a project of its own.
  - **PARSE-02** — Word delivers a bulleted list with blank lines between items,
    and after a blank line "• Python" or "• Docker" passed the heading test once
    the bullet was stripped: each skill became an empty section and the Skills
    section disappeared. Hit uploaded Word CVs with bulleted skills, and the
    product's **own DOCX exports read back with 0 skills**. A bulleted line is
    never a heading now.
  - Parser engine bumped 1.2 → 1.3 (the project's rule for any change in
    output), so `python manage.py reparse_resume_uploads` refreshes the 34
    uploads on this database parsed by the old engine.
- **Scanned PDF:** there is no OCR in the product. The file is accepted,
  scanned for viruses and parsed in 1.27 s, and the candidate is told plainly
  that it is a picture of a CV no applicant tracking system can read — honest,
  but nothing is extracted. Adding OCR (e.g. Tesseract) is a feature, not a
  fix.
- **Extraction benchmark** (36 cases, PDF and DOCX, after the fixes): macro-F1
  1.000, 0 fabrications, slowest case 265 ms.
- **Satisfaction: DOCX 8/10** — fast and accurate on everything that matters to
  matching; location still missed. **Scanned PDF 3/10** — the failure is
  handled well, but the capability is absent.

### B. Interview questions and evaluation

- Kit generation is slow on the local model (224.9 s; median 273 s over 101
  runs; 23 schema errors and 5 timeouts in 136 attempts). The candidate never
  waits for it — a session opens in 226 ms on the fallback pool and the kit is
  built in the background.
- Follow-ups are the strongest part: grounded in the answer, 30.0 s here
  (median 11.1 s over 149). One follow-up that repeated an earlier question was
  generated and correctly dropped (13.2 s spent).
- **Evaluation is too lenient.** Code review was answered with "I am not sure,
  I would probably just look it up" and an off-topic API story; it scored
  **3/5**, the same as the strong answers, with a rationale ("describes a
  concrete action to optimize the test suite") that nothing in the answers
  supports. The quote check only proves the quoted words exist, not that they
  answer the competency. This is the local 7B model; re-test with a hosted
  model before trusting practice scores.
- Hardening note (not reachable with today's single `ai` worker): AI scores
  have no unique constraint per (session, competency), so two evaluations
  running at the same moment write two rows. The audit produced this only by
  running an evaluation inline while the worker ran its own.
- **Voice:** 10 recorded answers, word error rate 2.4% overall (worst case 7.4%),
  median 2.32 s to transcribe, long pauses detected 3/3.
- **Satisfaction: questions 6/10** (relevant, formulaic, not CV-aware by
  design; follow-ups good). **Evaluation 5/10** (works, explains itself, does
  not separate a non-answer from a strong one). **Voice 8/10.**

### C. Resume creation (templates and prompt-driven)

- **Templates:** Meridian, Circuit and Vertex, PDF and DOCX, one page each;
  reading every file back with the product's own parser recovers both roles,
  all ten skills and every figure in the source (after PARSE-02). **9/10.**
- **Prompt-driven builder**, input by input:

  | Typed | Reply time | Offered |
  |---|---|---|
  | "shift supervisor at himalayan java cafe … weekly rota for 9 staff and cut overtime maybe 20 percent, also trained the new baristas" | 84.3 s | Title "Shift Supervisor", employer "Himalayan Java Cafe", bullet "Trained new baristas" |
  | "set up online ordering with google forms and sheets, orders went from about 30 to 55 a day" | 66.7 s | "Set up online ordering with Google Forms and Sheets"; then asked "What did that change?" although it had been said |
  | "i helped with marketing stuff too" | 38.3 s | Nothing; asked for detail (the right move) |
  | "posted on instagram 3 times a week … student discount … around 40 new regulars" | 51.3 s | Three separate bullets, the result split from its action |

  No figure was invented (0 numbers outside the notes). But Action + Result
  bullets were lost: the model wrote "Created weekly rota for 9 staff and
  reduced overtime by 20 percent" and the anti-fabrication filter dropped it,
  because its opening verb ("Created") is not a word the candidate used ("did
  the rota"). The rule is deliberate — it is what stops "Trained two tellers"
  being put on a teller's CV — so loosening it (e.g. letting "did / handled /
  was in charge of" ground "Created / Managed / Ran") is a product decision.
  The other losses are the model's own output (result dropped or split).
- **Fixed during the audit — ASST-01:** a reply that already ended with its
  question had the question appended again ("Can you tell me more about your
  role in marketing? Can you tell me more about your role in marketing?").
- **Satisfaction: 5/10** — safe (nothing invented) and it asks when detail is
  missing, but it drops the results that make a bullet worth having, and each
  reply takes 40–85 s on the local model.

## 4. Overall

**Suite satisfaction: 6.3/10** — mean of the seven scored rows (8, 3, 6, 5, 8,
9, 5 = 44/7); the untested live-video row is excluded, not guessed.

**Top issues (not fixed — decisions or external):**

1. **No OCR** for scanned CVs — add an OCR step, or keep the current honest
   refusal. Feature decision.
2. **Evaluation leniency and AI latency** come from the local 7B model; test
   with a hosted model (already an external blocker) before judging scoring
   quality.
3. **Prompt builder loses results** — decide whether paraphrased verbs may
   ground a bullet; consider instructing the model to keep action and result in
   one bullet.

Also open: interview questions are not CV-aware (a privacy/fairness decision for
shared pools; safe to add for private practice only); location not parsed from
the contact line; an AI-score uniqueness constraint as hardening.
