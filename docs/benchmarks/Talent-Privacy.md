# Talent Search and Candidate Privacy — Section Loop Report

**Date:** 2026-09-13.
**Scope:** FR-9.1–FR-9.4 (talent search, profile views, export, anomaly checks), FR-2.8 ("who opened your profile"), FR-7.6 and FR-1.9 (the candidate's data export), and the candidate's visibility, privacy and data screens.

**Why this section came next:** it holds the most sensitive rules in the product: who may find a candidate, what an employer may take off the platform, and whether the candidate can see and control both.

**Local status: STABLE. Not READY.** Manual-only checks are listed in [`Manual-Verification-Consolidated.md`](Manual-Verification-Consolidated.md).

## Method

- **Journey probe on the development database** (`talent_probe.py`, 23 checks), inside a rolled-back transaction:
  - owner and recruiter search;
  - a public profile opened, then the candidate's visibility screen;
  - CSV export with a formula in a headline;
  - malformed, oversized and empty exports;
  - another company's slug, anonymous visitors, an unknown id;
  - the candidate narrows visibility after being viewed;
  - the candidate blocks the company while public;
  - `nobody` visibility;
  - grouping of the "who opened" list.
- **Browser:** `scripts/talent_a11y.py` runs axe-core in Chromium, light and dark at 1280 px, plus an overflow check at 375 and 320 px. It covers talent search (empty, results, no match, refused), a talent profile, the candidate visibility screen, and the privacy and data settings. 32 scans; screenshots were reviewed.
- **Export smoke** on a real demo candidate: the file serialises, sections are counted, queries are captured, and stage names are listed.

## Defects found and fixed

| # | Defect | Severity | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| P1 | A company the candidate **blocked** still found a public candidate in search and opened the profile, which also gave it a fresh `public` grant | High (privacy) | `searchable_for` admitted any `everyone` profile; blocking only revoked grants | Blocked companies are excluded in `searchable_for`, the single authorisation point | `BlockedCompanyTests` (2) |
| P2 | After switching from "Anyone with the link" to "Employers you've applied to" or "Nobody", every company that had opened the profile could still find and read it | High (privacy) | The `public` grant created on first open was never revoked, and search admits any live grant | Narrowing visibility revokes live `public` grants and writes an audit row. Application, pool and invitation grants stay | `NarrowedVisibilityTests` (3) |
| P3 | Candidate-typed text was exported to CSV as a live formula (`=HYPERLINK(...)`) | High (security) | Cells written raw | Cells starting with `= + - @ tab CR` are prefixed with `'` | `ExportSafetyTests` |
| P4 | The candidate's data export held only profile sections: no applications, cover letters, **messages (FR-7.6, Must)**, profile views or access grants | High (compliance) | `export_profile` covered only the profile | Adds `applications` (candidate-facing stage names only), `cover_letters`, `messages` (deleted messages listed without text, recruiters not named), `profile_views` and `access`. The data screen shows a row and count for each | `DataExportActivityTests` (5) |
| P5 | A malformed `candidate` id in the export form caused a **500** | Medium (stability) | The raw string reached the UUID filter | Invalid ids and duplicates are dropped after the cap check; if none remain, the export redirects with a message | `ExportSafetyTests` (2) |
| P6 | The candidate visibility screen said blocking is done "in privacy settings", but that screen has no block control | Medium (misleading privacy copy) | Copy out of step with the UI (blocking exists only in a conversation) | Now points to Messages and says a block also hides the candidate from that company's searches | `WhoOpenedGroupingTests` |
| P7 | `min_months=-1` returned the whole searchable corpus, getting around the "empty search is refused" rule. `NaN`, `Infinity` and negative salaries were accepted | Low / medium | Negative and non-finite values parsed as real filters | Months clamped to ≥ 0; salary must be finite and ≥ 0 | `QueryParsingTests` (2) |
| P8 | The export's `profile_url` was `/employer/talent/<id>/`, which 404s | Low | Hard-coded path with no company | `reverse("employer:talent_profile", ...)` | `ExportSafetyTests` |
| P9 | "Who opened your profile" showed one company twice on the same day when it came through both an application and a search | Low | Grouped by source as well as company and day | Grouped by company and day, with all sources named ("From your application and a candidate search") | `WhoOpenedGroupingTests` |
| P10 | Data settings: axe **serious** `definition-list` / `dlitem` | Serious (accessibility) | `dt`/`dd` wrapped in an inner `div` | Each group's `dt`/`dd` are now direct children | axe rescan: clean |
| P11 | Talent results: axe `heading-order` (h1 → h3) | Moderate (accessibility) | No h2 over the result rows | Screen-reader-only "Matching candidates" h2 | axe rescan: clean |

## Verified working without change

- **Grants:**
  - a recruiter can search, but export is refused with 403 and writes no view row;
  - the owner (holds `candidate.export`) can export.
- **Visibility and access:**
  - opening a profile logs a view, creates the `public` grant, and appears on the candidate's screen the same day;
  - the profile page shows no email;
  - another company's slug returns 404, an unknown id 404, and a `nobody` candidate 404 (never 403);
  - anonymous visitors are redirected to sign in.
- **Export guards:**
  - 101 ids are refused (the cap counts the request as sent, so 101 copies of one id are refused too);
  - an empty export is refused;
  - no contact fields are exported.

## Measurements

| Item | Result |
|---|---|
| Probe | 23/23 PASS after fixes (7 FAIL before) |
| axe | 32 scans. Before: 4 serious (data settings, light and dark) and 2 moderate. After: 0 violations |
| Overflow | 0 at 375 and 320 px on all 8 screens |
| Talent search, one result | 18 queries (probe fixture) |
| Candidate data export, demo candidate | 14 queries. 5 applications, 8 letters, 4 messages, 1 grouped view row, 6 grants. Stages exported: Interview, New, Shortlisted (candidate-facing labels) |

The probe and scan use the development server and database. The accessibility scan opens a real talent profile, so the demo candidate (sita.rai) now has extra view rows and a `public` grant from Fonepay in the dev DB.

## Checks

| Check | Result |
|---|---|
| Targeted: `apps.candidates`, `apps.applications`, `apps.messaging`, `portals.candidate`, `portals.employer` | 731 OK. The first run had 1 failure: my de-duplication had moved the cap check. Fixed by checking the cap before filtering; rerun OK |
| New regression tests | 19: `test_talent_loop_regressions.py` (14), `test_data_export_regressions.py` (5) |
| Full project suite | Ran 3091 tests in 546 s — **OK** (`var/benchmark/full-suite-talent-loop.log`) |
| mypy (configured scope `apps`, 357 files) | clean, after fixing 1 union error introduced in this loop |
| Ruff format/check | clean on changed files |
| Migrations | `makemigrations --check --dry-run`: no changes detected |

## Remaining in this section (not fixed; explicit work)

> **Resolved by the owner on 2026-09-18** (SRS §0.4; Loop-Closure-Ledger §8): item 2 → **D-05** (terms under three characters match a skill's whole name or alias; an unknown one-letter term is refused); item 3 → **D-06** (narrowing revokes public, pool and invitation grants and pool memberships; applications stay; the privacy copy says so).

1. **FR-1.9, original files (Must):** the export is JSON only. Uploaded CVs and other original files are not packaged. This needs a decision on format (zip vs. per-file links) and on size limits.
2. **Weak empty-search rule:** `q=a` matches almost every candidate through the skill-name `icontains` match. The real controls are the per-membership rate limit, the 200-result ceiling and the anomaly checks. A minimum term length would help, but it changes search behaviour and needs a product decision.
3. **Pool and invitation grants vs. the "Employers you've applied to" wording:** the option text says only companies holding an application can open the profile, but pool and invitation grants survive narrowing. Either the copy or the rule must change; this is a policy decision.
4. **No list of blocked companies and no unblock control** for the candidate. `unblock_company` exists in the service only.
5. **Anomaly day boundary** is UTC midnight, not Nepal local time (a 5 h 45 min offset), so daily thresholds straddle the local working day. Anomaly events were not driven to a staff screen in this loop.
6. **Search form:** no skill picker, although the query supports skill ids.
7. **Rate limits** were not re-driven against Redis in this pass.

## Manual-only (added to the consolidated checklist)

- Open an exported CSV in Excel, LibreOffice and Google Sheets: formulas must show as text and non-ASCII names must display correctly.
- Screen reader: talent search results, export checkboxes and the result message; the privacy visibility radios; the data screen list and download.
- Two browsers: a recruiter has a profile open while the candidate narrows visibility or blocks; the recruiter's reload must return 404.
