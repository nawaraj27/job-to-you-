# CV extraction and review — benchmark record

What this file is: the before and after for every change made to the CV reading
and review pipeline, with the evidence that moved the number. It is a record,
not a plan. Anything still listed as a limitation is genuinely unsolved, not
merely unfinished.

Run it yourself:

```bash
python manage.py resume_benchmark --verbose
python manage.py resume_benchmark --json var/benchmark/extraction.json
python manage.py test apps.resumes.tests.test_extraction_benchmark
python manage.py test apps.resumes.tests.test_accessibility
python manage.py test          # the whole suite
ruff check . && ruff format --check . && mypy
```

## The corpus

`apps/resumes/benchmark/corpus.py`. Thirty-six (document, format) pairs across
32 CVs, each one written by hand together with the ground truth it was written
from, rendered to real PDF or DOCX bytes and read back through the production
reader. Counted from the code rather than from memory: `len(CASES)` is 32 and
`sum(len(case.formats) for case in CASES)` is 36. Nothing is mocked: a regression in the PDF text layer counts against
the score exactly as a regression in the section splitter does.

The corpus is generated rather than collected because real CVs are personal
data and cannot live in a repository, and because a benchmark nobody can re-run
is a number somebody once saw.

| Kind | Cases |
|---|---|
| Career stage | experienced, graduate, undergraduate, school leaver, early career, career changer, academic |
| Sector | software, finance, healthcare, education, marketing, sales, design, engineering, hospitality, administration, research, management, retail, social |
| Layout | single column, two column, sidebar labels, Word tables (2- and 3-cell), form style, multi-page |
| Content | Unicode names, letter-spaced titles, bracketed skills, one skill per line, wrapped bullets, keyword stuffing, dates in six spellings, no contact details, no employment, scan, near-empty |

Three rules the corpus obeys, because breaking them turns it into decoration:
not every CV has every field and the truth says so; the hard cases stay in; the
documents are realistic rather than convenient.

## Before → after

Measured on the same corpus, over the same reader, with nothing removed.

| Field | P before | P after | R before | R after |
|---|---|---|---|---|
| name | 1.000 | 1.000 | 0.917 | 1.000 |
| email | 1.000 | 1.000 | 0.957 | 1.000 |
| phone | 0.955 | 1.000 | 0.955 | 1.000 |
| link | 1.000 | 1.000 | 0.833 | 1.000 |
| headline | 0.941 | 1.000 | 0.941 | 1.000 |
| summary | 1.000 | 1.000 | 0.889 | 1.000 |
| skills | 0.935 | 1.000 | 0.991 | 1.000 |
| **experience** | 1.000 | 1.000 | **0.658** | **1.000** |
| experience dates | 0.960 | 1.000 | 0.923 | 1.000 |
| **education** | **0.727** | **1.000** | 0.857 | 1.000 |
| languages | 1.000 | 1.000 | 0.947 | 1.000 |
| certifications | 1.000 | 1.000 | 0.750 | 1.000 |
| projects | 1.000 | 1.000 | 0.667 | 1.000 |
| awards | 1.000 | 1.000 | 0.500 | 1.000 |

| | Before | After |
|---|---|---|
| macro-F1 | 0.923 | 1.000 |
| micro-F1 | 0.890 | 1.000 |
| **Fabrications** (content reported that is not on the document) | **22** | **0** |
| Clean cases | 14 of 25 | 36 of 36 |

The corpus was enlarged after the first round of fixes, from 25 pairs to 32 and
then to 36, specifically to re-expose weakness once the original set was
saturated. Six of the seven cases added in the first enlargement failed on first
run. The "before" column above is the original 25 pairs; the "after" column is
all 36, re-measured at closeout.

Slowest case: 98–223 ms across three closeout runs for the two-format standard
CV, well inside the budget for work that runs in a Celery worker rather than on
a request. (287 ms was the figure on an earlier, colder run; the scores were
identical.)

## Defects found, and what caused them

Ordered by what they cost the candidate.

### 1. A Word CV with its roles in a table lost its entire career

`_from_docx` read all the paragraphs, then appended all the tables. Document
order was destroyed, so employment written in a table landed underneath
whatever heading came last — usually Skills. A marketing manager's two roles
and her MBA were read as skill chips.

Fixed by walking `document.element.body` in order.
Regression test: `LayoutTests.test_a_table_is_read_where_the_document_puts_it`.

### 2. A two-column CV extracted to nothing at all

`pdfminer.extract_text` guesses a reading order and discards the coordinates.
On a sidebar layout it collected every section label at the bottom of the file,
detached from the content it labelled: no headings found, therefore no skills,
no roles and no education. A designer's CV read as an empty document.

Fixed by reading the layout instead of the text — `extract_pages`, lines kept
with their positions, lines sharing a baseline put back on one line, and the
gap between two fragments of a row preserved as whitespace so the label can be
split off its content. A genuinely side-by-side two-column page is detected and
read a column at a time. Any failure falls back to flat extraction.
Regression test: `LayoutTests.test_a_sidebar_label_is_read_before_the_column_beside_it`.

### 3. An unrecognised heading did not end the section above it

Everything under it was read as more of the previous section. "REFERENCE /
Available upon request" became two skills on a candidate's profile; a
publication list became education; a "COURSES" block became a second degree.
This was the largest single source of fabricated content.

Fixed with a heading-shape test for headings the vocabulary does not know,
guarded by a blank line above — without that guard, a CV listing one skill per
line has "SQL" read as a heading. The text is kept under its own key rather
than discarded. The vocabulary also grew: internships, volunteering, academic
appointments, courses, publications, and the singular "Reference".
Regression tests: `SectionBoundaryTests` (4 tests).

### 4. Dates written under the role lost the role

The grouper started a role at a dated line. On the very common Word layout that
puts the role on one line and its dates on the next, the dated line became a
role with no title and the title became a bullet under it — and the candidate
was told they had no work experience on a CV that is two thirds work
experience. Dates written *above* the role failed the same way.

Fixed by joining the pair before grouping.
Regression tests: `ExperienceShapeTests` (2 tests).

### 5. One line was read as one degree

A qualification written over three lines — degree, institution, years — became
three degrees, one called "Tribhuvan University" and one called "2012 - 2014".
All three were then offered for import and counted by the analysis.

Fixed by grouping: a line is a continuation when it supplies something the
entry above is missing.
Regression test: `SectionBoundaryTests.test_an_institution_line_is_not_read_as_a_heading`.

### 6. An ORCID was read as a telephone number

Sixteen digits with separators matches any phone pattern loose enough for the
formats this launches into, so an academic's ORCID was shown to employers as
their phone number. Links and addresses are now masked before phone matching,
which removes DOIs and ISBNs at the same time.
Regression test: `ContactShapeTests.test_an_orcid_is_not_a_telephone_number`.

### 7. A table row's cells were joined with whitespace

"Primary School Teacher  Rato Bangala School" reached a profile as a job title
with no employer, because nothing in the line said where one cell ended. Cells
now join with a separator every splitter downstream already understands.

### 8. A name written as a form field was not read at all

The guard that stops "Name - sunita gurung" becoming a *job title* also stopped
it becoming a name, so a whole class of first CV imported with no name on it.

### 9. Every fact was offered twice

`build_from_parse` superseded "open proposals from a different parse of this
upload" — a set that is always empty, because a parse is one-to-one on its
upload and re-reading updates that row in place. Re-reading a file therefore
left both generations on screen. Separately, a second CV re-offered everything
the first one had already offered. A candidate with two CVs on file opened an
import list that was two near-identical copies of the same twenty decisions,
and accepting one copy left the other sitting there.

Fixed by superseding on the upload, and by identity-matching offers across
parses. Live data: 48 open proposals → 24, zero duplicates.
Regression tests: `ProposalTests` (3 tests).

### 10. An empty document was called a photograph

A file with a text layer and almost nothing in it was told it was a scan, which
sends somebody to find a scanner they never used. Scans are now identified by
positive evidence — no letters at all, or scanner furniture in the text layer —
and an empty document gets its own message.

### 11. Every student was told their CV had a critical defect

`no_experience` fired CRITICAL — "There is no work history, so there is nothing
to assess" — for every student, graduate and school leaver on the platform. It
is wrong as a fact (a CV with a degree, four projects and a skills list has a
great deal to assess) and wrong as advice, because the only way to act on "add
your roles" when you have never had a job is to invent one.

Now: a CV carrying the sections an early-career CV is made of gets advice about
those sections at IMPROVE severity; a CV with nothing on it at all keeps the
critical finding the severity was written for.
Regression tests: `test_a_student_cv_is_not_told_its_missing_jobs_are_critical`,
`test_a_cv_with_nothing_on_it_at_all_is_still_critical`.

### 12. The date check charged points for never having had a job

It read only the experience section, so a student CV — dated education, no
employment — was warned its dates could not be checked and lost
machine-readability points for it. Marks deducted for having no job, in the one
part of the report that is meant to be purely about the file. It now reads
every dated entry, which is also what an applicant tracking system does.

### 13. The guided builder could not be finished honestly

Five required steps, the third of which was "Where you've worked". A student, a
graduate or a school leaver could not reach the end without inventing
employment, and the counter sat at "4 of 5" at them until they did. There was
also no step for projects — the section that matters most to exactly those
people — and the section is hidden by default.

Now: optional steps can be set aside in the candidate's own words ("I haven't
worked yet"), the counter counts what applies to them, a projects step exists,
and opening a step reveals its section. Nothing is written to the CV either
way — recording "no employment" as an empty experience section would be the
platform putting a claim on their document.
Regression tests: `GuidedAndAssistantTests` (4 tests).

### 14. Nothing checked that the AI quoted the CV

The prompt asks every finding to quote the words on the page, and forbids
inventing experience. Nothing verified it. A model that writes a plausible
quote instead of a real one produces "your CV says X" about words the candidate
never wrote — they read it, do not recognise their own document, and stop
believing the rest of the report, including the measured half that was right.

Every AI finding's evidence is now checked against the CV text. A finding whose
quote is not there is dropped; the measured report is untouched. Matching is
normalised, not exact, because a model drops bullet markers and trailing full
stops as it quotes.
Regression tests: `AiEnrichmentTests` (3 tests).

## Reaching the people who already uploaded

`ENGINE_VERSION` moved 1.0 → 1.1. Without a re-read, every one of the fixes
above reaches only new uploads: an existing candidate keeps the structure the
old engine found, the analysis keeps scoring it, and the import list keeps
offering whatever it mis-read.

```bash
python manage.py reparse_resume_uploads --dry-run
python manage.py reparse_resume_uploads
```

On the demo dataset: 29 re-parsed, 2 failed (both genuinely corrupt PDFs, which
is the correct answer for those files).

## Genuine limitations

Stated because pretending otherwise is how a benchmark becomes decoration.

- **A scanned CV cannot be read.** There is no OCR, by choice. The product says
  so clearly and offers to rebuild the CV instead, which is the honest answer:
  no applicant tracking system will read that file either.
- **Column detection is a heuristic.** A page whose two columns interleave
  without a clean vertical gutter is read row-major, which is right for sidebar
  layouts and wrong for a true two-column page with a narrow gutter. The
  fallback to flat extraction bounds the damage but does not remove it.
- **Certifications, projects and awards have no structured field.** They are
  kept as section text and are measured on recall of that text. Nothing yet
  turns them into rows a candidate can import or a matcher can read.
- **An employer written above its dated role line** is extracted as a role
  without its employer. The pairing is genuinely ambiguous in that layout and
  the benchmark scores the role, not the pairing.
- **The written review times out about a third of the time on the local
  provider.** `resume.analysis`: 15 ok, 7 timeout against `qwen2.5:7b-instruct`
  on CPU. This is hardware, not a defect in the pipeline — the deterministic
  report is complete and unaffected, and the screen says so — but it is what a
  developer on this machine will see.

## Review and management screens

Found by using the screens rather than by reading them.

### 15. Twenty-four import decisions in one flat list

A CV with a normal skills section produced twenty-odd rows, each labelled with
its own kind — "Skill", "Skill", "Skill" — under a single "Add all" that
accepted every one of them at once. Neither end of that is a decision anybody
makes.

`accept_all` has taken a `kind` argument since it was written and nothing ever
passed one, so the per-kind bulk action existed and was unreachable. The list
is now grouped, each group has a bulk action that means something ("Add all 8
skills"), and each row says which file it came from — with two CVs on file the
same fact can be offered from either, and "why am I being asked this" now has
an answer on the row. Live: 4 groups instead of 24 undifferentiated rows.

### 16. Removing an uploaded CV took one click and asked nothing

It deletes the file the candidate gave us and the reading of it, and there is
no undo on that screen the way there is in the editor. It now routes through
the product's own confirmation dialog, with a sentence that answers the
question somebody actually has — not "are you sure" but "what else goes with
it". The answer is nothing: a CV built from the file keeps its own history, and
so does every application that used one. `ResumeDocument.upload` is `SET_NULL`,
so that sentence is true by construction rather than by hope.

### 17. The dashboard promised twelve questions

The guided route has six. The number is now read from `GUIDED_STEPS` rather
than written into the copy — hardcoding it is what made it wrong, and a promise
about how long something takes is the one line a first-time user checks against
reality within a minute of believing it.

## Screens, measured

Django test client, warm, six samples per screen, against the demo dataset
(a candidate with 16 CVs, 5 uploads, 4 versions — heavier than a real one).

| Screen | p50 | p95 | Queries |
|---|---|---|---|
| Upload + import list | 45 ms | 370 ms (cold) | 17 |
| Your CVs | 41 ms | 49 ms | 16 |
| Guided builder | 84 ms | 105 ms | 42 |
| Editor | 83 ms | 95 ms | 41 |
| Assistant | 83 ms | 137 ms | 42 |
| Analysis report | 59 ms | 73 ms | 26 |
| Template gallery | 42 ms | 46 ms | 15 |
| Export history | 39 ms | 41 ms | 14 |

All inside the SRS's 300 ms page target. The three builder screens are the
heaviest because each renders the editor, the live preview, the version
history, the export list and the open suggestions in one response.

Rendering one CV to PDF: 10–17 ms per template after font warm-up.

## Templates, measured

The claim to test is that the ten templates are genuinely different documents
rather than one document in ten accent colours. Rendering the same version
through each and comparing where the glyphs land:

| Template | Layout | Columns | Left edges |
|---|---|---|---|
| atlas | sidebar | 2 | 40, 51, 235, 520 |
| atlas-warm | portfolio | 1 | 48, 354, 523 |
| circuit | technical | 1 | 45, 526 |
| lyceum | academic | 2 | 57, 244, 249, 518 |
| meridian | classic | 1 | 51, 524 |
| meridian-compact | compact | 1 | 43, 533 |
| plainspoke | minimal | 1 | 62, 509 |
| prism | creative | 2 | 34, 45, 241, 526 |
| regent | executive | 2 | 62, 235, 249, 513 |
| vertex | modern | 1 | 57, 515 |

Ten distinct geometries out of ten: four genuinely two-column, six single
column with different measures and margins. Not accent-only variation.

Preview and export cannot drift: `presentation.paper` and both exporters read
the same `rendering.layout.build` and the same `rendering.theme.for_template`.
The fidelity is structural rather than maintained.

## Empty states

Walked as a candidate with no CV, no uploads and no profile:

| Screen | What it says |
|---|---|
| Overview | A numbered path, one next action, and two alternatives |
| Upload | "Nothing uploaded yet" |
| Your CVs | "No CVs yet. Start one below, or upload the one you have" |
| Analysis | "No CVs yet. Start one and analysis will have something to read" |
| Exports | "Nothing exported yet. Open a CV and use Download PDF" |
| Guided builder | Redirects to the hub rather than erroring on a missing document |

No horizontal overflow on any CV screen at 375 px.

### 18. A responsibility nobody described passed the grounding guard

Found by running a real turn against the real model and reading what came back
— which is the only way this kind of gap shows up, because every sentence in
the output is plausible.

The assistant's grounding test counted content-word overlap. A bullet's claim
lives in what it says the person *did*; everything else is the setting, and the
setting is exactly what a model borrows from the input. Against

> "I worked as a teller at Nabil Bank for two years. I handled cash, opened
> accounts and helped customers with their queries."

the bullet **"Trained two new tellers on bank operations"** shares "two",
"tellers" and "bank" — enough overlap to pass — while the one word that makes
it a claim, "trained", appears nowhere. A responsibility invented for somebody
and written onto their CV is precisely what the guard exists to stop.

The leading verb now has to be a word the candidate used. Deliberately narrow:
it refuses an invented action, and does not try to refuse an invented object,
which needs more than word overlap to see.

Adversarial set, before → after: 10 of 11 correct → 11 of 11.

| Bullet | Should be | Before | After |
|---|---|---|---|
| Opened accounts for customers. | grounded | ✓ | ✓ |
| Handled cash at Nabil Bank. | grounded | ✓ | ✓ |
| Worked as a teller… for two years. | grounded | ✓ | ✓ |
| Rebuilt the settlement pipeline on Celery. | grounded | ✓ | ✓ |
| **Trained two new tellers on bank operations.** | **invented** | **✗ passed** | **✓ refused** |
| Managed a team of twelve across three branches. | invented | ✓ | ✓ |
| Cut fraud losses by 40%… | invented | ✓ | ✓ |
| Led the migration of the core banking system. | invented | ✓ | ✓ |
| Mentored three junior engineers… | invented | ✓ | ✓ |

Regression tests: `test_an_action_the_candidate_never_described_is_not_proposed`,
`test_a_rephrasing_that_keeps_the_candidates_verb_is_still_proposed`.

## The prompt builder, on the real model

One turn, `qwen2.5:7b-instruct` via Ollama, on the case the brief names:

> "I worked as a teller at Nabil Bank for two years. I handled cash, opened
> accounts and helped customers with their queries."

Produced: `title: Teller`, `org: Nabil Bank`. **No dates.** The invented
"2019 – 2021" the brief warns about was neither proposed by the model nor
needed to be refused — and `_is_grounded("dates", "2019 - 2021", said)` returns
False, so it would have been refused had it been. The draft was byte-identical
afterwards: suggestions are offered, never applied (INV-3).

Latency 79 s on local CPU. Slow, and it is the hardware.

## Authorization, probed rather than assumed

Ten CV-shaped objects belonging to candidate A, fetched as candidate B and
anonymously, plus five malformed ids.

| Probe | Result |
|---|---|
| Open / edit / print another candidate's CV | 302 to their own |
| Command endpoint on another candidate's CV | 404 |
| Export another candidate's CV | 404 |
| Download another candidate's render | 404 |
| Accept another candidate's import proposal | 404 |
| Delete another candidate's upload | 404 |
| `?report=` another candidate's analysis | 404 |
| All of the above, anonymous | 302 to sign-in |
| `not-a-uuid`, all-zero UUID, `0`, `-1`, `99999999` | 404, no traceback |

No disclosure in any response body. Scoping is in the queryset rather than
checked afterwards, with a policy call behind it and a single-use signed token
behind that.

## CV → application, on live data

| Check | Result |
|---|---|
| Applications carrying a frozen resume version | 19 of 19 |
| Pointing at a deleted or redacted version | 0 (`PROTECT`) |
| Submitted versions that have since diverged from the live draft | 5 of 19 |
| What the employer sees | version number + SHA-256 of the exact file sent |

The five divergences are the property working: the draft moves on, the
submitted document does not, and the employer's view names the file by hash so
"the CV I sent you" means one thing months later.

Deleting an upload cannot reach any of it: `ResumeDocument.upload` is
`SET_NULL` and `Application.resume_version` is `PROTECT`.

## Accessibility, audited on the rendered HTML

Nine CV screens, parsed and checked for heading order, accessible names,
labelled fields, image alt text, landmarks, and keyboard-unreachable handlers.

| Screen | Headings | Result |
|---|---|---|
| Upload | 8 | clean |
| Your CVs | 4 | clean |
| Guided builder | 13 | clean |
| Editor | 13 | clean |
| Assistant | 13 | clean |
| Analysis | 8 | clean |
| Templates | 61 | clean |
| Exports | 2 | clean |
| Processing | 2 | clean |

One `h1` per page, no skipped levels, every control named, every field
labelled, `<main>` present everywhere. The focus style is a single
`:focus-visible` rule with a 2px outline, an offset and a contrast ring, sized
for WCAG 2.2 Focus Appearance. HTMX confirmations are announced through an
`aria-live` region.

### 19. Full marks beside an open finding about the same thing

Seen on a real report: **"Evidence and impact 30 / 30"** sat directly above
**"Nothing in 'Backend Engineer at Himalaya Ledger Systems' is measured"**.

Both were true — the score is the whole CV, the finding is one role — and
together they read as a scoring mistake. A candidate who thinks the report
contradicts itself stops trusting the half that was right, which on this page
is the measured half.

The component note now reconciles them in the one place that can: "2 of 3
bullets carry a number — at or above the 35% we look for. The bar is the whole
CV, and "Backend Engineer" still has none — see below." Nothing is added when
every role is measured, or when the CV does not meet the bar in the first place
(the note already says "Aim for 35%", so there is nothing to reconcile).

Regression tests: `ScoreNoteTests` (3 tests).

## Second pass — the sections a first CV is made of

The first pass measured certifications, projects and awards on the raw section
text: whether the reader had *seen* the heading. It had. It could see a
candidate's three certifications, keep them as a blob, and hand the builder
nothing — and the benchmark called that full marks. Scoring the structured
field instead is what exposed the rest of this section.

### 20. Projects, certifications, awards and publications were dropped

`_content_from_parse` carried name, headline, contact, summary, experience,
education, skills and languages. Everything else the parser read went nowhere.

A student uploaded a CV whose projects were the best thing on it, pressed "Edit
as a CV", and opened a document with no projects in it. An academic lost their
publications the same way; a nurse lost their registration; a finance graduate
lost their CFA. These are precisely the sections a CV built on anything other
than employment depends on — which is every student, graduate, career changer
and academic on the platform.

Now: `ParsedResume` carries `certifications`, `projects`, `awards` and
`publications`; `Project` is entry-shaped, because a project has a description
and that description is what those candidates are judged on; and the document
builder creates each section only when the file has one, so an ordinary CV does
not open with an empty Publications heading on it.

`ENGINE_VERSION` 1.1 → 1.2, `STRUCTURED_SCHEMA_VERSION` 1 → 2 (ADR-A27).

Live, after re-parsing: projects on 2 uploads, certifications on 4, awards on 3,
publications on 1 — all previously zero.

### 21. A numbered publication list shredded itself

Academic CVs number their publications and every citation wraps. The
continuation of a citation starts with a capital, so the bullet-continuation
rule read it as a new item: each publication split in two, and the next one's
number glued itself onto the stray half.

> `…in the Koshi basin, part 1. Journal of`
> `Himalayan Hydrology, 11(2), 107-119 2. Adhikari, P. … part 2. Journal of`

Numbering is a list marker exactly as a bullet is. Where a section numbers
itself, the number is now the only thing that starts an item.

One real CV in the demo set: **0 publications → 33 complete citations.**

### 22. Word draws bullets from a style and puts none in the text

`paragraph.text` for a bulleted line is the line with no marker on it, so every
reader downstream asking "is this a bullet?" got no, for a document that is
nothing but bullets. On a Word CV it cost a student their projects: the two
bullets under "Notes API" arrived unmarked and each was read as another
project.

The marker is restored from the paragraph style. That is recovering information
the file has and the text layer drops, not guessing at one.

### 23. Academic heading spellings

"Selected publications", "Grants", "Fellowships", "Journal articles" and the
rest. The unknown-heading guard from the first pass had already stopped them
leaking into the section above — they landed safely under `other:` — but
nothing structured them. Added to the vocabulary.

"Teaching" was deliberately **not** added to the experience vocabulary. On an
academic CV it is usually a list of course names, and reading course names as
employment would be a fabrication. It stays an unrecognised heading, which is
the safe answer.

### Corpus and scoring

Two cases added, both of which failed on first run:

- `academic-numbered-publications` — numbered, wrapping citations; "Grants" as
  an awards heading.
- `projects-and-certificates` (PDF and DOCX) — a student with no employment
  whose CV is projects and certificates.

| | First pass | Second pass |
|---|---|---|
| Cases | 32 | 35 |
| Clean | 32 | 35 |
| macro-F1 | 1.000 | 1.000 |
| Fabrications | 0 | 0 |
| certifications scored on | section text | structured field |
| projects scored on | section text | structured field |
| awards scored on | section text | structured field |

The headline number did not move, and that is the point worth stating plainly:
it did not move because the corpus and the scorer were both made stricter at
the same time as the reader was made better. A benchmark that only ever goes up
is a benchmark that has stopped looking.

### 24. A failed written review cost the candidate their monthly analysis

The report said "Running the analysis again will try for one". Running the
analysis again calls `analyse`, which claims a `resume_analyses` entitlement —
one a month on the free plan. So a timeout on the platform's side spent a
candidate's whole allowance for the month, and what they lost it for was the
half that had already worked: the score and the checks are never produced by a
model and were complete the whole time.

`retry_enrichment` re-opens a failed report and runs only the model's half. No
version is frozen, no allowance is claimed, and it is idempotent by state, so a
double-click cannot duplicate the model's findings. The screen offers it as a
button and says plainly that it is free.

Verified end to end against the real local provider: a report in the failed
state went to `done`, with the document's version count and the account's
allowance both unchanged.

Regression tests: `RetryTheWrittenReviewTests` (7 tests), including that a
second failure leaves the deterministic report intact and that the button and
its "free" wording are on the page.

This does not make the local provider faster. It makes its slowness stop
costing the candidate anything.

### 25. Nothing checked that the file says what the screen says

The preview and both exporters read the same `rendering.layout.build` and the
same theme, so they cannot disagree about which blocks exist or where they go.
That sharing was the whole of the fidelity argument, and it does not cover the
case that matters: a field one of them forgets to draw is content that vanishes
on the file, and nobody finds out until it is in front of an employer.

`PreviewMatchesTheExportTests` reads the words off the preview's own `Document`
and requires every one of them in the exported PDF — for all ten templates —
and in the Word file. Word level rather than pixel level on purpose: a pixel
diff fails on a font hint and says nothing about whether anybody lost a
sentence.

Measured on a CV carrying every section the product can draw: **0 words missing
from any of the ten PDFs, 0 from the DOCX.** A third test names the four
sections added in this pass and requires them in every template's file, because
carrying them into the document and then not drawing them would be the same
loss one step further along.

Live check on the academic CV: 33 publications and the grant render in
Meridian, Lyceum and Atlas — including both two-column layouts — and in Word.

## Third pass — hostile input, scripts, and the layouts under load

### 26. A name in any script but a cased one was never read

"Capitalised" is a property of cased writing systems. Devanagari, Arabic, Thai,
Chinese, Japanese and Korean have no case at all, so `isupper()` is False for
every character in them — and a name test written as "each word is capitalised"
rejects every name written in any of them.

That is not a niche. This reader's own heading vocabulary carries South Asian
spellings with the comment "this launches into that market first", and नवराज
गौतम is an ordinary way to write a name there. Every such candidate imported a
CV with no name on it, and the builder then offered them the name on their
account instead.

An uncased letter is one whose upper and lower case are the same character. A
single token is allowed as a name only when it is written in an uncased script,
because that is how a Chinese, Japanese or Korean name is written and there is
no second token to look for — a lone Latin word is still a heading, not a name.

| Script | Before | After |
|---|---|---|
| Latin | ✓ | ✓ |
| Cyrillic, Greek | ✓ | ✓ |
| Devanagari | ✗ | ✓ |
| Arabic | ✗ | ✓ |
| Thai | ✗ | ✓ |
| Chinese, Japanese, Korean | ✗ | ✓ |

**0 of 9 → 9 of 9**, with the guard that "CURRICULUM VITAE" is still not a name
kept and tested. Corpus case `devanagari-name` added — DOCX only, and the
reason is the corpus rather than the product: reportlab's built-in fonts carry
no Devanagari glyphs, so rendering it to PDF here would measure the benchmark's
own typesetting rather than the reader.

### 27. An empty Word file was called a photograph

`parse` did not know what kind of file it was reading, so the scan heuristic
fired on an empty `.docx`: the author was told their file was "a picture of a
CV rather than a document" and advised to export it from Word as a PDF instead
of scanning it — advice about a machine they never touched, for a document they
wrote in Word. Only a PDF can be a photograph.

### 28. The benchmark reporter died on the corpus it reports

A Windows console is cp1252 and raises on the first Devanagari character. The
corpus deliberately contains a name in that script, so the reporter crashed
printing the one case it most needed to report. Output is now encoded with
replacement.

### Hostile input

Sixteen files that are not well-behaved CVs, each one something a real person
uploads. The bar is not that they parse — several cannot — but that each fails
as a result the product can explain, never as an unhandled exception in a
worker.

| Input | Outcome |
|---|---|
| Zero bytes (PDF and DOCX) | Refused, explained |
| Truncated PDF, header only, one null byte | Refused, explained |
| HTML renamed `.pdf`, ZIP renamed `.docx` | Refused, explained |
| Password-protected PDF | Refused, with the actionable message |
| Empty DOCX, DOCX of blank lines | "There is almost nothing in this file" |
| 40 pages, 5,000-character line | Read, bounded at 30 pages |
| Control characters, emoji, right-to-left text | Read |

**0 crashes.**

### Templates under load

Nine shapes of CV against all ten templates, PDF and DOCX — 90 combinations
each way.

| Content | Result |
|---|---|
| 56-character name, 86-character job title | Renders; name and title present in all ten |
| 78-character employer | Renders in all ten |
| Three 250-word bullets | Renders in all ten |
| Forty skills | 2 pages in the roomier templates, 1 in the compact ones |
| Twelve roles | 2 pages, and "Engineer 12" is on the file |
| No experience, no skills, nothing at all | Renders in all ten |

**0 crashes, 0 content dropped.** Too much content becomes more pages rather
than one overfull page with the end of the CV drawn off the paper.
Regression tests: `TemplatesUnderHardContentTests` (3 tests, 90 subtests).

### Keyboard, exercised rather than read

The upload screen offers 102 keyboard stops, **none** with a positive
`tabindex` — DOM order is the tab order — and **none** without an accessible
name. The first stop is "Skip to content".

The confirmation dialog, driven from the keyboard:

| Property | Result |
|---|---|
| `role="alertdialog"`, `aria-modal`, labelled and described | ✓ |
| Focus moves into the dialog on open | ✓, onto **Cancel** — the safe choice for a destructive action |
| Escape closes it | ✓ |
| Focus returns to the control that opened it | ✓ |
| Tab is trapped inside | ✓, wraps back to Cancel |

## Why there is still no OCR, on purpose

`pytesseract`, `easyocr` and PyMuPDF are all importable here, so adding OCR
would be a morning's work. It would also be wrong.

The product's answer to a scanned CV is "this file is a picture, no applicant
tracking system can read it either, rebuild it here or export it from Word".
That advice is correct, and it is the advice that actually helps the candidate:
the employer's software will reject the scan whatever we manage to read out of
it. OCR would let the platform quietly succeed where the employer will fail,
and send somebody into an application with a file that cannot be parsed at the
other end.

This is a decision, not a gap. It is listed here so that nobody re-opens it as
an oversight.

### Measured (2026-09-26)

The decision was re-examined with numbers: Tesseract 5.5.0 (Apache 2.0, local),
`scripts/ocr_benchmark.py`, every PDF case of this corpus scored by this
corpus's own scorer. OCR runs only where production finds no text layer.

| Input | Macro-F1 | Fabrications | Median time | Notes |
|---|---|---|---|---|
| Text PDF (production today) | 1.000 | 0 | 0.01 s | OCR triggered on 0 of 28 — detection is exact |
| Clean scan, 200 dpi | 0.962 | **15** | 0.45 s | 16 of 28 cases clean; the table layout 0.571 |
| Skewed 3° | 0.947 | **33** | 0.46 s | |
| Low quality (110 dpi, JPEG, noise) | 0.420 | **67** | 2.2 s | every field ≈ 0; one page exceeded 120 s (unbounded, it ran 400+ CPU seconds) |

Nepali (Devanagari) text: character similarity 0.945 with the `nep` language
data, 0.158 with `eng` — OCR would need the language chosen correctly. A PDF
whose header is an image and body is text never triggers OCR (the header is
lost either way). Memory stayed under 110 MB.

**Outcome: not integrated.** The reasoning above stands, and the numbers add a
second one: OCR introduces fabricated values even on clean scans (15 across 28
cases), where this reader's standard is zero, and a poor scan produces garbage
at unbounded CPU cost. The honest "this is a picture" message remains the
better product.

## Parser changes, 2026-09-25/26 (engine 1.3 → 1.4)

- A Projects section written only as bullets kept every project (it was dropped).
- A bulleted list in a Word file no longer turns "• Python" into a section
  heading — bulleted skill lists were being lost, and the product's own DOCX
  exports read back with no skills.
- The place on the contact line ("Pokhara, Nepal", beside the email or phone)
  is read as the location and carried into the CV on import; it was dropped,
  and the assistant then asked for it. Only the contact line is read; nothing
  is inferred.

After deploying, `python manage.py reparse_resume_uploads` re-reads uploads
parsed by an older engine.

## Closeout verification

A focused re-check of this record against the code as it now stands, because the
report was written and the code then changed again. Nothing below is carried
over from an earlier pass: every figure in this section was measured after the
last edit to `extract.py` and `corpus.py`.

### What the post-report edits touched

There is no diff to read. This tree is not under version control of its own —
`git rev-parse --show-toplevel` resolves to the user's home directory and
`git ls-files` for this project returns nothing — so there is no commit boundary
to compare against and no way to reconstruct what the files said before. What
can be established is which files were edited last, and what kind of code they
are:

| File | Last edited | What it is |
|---|---|---|
| `apps/resumes/services/parse.py` | 13:25 | **product** — the ingest path |
| `apps/resumes/parsing/extract.py` | 13:27 | **product** — the parser |
| `apps/resumes/tests/test_parsing.py` | 13:27 | tests |
| `apps/resumes/benchmark/corpus.py` | 13:29 | **benchmark** — the corpus |
| `apps/resumes/management/commands/resume_benchmark.py` | 13:29 | **benchmark** — the runner |
| `apps/resumes/tests/test_rendering.py` | 13:32 | tests |

So the answer is both: product code and benchmark code, plus the tests for each.
That is the case where a benchmark number is least trustworthy on its own — the
thing measured and the thing measuring it both moved — which is why everything
was re-run rather than re-read.

This file itself was last written at 13:55, after those edits, and had not been
reconciled with them: it described a corpus of "thirty-two pairs across 26 CVs"
in one place and "36 pairs, 30 CVs" in another, against an actual 36 pairs
across 32 CVs. Both are corrected above. A stale count is not a wrong
measurement, but it is exactly the kind of drift that makes a record stop being
evidence.

### Re-run after those edits

| Command | Result |
|---|---|
| `python manage.py test` | **2,849 tests, green** — the same count this file claimed, so that figure does correspond to the final code state |
| `python manage.py resume_benchmark`, three times | **36 of 36 clean**, macro-F1 1.000, micro-F1 1.000, 0 fabrications, every run |
| `python manage.py test apps.resumes` | 507 tests, green |
| `python manage.py test` again, after the closeout fixes and their 15 new tests | **2,864 tests, green** |
| the CV feature areas on their own — upload, ingestion, documents, versioning, deletion, templates, rendering, exports, guided building, analysis, and the applications that carry a frozen CV version | 531 tests, green |
| `ruff check .` | 14 errors — **3 of them in `apps/resumes`** |
| `ruff format --check .` | 19 files would be reformatted — **6 of them in `apps/resumes`**, one of them `resume_benchmark.py`, edited in the last round |
| `mypy` | **28 errors in 4 files** — 24 in `rendering/docx.py`, 2 in `benchmark/scoring.py`, one each in `selectors.py` and `services/analysis.py` |

The suite and the benchmark were clean. The two checks the README names alongside
them were not, and one of the files failing them was benchmark code from the
final round. Those are now fixed: `ruff check` and `ruff format --check` pass
across `apps/resumes`, and `mypy` passes across all 348 files.

`scoring.py` was the one worth reading rather than silencing. `_set_score` was
annotated `list[str]`, and education is scored on `Study` rows — the annotation
was wrong about what the function does, not merely absent.

### Defects found at closeout

Four, all reproducible, all fixed, all with a regression test.

#### 19. A second employer took over a role that had no employer

The worst of the four. On a blank CV, described in two turns:

> "I cut the nightly settlement run from 4 hours to 35 minutes, and moved 12
> batch jobs onto Celery."
>
> "Before that I was a teller at Nabil Bank. I opened accounts for customers."

The result was **one** role — Teller, Nabil Bank — with the settlement work
listed underneath it. Somebody's achievements, attributed to an employer they
never said they did them for, on the document employers check.

`_is_a_different_job` only noticed a second employer when the role it was aiming
at **already named one**. A role with bullets and no employer is not an edge
case here: it is what this builder produces whenever the model's guess at the
employer is refused as ungrounded, which is the common path — the first turn
above names no employer at all, so "Nexa Payments" was dropped and the role was
created by the accept with an empty `org`.

Fixed by treating a named employer and bullets that predate it as two jobs. When
they are in fact one, the cost is a second role the candidate merges themselves
— a tidying job rather than a false claim, which is the right way round.

Regression tests: `WorkAlreadyOnTheCvKeepsItsOwnEmployerTests` (3 tests),
including one that the guard must not split a job whose employer *is* already on
the role, and one that an empty reserved role still receives its identity.

#### 20. A figure the candidate took back stayed one click from the CV

> "I cut the nightly settlement run from 4 hours to 35 minutes."
>
> "Sorry, that is wrong — it was 3 hours, not 4."

Both bullets then sat in the suggestion list, offered, either one acceptable.
Grounding cannot see this by construction: it asks whether a figure appears
anywhere the candidate has written, and the 4 does — one turn ago. `_supersede`
existed and was wired only to the role's own fields, so a corrected job title
retired the old one and a corrected *figure* did not.

Fixed by reading the retraction: a denial followed directly by a number ("not
4", "was not the 40 I said", "instead of 12") retires every waiting proposal
carrying that figure, and refuses it in anything the turn proposes. Deliberately
narrow — "not counting the 6 months of internship" takes nothing back, and there
is a test that says so.

Regression tests: `TheConversationCorrectsItselfTests` (4 tests).

#### 21. The save-a-version row pushed three builder screens sideways on a phone

Measured in a browser at 375 px, on the rendered pages: `documentElement.scrollWidth`
409 against `clientWidth` 375 on the **editor**, the **guided builder** and the
**assistant**. 34 px of horizontal overflow on the three screens where a
candidate does the actual work.

The cause is one row in `_editor_status.html`: Undo, a `w-44` (176 px) change
note and "Save a version" in a `flex` with no `flex-wrap`, coming to 368 px
inside a 330 px card. The previous pass recorded "no horizontal overflow on any
CV screen at 375 px"; that claim was wrong for these three, and the partial they
share is why all three failed together.

Fixed by letting the row wrap and the note shrink (`flex-wrap`, `w-full
min-w-0 sm:w-44`). Re-measured: 375 / 375 on all three, and `window.innerWidth`
back to 375 — it had been reporting 409, which was the browser zooming out to
fit the overflow.

Regression test: `TheBuilderFitsAPhoneTests`. It holds the markup that made it
fit rather than the measurement, which cannot be taken from a Django test; the
measurement is in this record.

#### 22. Every confirmation on a CV screen was silent

"Made primary", "Version saved", "Deleted" — each rendered as a line of green
text inside an htmx swap, with no `role`, no live region, and nothing announced.
`CareerAI.announce` and the polite `#announcer` region in `base.html` were both
already written, and **had no caller anywhere in the product**.

Fixed by marking the confirmation `data-announce` and copying its text into that
one persistent region after every swap and on load. Not a live region of its
own: a region inserted together with its text is announced unreliably. The error
alert above it is left alone — `role="alert"` is read when it appears, and
announcing it as well would say the same sentence twice.

Verified in a browser: dispatching `htmx:afterSwap` over a swapped-in
confirmation leaves "Version saved" in `#announcer`. That the region receives the
text is verified; that a screen reader speaks it is not — see the limitations.

Regression tests: `ConfirmationsAreAnnouncedTests` (2 tests), plus
`EveryCvScreenTests.test_there_is_one_live_region_for_the_whole_page`.

### The prompt builder, re-checked through candidate workflows

Five workflows, end to end through `assistant_service.respond` and the accept
path, against the fake adapter so the model's output is the variable under
control rather than the weather.

| Workflow | Result |
|---|---|
| Create a CV from a prompt | Bullets are offered, a role is reserved, and the first accept creates it. Two jobs described in one conversation now become two roles — **defect 19**, fixed |
| Modify an existing CV | Accepting adds to the role the conversation is about; accepting two keeps both |
| Correct a previous instruction | A corrected title already retired the old one; a corrected **figure** did not — **defect 20**, fixed |
| Preserve unrelated content | Summary, skills, education, languages, the other role and every contact field come back byte-identical after accepting every suggestion. New regression test, because the accept path rewrites the whole document |
| Handle missing facts | Held. Told "I answered the phones and filed paperwork for a couple of years", the model offered the employer "Himalayan Traders", the dates "2020 – 2022", the title "Office Administrator" and the bullet "Supervised a team of four administrators": **all four refused**, one bullet offered — the one the candidate actually described |

### Accessibility, as far as this environment reaches

Eleven CV screens, parsed with `lxml` and checked for duplicate ids, heading
order, an accessible name on every focusable control, a label on every field,
alt text on every image, and handlers on things the keyboard cannot reach. All
eleven clean, and the audit is now a test (`EveryCvScreenTests`) rather than a
one-off script, so a template that drops a label fails rather than passes
quietly.

The confirmation dialog's semantics are asserted too: one `role="alertdialog"`,
`aria-modal="true"`, and both `aria-labelledby` and `aria-describedby` pointing
at elements that exist on the page.

**Still no screen-reader session.** That has not changed and is not claimed. The
markup is right, the live region now receives the text, and nobody has listened
to these pages with assistive technology.

## Where it stands

| | Value |
|---|---|
| Full test suite | **2,864 tests, green** |
| Corpus | 36 (document, format) pairs, 32 CVs |
| Clean cases | **36 of 36**, on three consecutive runs |
| macro-F1 / micro-F1 | **1.000 / 1.000** |
| Fabrications | **0** |
| `ruff check` / `ruff format --check` on `apps/resumes` | clean |
| `mypy` | clean, 348 files |
| Horizontal overflow at 375 px | 0 of 11 CV screens |
| Accessibility parse of the CV screens | 11 of 11 clean |
| Hostile inputs surviving without a crash | 16 of 16 |
| Template × content combinations rendering | 90 of 90, PDF and DOCX |
| Writing systems whose names are read | 9 of 9 |
| Defects found and fixed | 22, across four passes |

### What the benchmark result does and does not mean

36 of 36 and zero fabrications are true **of this corpus**: 32 CVs written for
this repository, rendered by `render()` to PDF and DOCX and read back through
the production reader. They are not a statement about CVs in general.

Three things follow from that, and they are worth saying plainly because a
saturated benchmark is easy to mistake for a solved problem:

* the corpus is generated, so it contains the layouts somebody thought to write
  down. A real upload can be stranger than anything in it;
* it has been saturated twice and enlarged twice in response, and both times the
  new cases failed on first run. A score of 1.000 is better read as "this set no
  longer discriminates" than as "extraction is finished";
* nothing here measures the one input the product genuinely cannot read — a
  scan — because that is a deliberate refusal rather than a failure.

### Scores, and what caps each one

Evidence-based and deliberately not held at the previous 9.7. The closeout found
four reproducible defects in areas the last pass called done, and a score that
does not move when that happens is not a measurement.

| Area | Score | What it rests on | What caps it |
|---|---|---|---|
| Extraction and parsing | 9.6 | 36/36, 0 fabrications, three runs; 16/16 hostile inputs; product and benchmark both re-run after the final edits | Column detection is a heuristic; the corpus is generated and has saturated twice |
| Prompt builder | **9.2** | Five workflows end to end; nothing invented across the adversarial set; INV-3 structural | Defect 19 was a misattribution of real work to the wrong employer and survived the last pass; grounding is still word-level; the new retraction test is figures only |
| Analysis and review | 9.5 | Deterministic half complete and never model-dependent; retry is free | The written half depends on a provider that times out about a third of the time locally |
| Templates, rendering, exports | 9.6 | 90/90 template × content, PDF and DOCX, 0 content dropped | No print-fidelity check beyond page counts |
| Responsive UX | **9.3** | 0 of 11 screens overflow at 375 px, measured in a browser | Defect 21 was a 34 px overflow on the three screens candidates work in, and the previous pass recorded the opposite |
| Accessibility | **8.8** | 11/11 screens clean on a real parse, now a test; dialog semantics asserted; the announce path verified in a browser | No screen-reader session. Nothing replaces one |
| Authorization and data handling | 9.7 | 10 CV-shaped objects probed cross-account plus 5 malformed ids; scoping in the queryset | — |
| **Overall** | **9.4** | The suite, the benchmark, the gates and the workflows above, all after the final edits | Accessibility is unlistened-to; two of the four closeout defects were in areas previously reported complete |

### A note on the timings

Re-measured at the end, the CV screens read p50 119–247 ms against p50 36–84 ms
earlier in the same session. Nothing regressed: the **query counts are
identical** on every screen (17, 16, 42, 41, 42, 26, 15, 14, 16), and building
the preview — the only part these changes touched — costs 0.20 ms.

A trivial `SELECT 1` against the Postgres container on this machine costs
1.87 ms. Forty-two queries is therefore about 79 ms of round-trip before
anything is computed, and the container was restarted mid-session. The
measurement is of the database link, not of the code.

It does point at something real, though, and it is worth writing down rather
than fixing badly: **the builder screens issue 42 queries**, because each
renders the editor, the live preview, the version history, the export list and
the open suggestions in one response. That is what makes them latency-sensitive
on a deployment with a slow database link. They are inside the 300 ms target
either way, and the fix is a batching pass rather than anything this loop
should attempt on the way past.

### Genuine limitations

1. **No OCR**, by decision — see above. A scan cannot be read, the product says
   so plainly, and the advice it gives instead is the advice that helps.
2. **Column detection is a heuristic.** A page whose two columns interleave
   without a clean vertical gutter is read row-major. The fallback to flat
   extraction bounds the damage; it does not remove it.
3. **No screen-reader session.** The markup is audited on every CV screen and
   the audit is a test; keyboard operation was driven in an earlier pass — tab
   order, focus movement, the modal's trap, Escape, focus return — and the live
   region is verified here to receive the text a confirmation carries. Nobody has
   listened to these pages with assistive technology, and none of the above is
   a substitute for that.
4. **Grounding is word-level.** It refuses an action the candidate never
   described; it cannot refuse an invented *object* attached to a verb they did
   use, which needs more than word overlap to see.
5. **A retraction is recognised only when it carries a figure.** "Not 4", "was
   not the 40 I said", "instead of 12" are caught. "I didn't actually run that
   project" is not: there is no number to retire the proposal by, and a general
   contradiction detector is a different piece of work from a narrow, testable
   pattern.
6. **The "different job" guard can split one job in two.** A candidate who
   describes work, accepts the bullets, and only then names the employer gets a
   second role carrying the employer, and merges the two by hand. That is the
   deliberate direction of the trade in defect 19: a tidying job rather than a
   false claim.
7. **The local provider is slow.** 79 s for one assistant turn and roughly a
   third of written reviews timing out on `qwen2.5:7b-instruct` on CPU. It no
   longer costs the candidate an analysis, and it is still slow.
8. **The benchmark corpus is generated**, and has saturated twice. See the scope
   note above: 36 of 36 is a statement about these 32 CVs, not about CVs.
9. **`ruff check` and `ruff format --check` still fail outside `apps/resumes`** —
   11 lint errors and 13 unformatted files in other sections of the product.
   They were left alone deliberately: this pass was the CV section, and the CV
   section is clean.
