# CareerAI — design system

Source of truth for the public pages. The code lives in
[`static/css/src/input.css`](../static/css/src/input.css); the live gallery is at
`/design-system/` (DEBUG only). If a rule here and the CSS disagree, the CSS is
wrong — fix it there, not by working around it in a template.

---

## 1. Position

An AI career platform for students and job seekers.

The product is used by someone who is anxious, under time pressure, and has
probably already been rejected without explanation. The design has one job
before it has any other: look like something that will tell them the truth.

**Personality:** trustworthy, professional, human, career-focused, modern but
timeless.

**Reference points:** LinkedIn (institutional), Notion (calm density), Linear
(restraint), broadsheet editorial (hierarchy, photography, measure).

**Explicitly not:** an AI showcase. No robots, no neural imagery, no "powered by
GPT" badge, no glow. The intelligence is a means; the career is the subject.

---

## 2. Rules of the house

These are the decisions that keep the product from drifting back into a
generated-landing-page look. They are not stylistic preferences.

1. **Nothing floats.** One elevated object exists on the site — the product
   window in the hero — and it is cropped by the section edge so it reads as
   the application continuing, not as a card hovering over a picture.
2. **No glass.** No `backdrop-blur`, no translucent panels over photography.
   Frosted surfaces are the single most recognisable generated-page tell, and a
   blurred bar destroys the hero image at exactly the point the reader looks.
3. **Hairlines, not shadows.** Separation comes from a 1px rule and space.
   `--shadow-lg` exists for the one window; nothing else should reach past
   `--shadow-sm`.
4. **Cards only for peers.** A card grid asserts "these items are
   interchangeable". True of the four workspaces. False of a six-step arc, which
   is set as numbered rules instead.
5. **Whitespace is the hierarchy.** One idea per band. `.section` is
   4rem → 6.5rem → 8.5rem of vertical padding by breakpoint.
6. **Photography is content.** Every image gets real `alt` text and a visible
   credit. If a picture is not worth describing it is not worth loading.
7. **Proof must be checkable.** No invented user counts, no logo wall, no
   fabricated testimonials. Everything in the facts strip is verifiable inside
   the product on day one.
8. **Motion is optional.** One 0.45s fade on the auth layout, 150–200ms colour
   transitions on interactive elements, nothing else.
   `prefers-reduced-motion` is honoured globally.

---

## 3. Colour

Primitives are never used directly in markup. Components reference the semantic
tokens, which flip with the theme.

### Brand — institutional navy

Chroma is roughly half what a default Tailwind blue carries. At full saturation
this hue drifts toward indigo, which is the house colour of every AI demo on the
internet; pulled down, it belongs to banks, universities and newspapers.

| Step | oklch | Use |
|---|---|---|
| `brand-50` | `0.968 0.011 250` | accent-soft fill |
| `brand-600` | `0.452 0.095 255` | primary button, focus ring |
| `brand-700` | `0.381 0.085 256` | primary hover, link text |
| `brand-800` | `0.316 0.069 257` | brand mark |
| `brand-950` | `0.194 0.037 259` | — |

### Neutral — warm ink

Hue 60–85, chroma 0.004–0.014. Cool grey plus saturated blue is the default
palette of a generated page; a warm neutral reads like paper and print.

`ink-50` … `ink-950`, with `ink-950` (`0.152 0.008 60`) as the ground for every
photographic band.

### Mark — ember

One warm accent (`ember-400/500/600`), used **only** on rules, numerals and
marks. Never a surface, never a button. It is punctuation, not a second brand.

### Status

`positive` / `caution` / `critical`, muted on purpose — an analysis report that
shouts in red reads as a scam, not a diagnosis.

**Never reference those primitives from markup.** Four semantic triples wrap
them, and components consume only these:

| Token | Use |
|---|---|
| `success` / `warning` / `danger` / `info` | the hue itself — meter fills, borders, rules |
| `…-fg` | text and icons on the page background |
| `…-surface` | the tinted fill behind `-fg` |

The `-fg` step is darker than the base in light mode and *lighter* in dark,
because the step that clears 4.5:1 on white is unreadable on ink. Defining that
once is what removed fourteen `.dark .badge-*` / `.dark .chip-*` overrides that
had to be maintained in parallel. `focus` is a token too, so the focus ring can
differ per theme without any component knowing.

### Semantic tokens

`canvas`, `surface`, `surface-sunken`, `surface-raised`, `hairline`,
`hairline-strong`, `content`, `content-muted`, `content-subtle`, `accent`,
`accent-hover`, `accent-soft`, `accent-content`, `mark`.

### `.on-photo`

Inverts the semantic tokens for a subtree over a photograph, so components keep
using `text-content` / `border-hairline` and simply come out light.

> The rule sets **custom properties only** — it is unlayered, so a bare `color`
> declaration there would outrank every layered `text-*` utility inside it and
> flatten the muted and subtle tiers to solid white. The subtree sets its colour
> with a normal `text-content` utility instead.

### Contrast

Body text is `content-muted` (`ink-600`, ≈ 7:1 on white) or darker. Never
`ink-400` for prose. Over photography, the scrim carries `0.42–0.90` alpha ink
weighted toward the left where the type sits.

---

## 4. Typography

Two voices, and the split *is* the hierarchy — which means size and weight can
do far less shouting.

| Role | Family | Notes |
|---|---|---|
| Statements — headlines, figures, quotes, the wordmark | **Newsreader** (`--font-serif`) | Variable, optical sizing 6–72, weights 400/500/600 |
| Interface — nav, buttons, labels, body, forms | **Inter** (`--font-sans`) | Variable 400–650 |
| Data — IDs, scores | system mono | |

`h1`/`h2` are serif by default in the base layer. `h3`/`h4` stay sans: they sit
inside components next to UI text, and a second voice per card is noise.

### Scale

| Token | Size | Line height | Tracking |
|---|---|---|---|
| `text-display-lg` | 4rem | 1.02 | −0.021em |
| `text-display` | 3rem | 1.08 | −0.018em |
| `text-display-sm` | 2.25rem | 1.14 | −0.014em |
| `text-title` | 2rem | 1.16 | −0.014em |
| `text-lede` | 1.1875rem | 1.62 | — |
| body | 1rem | 1.5–1.7 | — |

Tracking is roughly half the previous values: the old −0.032em was tuned for a
grotesque and smears a text face at display size.

### Interface scale

The display scale above carries the marketing pages. The product runs on five
sizes named by role, not by measurement:

| Token | Size | Role |
|---|---|---|
| `text-body-lg` | 0.9375rem | primary interface text, short in-app paragraphs |
| `text-body` | 0.875rem | interface prose |
| `text-meta` | 0.8125rem | secondary interface text — the workhorse |
| `text-caption` | 0.75rem | timestamps, credits, fine print |
| `.overline` | 0.6875rem | micro label above a value or section |

These replaced 393 arbitrary `text-[0.8125rem]`-style utilities spread across
seven values, two of which duplicated Tailwind's own steps written a second
way. Seven ad-hoc sizes is not a scale; it is a set of decisions nobody can
repeat. Line heights are bound to each size so vertical rhythm cannot drift.

`.overline` is a component rather than a size because 52 sites were hand-writing
the same four utilities (`text-[0.6875rem] font-semibold uppercase
tracking-wider text-content-subtle`) in two slightly different combinations.

**Measure.** `--container-measure: 62ch`, applied with `.measure` or a
`max-w-[Nch]`. Every paragraph is capped at the measure, not at whatever the
grid column happens to be.

---

## 5. Components

Layout — `.container-page` (80rem), `.section`, `.section-tight`, `.measure`

Editorial — `.eyebrow-rule`, `.lede`, `.step-row` + `.step-number`,
`.stat-value`, `.pullquote`, `.photo`, `.photo-scrim`, `.photo-scrim-even`,
`.photo-credit`, `.product-window` + `.product-window-bar`, `.link-editorial`

Interface — `.btn` (`-primary`, `-secondary`, `-ghost`, `-danger`, `-hero`,
`-hero-ghost`; `-sm`, `-lg`, `-icon`, `-block`), `.card`, `.panel`, `.input`,
`.select`, `.textarea`, `.choice`, `.badge`, `.alert`, `.nav-link`,
`.menu-panel`, `.menu-item`

Application — `.app-shell`, `.app-rail`, `.rail-link`, `.app-head`, `.app-title`,
`.stack` + `.stack-head`, `.rows` + `.row`, `.toolbar`, `.search-field`,
`.filter-btn`, `.segmented`, `.tabs` + `.tab`, `.chip` (`-match`, `-gap`,
`-toggle`), `.meter` + `.meter-fill`, `.figure-block` + `.figure-value`,
`.empty`, `.skeleton`, `.dropzone`, `.steps` + `.step-dot`, `.dialog-panel`,
`.paper` (+ `-inner`, `-name`, `-role`, `-h`, `-job`, `-body`, `-side`,
`-accent`, `-bar`), `.sortable`

Templates — `components/photo.html`, `components/report_preview.html`,
`components/score_ring.html`, `components/check_row.html`,
`components/section_header.html`, `components/brand_mark.html`,
`components/avatar.html`, `components/app_head.html`,
`components/empty_state.html`, `components/meter.html`,
`components/status_pill.html`, `components/pagination.html`,
`components/job_row.html`, `components/match_reason.html`,
`components/insight.html`, `components/paper.html`,
`components/template_card.html`

Layouts — `layouts/page.html` (content pages), `layouts/auth.html` (sign in /
up), `layouts/app.html` (signed-in shell with a role-specific rail),
`layouts/builder.html` (editor chrome: no nav, no footer)

`.btn-hero` is white-on-ink rather than brand-on-ink: at hero contrast the navy
stops reading as a button and starts reading as a hole in the image.

### The two halves of the system

Above the fold of this list is the **public site**: editorial, photographic,
generous. Below it is the **product**: denser, quieter, hairline-ruled. They
share every token and neither borrows the other's rhythm — a marketing hero
band on a dashboard is what makes a working screen feel like a brochure, and a
dense data row on a landing page reads as unfinished.

### `.paper`

A CV rendered as an actual document, sized entirely in `cqw` (container-query
width units) so one set of markup is a 160px gallery thumbnail and a full-width
preview at identical proportions (1 : √2). Three layouts — `classic`,
`sidebar`, `modern` — and an accent passed as an inline custom property, since
a document's colour is data and Tailwind cannot generate a class for a value
chosen at render time.

This is what stops the template gallery looking like a row of coloured
rectangles: they are pages, with real names, dates and section headings.

---

## 6. Homepage structure

Read top to bottom the page is an argument, not a feature list.

| Band | Ground | Job |
|---|---|---|
| Hero | photograph + scrim | Promise, with a face. Product window cropped by the fold on desktop, shown in full inside the same band on mobile. |
| Facts | canvas | The first thing after a claim is something checkable. |
| Story | sunken + photograph | Win the argument. A reader who has not accepted the problem reads features as noise. |
| Journey | canvas | The order of the work — five numbered rules, not cards. Every one links to the page that carries its detail. |
| Capabilities | sunken | Four peers, so a grid is honest here. |
| Audiences | canvas + photograph | Candidate and employer, same hire. |
| Close | photograph + scrim | The ask, on the picture the page has argued toward. |

**The homepage routes; it does not explain.** Anything that needs a paragraph to
justify belongs on the page it links to — CV analysis, the builders, templates,
job matching and employer solutions each have one.

**Reserved:** a testimonial band belongs between Capabilities and Audiences. It
stays empty until there are real quotes and real photographs — fabricated social
proof on a trust product is worse than none.

---

## 7. Photography

Direction: candid, documentary, real light. Students, graduates, interviews,
desks, workplaces. No handshake-in-a-glass-tower stock, no composited crowds, no
generated faces.

Records live in `apps/core/content.photos()`; each carries `alt`, `credit`,
`credit_url` and an `object-position` so a face is never cropped out at narrow
widths. Rendered through `components/photo.html`, which emits a five-width
`srcset`, lazy-loads everything except the hero, and sets
`fetchpriority="high"` on the hero alone.

**Before launch:** self-host. Download each file into `static/img/photos/` under
the `local` name in `photos()` and set `SELF_HOSTED_PHOTOS = True`. That removes
a third-party request from the critical path and pins the crop so an image can
never change underneath you.

---

## 8. Accessibility floor

- Contrast ≥ 4.5:1 for all prose, both themes.
- One focus treatment, defined once in the base layer, never removed.
- Touch targets ≥ 44px; `.btn-lg` is 46px.
- Card-wide links use a stretched `<a>` with an `aria-label`; the decorative
  numeral is `aria-hidden`.
- Statistics are marked up as `<dl>`, with the label in `<dt>`.
- `prefers-reduced-motion` collapses all animation and smooth scrolling.
- Verify at 320 / 375 / 768 / 1024 / 1440px, in light and dark. 320 is the one
  that breaks: the header lost 17px to the theme switch there, which is why the
  switch now lives in the drawer below `sm`.
- **Never colour alone.** A matched skill carries a tick, a gap carries a warning
  icon in the warning tone (a dashed edge and a plus until 2026-09-22, UI-01 —
  it read as a broken label), a status pill carries an icon and a word, a meter prints its
  number. Everything in the product must survive greyscale.
- Mobile is redesigned, not folded up: the app rail becomes a scrollable section
  strip, filters move into a bottom sheet, and the job detail page pins its apply
  action to the bottom of the screen.
- **Touch targets grow only where the pointer is coarse.** A 30px control clears
  2.5.8 but is a poor thumb target; `@media (pointer: coarse)` lifts the small
  button, icon button, tab and rail link without costing desktop density.
- **Dialogs manage focus.** `Alpine.data("dialog")` traps Tab in both
  directions, closes on Escape, locks body scroll and hands focus back to the
  control that opened it. A dialog that skips this is a keyboard trap in
  reverse — Tab walks straight out into a page the user cannot see.
- **Focus ring is two rings** — 2px brand plus a 1px canvas halo — so it stays
  visible on tinted surfaces, photographs and dark buttons alike (2.4.11).
- Measured, not assumed. Lowest contrast in the product: **4.77:1** light
  (`content-subtle` at caption and overline sizes), **5.04:1** dark (primary
  button). Status foregrounds run 4.8–6.5:1 on white **and on their own
  `-surface` tint** — the pairing a badge uses. `caution-700` was 4.44:1 on
  its tint until 2026-09-21 (A11Y-01) and was darkened to `oklch(0.52 0.118
  55.9)`; `apps/core/tests/test_css_classes.py::StatusContrastTests` holds
  the line. Measure through a canvas — the
  browser reports `oklch()` strings and a naive parser silently reads them as
  RGB, which produces confident nonsense.

---

## 11. Loading, empty and error states

**Skeletons, not spinners.** A spinner occupies no space, so the content
arriving under it shoves the page — the layout shift is the real cost and it
lands exactly as the reader starts reading. `components/skeleton.html` has four
variants (`rows`, `papers`, `report`, `stats`, `text`), each built to the
metrics of the thing it replaces, staggered 90ms per row, `aria-hidden` with
`aria-busy` on the live region instead.

Wiring is the `.loading-swap` pattern: both states sit in the DOM, HTMX adds
`.htmx-request` to the region, CSS switches which one is shown. No request, no
layout pass. On localhost responses land in ~3ms so the skeleton never appears —
that is correct, and the mechanism is what production latency needs.

**Empty states differ by cause.** An empty search names what was searched for
and offers ways to widen it; an empty saved list explains what will appear there
and offers the action that fills it. Same component, different copy — a shared
"nothing here" is a dead end wearing a component's clothes.

**Error states say what happened, whose fault it is, and what to do.** `404`,
`403` and `500` templates exist; `500.html` is standalone because Django renders
it with an empty context, so a 500 page that extends `base.html` is a 500 page
that throws its own 500. Failed HTMX swaps replace the region with a retry
rather than leaving stale results with no explanation.

---

## 9. Product architecture

The signed-in product lives in four apps, each with a `samples.py` that stands
exactly where its `models.py` will:

| App | Screens |
|---|---|
| `apps.resume` | upload, processing, analysis, builder hub, guided, full-control editor, assistant, template gallery, template detail |
| `apps.jobs` | browse + filters, detail, matched, saved, applications, public company profile |
| `apps.dashboard` | candidate overview (new and active states), profile |
| `apps.employers` | solutions, registration, hiring overview, postings, post a job, applicants, company profile, verification |

**Mock data rule.** No template contains a literal record. Every screen reads
dataclasses whose field names, related objects and computed properties are the
ones the future models will expose — `job.salary_display`, `job.match.missing`,
`company.verification`, `application.status`. Swapping a `samples` call for a
QuerySet is a one-line change in a view.

Two states are switchable while there is no backend: `?as=candidate|employer`
and `?state=new|active` (see `apps/core/demo.py`), surfaced by a DEBUG-only
switch in the corner. Deleting that module and the `{% if debug %}` guard in
`base.html` is the whole removal.

Modules that are declared but not built — mock interviews, cover letters,
career roadmap — stay `False` in `apps/core/features.py`. Navigation drops any
item whose feature is off *or* whose URL does not resolve, so a half-wired
route can never render a dead link.

---

## 10. The logo

The wordmark is not styled text. `components/brand_mark.html` is the only place
the brand is drawn — header, footer, auth screen and gallery all include it.
Point `site.logo` (and optionally `site.logo_dark`) at a static path in
`apps/core/context_processors.py` and a real logo file replaces the glyph
everywhere at once, in both themes, at three sizes.
