# Assisted CV

The primary way to build a CV (tracker rows AS-1 to AS-4). The candidate
describes the CV they want in their own words; the assistant asks only for
what is missing and drafts CV lines only from the answers; the candidate
reviews every line and saves.

## Where it is

| Entry point | URL / place |
|---|---|
| Start screen | `/cv/builder/describe/` (`resume:ai`). Shown when the candidate has no CVs, or with `?new=1` |
| Start action | `POST /cv/builder/describe/start/` (`resume:assistant_start`) |
| Conversation | `/cv/builder/<document>/describe/` (`resume:assistant`) |
| Navigation | First item in the **CV tools** menu and the candidate sidebar ("Assisted CV"); first card on the CV hub ("Start with Assisted CV"); pre-selected mode when starting a CV |

## The start screen (`templates/resume/assistant_start.html`)

- One large prompt box: "Describe the CV you want".
- An optional "Job you are aiming for" field.
- Four example prompts (`ASSISTANT_EXAMPLES` in `portals/candidate/resume_views.py`).
  Clicking one fills the box (`data-fill`, `static/js/app.js`) for the
  candidate to change; nothing is sent until they press **Start my CV**.
- A three-step strip: describe, answer, review and save.
- Secondary routes below: start from a template, upload a CV.

### Starting

`AssistantStartView.post`:

1. Refuses fewer than 15 characters with a 400 and the text kept in the box;
   nothing is created.
2. Creates an **unsaved draft** (`docs/CV-Lifecycle.md`) in assistant mode,
   named "CV for <target>" when a target is given.
3. Sends the description as the first turn through `services.improvements.submit`,
   answered in the background on the `ai` queue like every other turn.
4. A hidden one-time token (`ImprovementJob.request_token`) makes a
   double-click or resubmitted form start one CV, not two.

### Consent to reuse

If the candidate already has a parsed upload or a profile headline, a checkbox
offers "Start from what I have already given Kaarya". It is **never
pre-ticked** (`ReuseIsOptInTests`). Unticked, the draft starts blank. Ticked,
it starts from the parsed upload, with every entry marked as imported.

## The conversation page (`templates/resume/assistant.html`)

Order on the page is the order of the work:

1. **Talk it through** (step 1 of 2): the transcript and composer. The
   background job panel shows the stage, survives a refresh and offers a
   retry.
2. **Review and edit your CV** (step 2 of 2): drafted lines arrive under
   Suggestions beside the live preview. Nothing reaches the CV until the
   candidate accepts it; the CV is kept when they save a version.
3. **Improve against your latest analysis** (optional, collapsed).

## How the assistant behaves (`apps/resumes/services/assistant.py`)

- **Only what the candidate said.** Bullets are built from claims in the
  candidate's own messages (`services.evidence`). A message that disowns a
  claim ("I didn't do that") is not evidence for it.
- **Targeted questions.** One gap at a time when aiming at a job ("Have you
  used Kubernetes? If so, where?"). "Skip" moves to the next gap without a
  model call.
- **No repeated questions.** `_mentions` drops a follow-up that the reply
  already asks, including in other words (60% of its content words in one
  question).
- **Stops asking for numbers.** Once the candidate says they don't remember a
  figure (`_FORGOT`), questions that ask for one in any phrasing
  (`_FIGURE_QUESTION`: "how many", "the number of …", "roughly", "quantify",
  "percentage" …) are removed from replies (`_without_figure_questions`).
- **No second person in bullets.** "during your two-year tenure" is rewritten
  or rejected; a CV line is about the work, not addressed to the reader.

## Allowance (ADR-A59)

Each answered turn uses one **assisted CV reply** (`assistant_replies`,
daily): Free 20 a day, Pro 300 a day, set in `apps/billing/seeds.py`.

- Claimed before the model runs (`entitlements.claim`, atomic).
- Released if the model run does not succeed (timeout, provider or schema
  error), so a failure costs nothing.
- At the limit the model is not called; the conversation records the
  candidate's message and answers with the limit, how many were used, the
  reset date and the upgrade route. Nothing they wrote is lost.

## Tests

- `apps/resumes/tests/test_assisted_cv.py`: start screen, `?new=1`, start
  creates a draft and the first turn, double-submit, too-short text, reuse
  only when ticked, never pre-ticked, page order, paraphrased questions,
  forgotten figures, metering and refund, no candidate profile yet.
- `apps/resumes/tests/test_assistant.py`, `test_evidence.py`: grounding,
  skip, second-person bullets.
- Browser: Journey B (start from an example, real model reply in 82 s, a
  "don't remember" answer, accept a line, save a version, CV listed as saved).
