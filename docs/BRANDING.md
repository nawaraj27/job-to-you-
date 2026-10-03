# Branding

## The name

The public name is **Kaarya**, tagline "CV, Jobs → Opportunities" (tracker
rows BR-1 to BR-3). Internal identifiers keep the original project name on
purpose: Python packages, the `careerai` database, Docker service names,
migrations, test settings and the webhook headers (see "Kept on purpose").

## Where the name comes from

| What | Where it is set | Read by |
|---|---|---|
| Site name | `SITE_NAME` (env), default `Kaarya` | `{{ site.name }}` in templates; `apps.core.branding.site_name()` in Python (emails, messages, PDF metadata) |
| Tagline | `SITE_TAGLINE` (env), default `CV, Jobs → Opportunities` | `{{ site.tagline }}` (large brand lockup) |
| Sender | `DEFAULT_FROM_EMAIL`, built from `SITE_NAME` | every email ("From: Kaarya <…>") |
| Support email | `SITE_SUPPORT_EMAIL` (env), default `hello@careerai.app` | footer, contact page |

`apps/core/tests/test_branding.py` fails if any template spells the name out
instead of using `{{ site.name }}`.

The two "Apply on …" choice labels used to include the name, which made a
rename produce a migration. They now read "Applied directly" / "Apply
directly" (migrations `applications/0004` and `jobs/0004`), so changing
`SITE_NAME` needs no migration.

## The logo

**Source.** The supplied artwork, kept unaltered as
`static/img/brand/kaarya-logo-source.jpg`. It shows a mark (two crossing
strokes with an arrow) above the word "KARYA" and the tagline.

**The wordmark is text, not the image.** The supplied artwork spells the name
with one A ("KARYA"); the product name is Kaarya. Rather than redraw the
lettering, only the **mark** was taken from the artwork and the name is set in
type beside it from `site.name`, so the logo always spells the configured
name.

| File | What | Use |
|---|---|---|
| `kaarya-mark.webp`, `kaarya-mark.png` | The mark, 383×305, transparent background. Extracted from the source by un-blending the paper background, no redrawing | header, footer, auth, every portal |
| `favicon-32.png` | Mark on a square | browser tab |
| `icon-192.png` | Mark, 192 px | web app icon |
| `apple-touch-icon.png` | Mark, 180 px, padded | iOS home screen |

`templates/components/brand_mark.html` renders the lockup:

- `<picture>` with WebP and PNG sources; width and height set, so no layout
  shift.
- The wordmark `<span class="brand-wordmark">{{ site.name }}</span>`: Inter
  800, uppercase, letter-spaced, `#0f3768` in light, `brand-100` in dark.
- The tagline only at the large size (auth screens).
- Alt text: the mark's `alt` is empty when the name is printed beside it (the
  name is the accessible text), and `site.name` when the mark is shown alone.
- **Dark theme:** the mark sits on a small paper-coloured tile
  (`.dark .brand-mark img`), because the navy strokes of the original would
  disappear on a dark header.

Used in the public header and footer, sign-in and sign-up, the candidate,
employer and staff portals, and the 500 page (which cannot use the context
processor and links the static files directly).

## Kept on purpose

- The webhook headers `X-CareerAI-Signature`, `X-CareerAI-Event` and
  `X-CareerAI-Timestamp`: an API contract with integrators. Renaming them
  needs a deprecation window, announced to webhook subscribers first. The
  employer webhook page names them because integrators must check them.
- The billing `User-Agent` is now `Kaarya-billing/1`; providers do not depend
  on it.
- Demo and fixture passwords: test data, not branding.
- Package, module, database, cache and Docker names.

## Needs a person

- **The support address and domain.** `hello@careerai.app` is still the
  default for `SITE_SUPPORT_EMAIL` and the sender. A Kaarya address cannot be
  invented here: mail to a domain nobody owns is lost. Set
  `SITE_SUPPORT_EMAIL` and `DEFAULT_FROM_EMAIL` once the domain exists.
- **The privacy policy contact.** The shipped legal text (version
  `0.1-draft`, `apps/core/legal_seeds.py`) names `hello@careerai.test`. Legal
  versions are kept reproducible, so this changes with the next version
  published by whoever owns the legal text, not by editing 0.1.
- **Optional:** artwork that spells KAARYA, if the lettering of the original
  is wanted rather than the typeset wordmark.

## "AI" in product names

CV features are named for what they do, without "AI": **CV Analysis**,
**CV Builder**, **Assisted CV**, **CV Improvement** and **CV Templates**.

Disclosure is not naming. Text a model wrote is still labelled "Written by AI"
or "AI-assisted" (SRS §9.3.9), because a candidate is entitled to know which
sentences in a review of their own CV came from a model.
