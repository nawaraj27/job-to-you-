# Job search: the company verification filter

Tracker row JB-1. Candidates can narrow the public job list, and their matched
jobs, to verified companies, to companies not yet verified, or see all.

## What "verified" means

The only source is `Company.verification_state == "verified"`, set by staff
through the audited verification review (`/staff/verification/`). No request
parameter, job field or employer setting can make a company read as verified.
Anything other than the two known values in `?verification=` is ignored
(the search falls back to all companies), never trusted.

## The filter

| Value | Label on the rail | Rows shown |
|---|---|---|
| (empty) | All companies | every published job |
| `verified` | Verified companies | jobs whose company is verified |
| `unverified` | Not yet verified | every other company (not submitted, pending, more information requested, rejected) |

- Defined once: `CompanyVerification` choices and the `verification` field of
  `SearchQueryInput` (`apps/jobs/search.py`); `_apply_filters` filters or
  excludes on the company's state.
- Combines with text search, location, workplace, remote scope, employment
  type, experience, category, salary, skills, company and sort, because it is
  one more field of the same query object.
- Kept by pagination, the active-filter chips (removable), saved searches and
  job alerts (`as_mapping` / `from_mapping`).
- **Facet counts** beside each choice exclude the verification filter itself,
  so choosing "Verified companies" still shows how many unverified jobs the
  other choice would give.
- **Matched jobs** (`/jobs/matched/`): the same choices as chips above the
  list (`RecommendedJobsView`).

## On each row

`components/job_row.html` says which kind of company it is: a "Verified"
badge with an icon (text, not colour alone), or "Not verified" in muted text.

## Tests and evidence

- `apps/jobs/tests/test_verification_filter.py`: default all; each choice;
  tampered values ignored; combined with text, workplace and sort; facet
  counts with and without other filters; the other choice stays on offer;
  pagination of 24 verified jobs over two pages without overlap; saved
  searches; labels on the rail and rows.
- Browser: Journey A (filters and pagination) and Journey E (an unverified
  employer's job appears under "Not yet verified" with the "Not verified"
  label at 375 px, and not under "Verified companies").
- Accessibility: `scripts/kaarya_a11y.py`, all three states at 320, 375, 768
  and 1280 px in light and dark: no serious or critical axe findings.
