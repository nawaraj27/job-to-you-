# CareerAI

A career platform: analyse a CV against real ATS rules, rebuild it, match it to
roles, and rehearse the interview. Server-rendered Django, no SPA.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Rendering | Django templates (SSR) | Every component is already a Django partial — nothing to port later |
| Styling | Tailwind CSS v4, CSS-first config | Design tokens live in one CSS file, no `tailwind.config.js` |
| Interactivity | Alpine.js | Menus, disclosures, theme switch — declared in markup |
| Partial updates | HTMX | Server-rendered fragments instead of a client-side router |
| Icons | Inline SVG via `{% icon %}` | Inherits `currentColor`, no extra request, no flash |
| Auth | Custom `accounts.User` | Email login and candidate/employer roles from day one |

No React, Vue, or build-time JS framework.

## Running it

Postgres and Redis run in Docker; Django runs on the host so the reload loop
stays fast.

```bash
docker compose up -d
pip install -r requirements-dev.txt
npm install && npm run build
python manage.py migrate
python manage.py seed_demo          # a platform with people on it — see below
python manage.py runserver 127.0.0.1:8137
```

### The demo dataset

`seed_demo` fills an empty database with a market to look at: eight companies,
twenty-five live postings across eight categories and five seniority levels,
ten recruiters, six candidates with real CVs, applications spread through the
pipeline, and the analytics and notifications those produce.

```bash
python manage.py seed_demo            # idempotent; safe to re-run
python manage.py seed_demo --reset    # delete the previous demo rows first
```

Everything is written through the domain services, so the events, the audit
trail, the application counters and the pipeline invariants are all correct —
a database that looks populated and behaves wrongly is worse than an empty one.

Every seeded account is on `@example.com`, the reserved documentation domain
that can never receive mail, and they share one password the command prints
when it finishes:

| Who | Sign in as |
|---|---|
| Candidate | `priya.sharma@example.com` |
| Recruiter | `anita.gurung@example.com` |

The command refuses to run with `DEBUG` off. It also does two things a laptop
otherwise never does: it drains the outbox and runs the notification fan-out,
and it backfills the analytics rollups — both are long-running processes in a
real deployment, and without them a seeded recruiter opens Analytics to "come
back tomorrow" and a seeded candidate opens an empty bell.

Two more terminals during development — Tailwind in watch mode, and the outbox
worker that delivers domain events:

```bash
npm run css:dev
python manage.py outbox_worker
```

> Port 8137 rather than 8000, and Postgres on 5433 rather than 5432: both
> defaults are already taken on the current dev machine. A natively installed
> PostgreSQL on 5432 silently shadows the container, so the compose stack
> publishes 5433 and `config/settings/dev.py` matches.

Background work runs on Celery. Six queues are isolated so a slow AI call or a
burst of PDF renders cannot starve ordinary work:

```bash
celery -A config worker --queues=default,files --pool=solo
```

### Checks

```bash
ruff check . && ruff format --check .
mypy
python manage.py test
```

`DJANGO_SETTINGS_MODULE` defaults to `config.settings.dev` for `manage.py`,
`config.settings.prod` for WSGI/ASGI, and the test suite selects
`config.settings.test`. Production refuses to start without `DJANGO_SECRET_KEY`,
`DJANGO_ALLOWED_HOSTS` and `POSTGRES_PASSWORD`.

## Architecture

Three documents govern this codebase and are worth reading before changing it:

| Document | What it is |
|---|---|
| [`docs/architecture/CareerAI-SRS.md`](docs/architecture/CareerAI-SRS.md) | The frozen architecture (v1.1). What the system is |
| [`docs/architecture/ADR.md`](docs/architecture/ADR.md) | 34 decision records. Why it is that way, and what it cost |
| [`docs/DEVELOPER.md`](docs/DEVELOPER.md) | Engineering rules. How to write code here |

## Layout

```
config/           settings, urls, wsgi/asgi
apps/
  core/           navigation, feature flags, marketing content, page views
    templatetags/ {% icon %}, widget helpers
  accounts/       custom user, login, signup
templates/
  base.html       document shell
  layouts/        page.html (inner pages), auth.html (focused forms)
  partials/       header, mobile nav, footer, user menu, theme toggle
  components/     alert, form_field, section_header, score_ring, check_row
  pages/          home, pricing, about, contact, privacy, terms, design_system
static/
  css/src/input.css   design tokens + component layer (the design system)
  js/app.js           theme, Alpine components, HTMX defaults
  vendor/             alpine + htmx, copied from node_modules by `npm run vendor`
```

## Design system

Everything visual is defined in [`static/css/src/input.css`](static/css/src/input.css)
in three tiers:

1. **Primitives** (`@theme`) — brand/neutral/status scales, type scale, radii,
   shadows, motion. Never referenced directly from markup.
2. **Semantic tokens** (`@theme inline` + `:root` / `.dark`) — `surface`,
   `content`, `hairline`, `accent`… These are what components use, and they are
   the reason dark mode needs no `dark:` variant on almost anything.
3. **Components** (`@layer components`) — `.btn`, `.card`, `.input`, `.badge`,
   `.alert`, `.nav-link`, `.menu-panel`…

Browse the whole vocabulary at `/design-system/` (DEBUG only). If a component
isn't on that page, it isn't part of the system yet.

Theme is stored in a `theme` cookie, so Django renders the correct mode on the
first response — no flash, no client-side correction.

### Adding a component

1. Add the class to the `@layer components` block.
2. Add a partial in `templates/components/` if it needs markup.
3. Add it to `templates/pages/design_system.html`.

Class names are scanned from source text — never build one by string
concatenation in a template (`bg-brand-{{ step }}` produces no CSS).

## Navigation and feature flags

Navigation is never hardcoded in templates. It is declared in
[`apps/core/navigation.py`](apps/core/navigation.py) and gated by
[`apps/core/features.py`](apps/core/features.py).

A module that isn't built yet is simply not advertised — no dead links, no
"coming soon". To ship one:

1. Build the app and register its URLs.
2. Flip its flag in `DEFAULT_FEATURES` (or override `CAREERAI_FEATURES` in settings).

Header, mobile drawer, footer, and the homepage CTAs all pick it up. Homepage
CTAs fall back to sign-up while a module is off, so no button is ever inert.

## Content

Marketing copy and pricing live in [`apps/core/content.py`](apps/core/content.py),
not in templates. When these become database models, only that file changes —
the templates already iterate the same shapes.

## Conventions

- Mobile first; every screen is checked at 375px and at desktop width.
- One focus style, defined once, never removed.
- `prefers-reduced-motion` is respected globally.
- Forms render through `components/form_field.html` so labels, hints, and errors
  are identical everywhere.
- Icons come from the `{% icon %}` registry — no inline one-off SVGs in pages.

## Not done yet

- Resume, jobs, interview, career tools, employer portal, and dashboard modules
  (flags are off, and the shell is ready for them).
- Privacy and Terms are drafts and need legal review.
- Pricing amounts in `content.py` are placeholders.
- Inter is referenced first in the font stack but not self-hosted; the system
  stack is used until it is added.
