# Employer Onboarding, Team and Verification — Section Loop Report

**Date:** 2026-09-14.
**Scope:** company registration (SRS §19 onboarding flow), company verification with documents and a reviewer note (§5.6 `CompanyVerification*`, §7.5 state machine, §19.5 staff review with claim-and-lock), team invitations and membership lifecycle (§2.4–§2.6), role and company-scoped permission boundaries (A1, A18), the staff verification and anomaly queues (A5, §15.4), and the message-report workflow as far as the SRS requires it.

**Local status: STABLE. Not READY.** Manual-only checks are listed in [`Manual-Verification-Consolidated.md`](Manual-Verification-Consolidated.md) (rows 1.3f–1.3i, 2.5d–2.5f).

## Latest claims re-checked before starting

- Baseline before any change: `apps.companies portals.staff portals.employer apps.files apps.audit` — **215 tests OK** (`var/benchmark/baseline-employer-loop.log`). The first attempt failed only because it ran with dev settings (`files.E002`, permissive scanner with DEBUG off); every run since uses `--settings=config.settings.test`, as the previous loops did.
- The Talent loop's full-suite log (`var/benchmark/full-suite-talent-loop.log`) ends `exit=0`.

## Method

- **Real app, dev server on 8137, Celery worker running** (so uploads are scanned), two cookie jars (`localhost` and `127.0.0.1`):
  1. Signed up through the form as an employer, registered "Loop Himalaya Traders".
  2. Opened Verification. **Before:** no upload control; "Submit for review" returned "Attach your registration documents before submitting." — the flow could not be completed by anyone.
  3. After the fix: uploaded a PDF through the file input, the worker scanned it clean, submitted; the screen froze the documents and showed "In review".
  4. As staff (`ops@careerai.example.com`): queue listed the company, its website and the document by name; claimed; opened the document (200, `application/pdf`, `Content-Disposition: attachment`, `Cache-Control: private, no-store`); asked for more information with a reason. The dev DB then held an `AttachmentAccess` row (`verification_review`) and audit rows `…document_added`, `…state_changed`, `…claimed`, `…document_opened`, `…state_changed`.
  5. Owner's screen: "More information needed", the reviewer's note, and upload reopened. After draining the outbox, the owner had the in-app notification "Loop Himalaya Traders needs more information" with the reason and a link to the verification screen.
  6. Owner invited `loop.recruiter@example.test`; the console email carried the link.
  7. Signed in as the staff user, the link showed the address mismatch and no accept button. Signed out, the link went to sign-in, whose "Create an account" link now carried `next`; signed up as a **job seeker** with the invited address, landed back on the invitation, accepted, reached the company overview. The candidate dashboard still worked for the same account (dual use). The spent link then said it was no longer valid.
  8. As the recruiter: team screen read-only, verification screen read-only with an explanation.
  9. Owner withdrew a second invitation, changed the recruiter to Viewer, then removed them. The removed member's very next request to the company returned 404, and `/employer/` sent them to registration.
- **Journey probe on the development database** (`employer_probe.py`, scratchpad, rolled back): company creation, uploads by owner/recruiter/outsider, invitation validation, withdraw, role change, admin-cannot-make-owner, invitation → sign-in → sign-up → invitation, two-moderator claim collisions, support/outsider refusal, rejection shown to the company, anomaly queue authority and bad window, last-owner self-removal.
- **Browser scan:** `scripts/employer_a11y.py` — axe-core in Chromium, light and dark at 1280 px, overflow at 375 and 320 px — over company registration, verification, team and overview (owner), the verification and anomaly queues (staff), and sign-in/sign-up opened from an invitation (signed out). 32 scans.
- **Concurrency:** `TransactionTestCase` with two real connections and a widened window (the `apps/billing` pattern), run once against a copy with the new lock patched out to prove the test detects the race, then against the real code.

## Defects found and fixed

| # | Defect | Severity | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| E1 | **Verification could never be submitted.** The screen had no upload, and the service refused a submission without a document ("Document upload arrives with the company profile screens") | High (a Must flow dead-ended) | `CompanyVerificationDocument` had no service or view | `add_verification_document` / `remove_verification_document`: `company.edit` required; only in `not_submitted`/`more_info`; kinds limited to the two the screen asks for; PDF, Word, PNG, JPEG through `files.store` with company visibility and the company as owner; cap of 10; audited. Screen lists documents with scan state, upload form, remove, and "Submit" disabled with a reason until a document exists | `VerificationDocumentTests` (11), `VerificationScreenTests` (7) |
| E2 | **Staff could not read a submitted document.** The queue showed a count only, so a reviewer verified blind | High (review integrity) | No staff route to a company-visibility file; `files.policies` admits company members and `is_staff`, not `StaffRole` | `staff:verification_document` (moderator and above): only servable (scanned clean, not deleted) files; writes `AttachmentAccess` (`verification_review`) and an audit row before serving; always a download, `no-store`. Queue lists each document by kind and filename, or why it cannot be opened | `VerificationDocumentAccessTests` (6) |
| E3 | **Claim-and-lock was advisory.** Any moderator could decide an item a colleague had claimed, release a colleague's claim, or decide an item nobody had claimed | High (§19.5 collision control) | Only the template hid the buttons; `review_verification` and `release_verification` did not check the claim | Deciding needs your own live claim (`Conflict` otherwise); releasing another reviewer's live claim is refused; a decision clears the claim; a decision value outside verified / more information / rejected is refused | `ReviewClaimTests` (7), `ClaimEnforcementScreenTests` (3) |
| E4 | **Two owners removing each other at once left the company with no owner** | High (§2.6 last-owner rule) | Count-then-write with no lock; the check also used memberships loaded before the wait | `_lock_team` locks the company row, then re-reads actor and target, for remove, role change and transfer | `ConcurrentOwnerRemovalTests`. Without the lock: `['removed', 'removed']`, zero owners; with it: one refused (`var/benchmark/race-proof.log`) |
| E5 | **Nobody at the company was told a reviewer's decision** — including "more information needed", which is a request | Medium (FR-11 / §19.5) | Company events had no audience | New `company.verification_reviewed` event to a new `company_editors` audience (owners, admins, `company.edit` holders; not recruiters, not removed members): "<Company> needs more information / is verified / was not verified", reason as body, link to the screen | `ReviewNotificationTests` (2) |
| E6 | An open invitation **could not be withdrawn** | Medium (§2.6 "revocation is immediate") | No service or control | `revoke_invite`: `team.invite` required; scoped to the company (another company's id is 404); only an owner withdraws an owner invitation; accepted or already withdrawn is refused; audited. "Withdraw" on the team screen; acceptance of a withdrawn link is refused | `RevokeInviteTests` (7), `TeamScreenTests` |
| E7 | **Invited person with no account lost the invitation** at sign-up and landed on a dashboard | Medium (onboarding) | Sign-in's "Create an account" link dropped `next`, and sign-up ignored it | Sign-in carries `next` into sign-up and back; sign-up redirects to a same-host `next` only (`url_has_allowed_host_and_scheme`) | `InviteSignupRoundTripTests` (3), including an off-site `next` |
| E8 | A **blank or malformed email, or an unknown role**, made an invitation (the unknown role would grant nothing when accepted) | Medium | `EmailField`/`choices` are not validated on `objects.create` | Email validated; role must be in `Role` for invite and role change | `InviteValidationTests` (2), `ChangeRoleTests` |
| E9 | **No way to change a member's role** from the product (the service existed) | Medium | Missing control | Per-member role select and "Change role"; non-owners are not offered Owner; ownership transfer moved behind a disclosure that states the consequence | `TeamScreenTests` |
| E10 | A company website of "not a url" was stored and shown to reviewers | Low | `URLField` not validated on save | `_clean_website`: blank or http(s); a bare domain gains `https://`; registration re-renders with what was typed (400) instead of redirecting it away | `WebsiteTests` (3), `CreateCompanyScreenTests` |
| E11 | Stale membership rows decided role changes: a member removed, or an owner demoted, in another tab could still act | Low | Service trusted objects loaded by the view | Re-read inside the team lock (E4) | `ChangeRoleTests` (2) |
| E12 | Staff decision buttons read "More_Info"; anomaly rows showed raw codes (`view_volume`) | Low (clarity) | `state|title`; metadata printed as-is | Labels from `VerificationState`; `SIGNAL_LABELS` for anomaly kinds (unknown kinds still print) | `test_the_decision_buttons_read_as_words`, `AnomalyLabelTests` |
| E13 | A withdrawn invitation's page said "expired or already used" | Low (copy) | Written before withdrawal existed | "expired, been withdrawn, or already been used" | — |

Also: `accept_invite` now locks the invitation row, so a withdrawal and an acceptance at the same moment settle one after the other.

## Verified working without change

- A stranger or a removed member gets **404, never 403**, on every employer URL. A suspended company locks every surface. A foreign `membership_id` posted to the team screen is refused.
- Invitation tokens are stored as SHA-256 only. Invitations bind to the invited address and a second use is refused.
- The last owner cannot remove or demote themselves. An admin cannot invite, promote or remove an owner.
- Staff authority is `StaffRole`: a candidate, and a Django `is_staff` user without a role, get 404; support gets 403 on moderator queues. Anomaly window falls back to 7 days on a bad value.
- Anomaly queue reads the audit rows written by `candidates.services.anomaly` and takes no automatic action.
- A double click on "Create company" is already guarded client-side (`static/js/app.js`, the busy-on-submit handler).

## Measurements

| Item | Result |
|---|---|
| Probe (`employer_probe.py`) | Before: 13/30 PASS. After: 30/31. The one remaining FAIL is the probe's own limit: its upload is rolled back, so the scan never runs and the queue correctly offers no link |
| axe, employer and staff screens | 24 scans (owner and staff, light and dark): 0 violations |
| axe, sign-in/sign-up from an invitation | Sign-up: `color-contrast` serious (password hints, submit button). **Pre-existing:** the plain sign-up and sign-in pages report the same targets (`var/benchmark/signup-a11y-baseline.json`), and sign-in was clean in the other run, so the result depends on timing (likely the auth layout's entrance animation). Not changed in this loop; see Remaining |
| Overflow | 0 at 375 and 320 px on all 8 screens |
| Real upload in dev | Scanned clean by the worker within the page reload; served to staff as a 193-byte PDF |

## Checks

| Check | Result |
|---|---|
| Targeted: `apps.companies portals.staff portals.employer apps.accounts apps.notifications apps.files apps.audit` | **493 OK** (`var/benchmark/targeted-employer-loop.log`). The first run had 5 errors and 1 failure, all in new tests: calling `response.close()` on a `FileResponse` fires `request_finished`, which closed the test's DB connection; and a success message repeated the withdrawn email. Both tests corrected |
| Changed test | `apps/companies/tests/test_verification.py` now claims before deciding, which the service requires |
| New regression tests | 63: `test_onboarding_loop_regressions.py` (35), `test_team_concurrency.py` (1), `portals/employer/tests/test_onboarding_screens.py` (17), `portals/staff/tests/test_staff_screens.py` (+10) |
| Full project suite | Ran **3154** tests in 587 s — **OK** (`var/benchmark/full-suite-employer-loop.log`; 3091 before this loop + 63 new) |
| mypy (`apps`, 357 files) | clean, after typing one dict introduced in this loop |
| Ruff | changed files formatted and clean. `ruff check apps portals` reports 6 pre-existing findings in `apps/core/management/commands/seed_demo.py`, untouched here |
| Migrations | `makemigrations --check --dry-run`: no changes (no model fields changed; `VERIFICATION_DOCUMENT_KINDS` and `label` are Python only) |

## Policy conflicts — documented, not decided

> **Resolved by the owner on 2026-09-18** (SRS §0.4, Loop-Closure-Ledger §8): 1 → **D-02** (a rejected company may apply again after 30 days as a new attempt; every decision kept); 2 → **D-03** (never bind to another account); 3 → **D-01** (`is_staff` reads no private file; admin shows metadata only); 4 → **D-04** (members, invitees and the submitter cannot claim or decide). Item 5 stays deferred by SRS §21. Remaining item 1 below (domain proof) was built in the closure run and is optional by **D-07**. The list is kept as it was asked.

1. **Rejected is terminal.** SRS §7.5 draws `pending → verified | rejected` with no way back, and the state machine matches. There is no re-submission after rejection, so the screen shows the reason and a link to contact CareerAI. Whether a rejected company may try again (and after how long) is a product rule.
2. **Invitation address mismatch.** SRS §2.6 says the user "must choose to sign in as the invited address or to bind the invite to the current account, which is recorded". The code only refuses. Binding would let anyone holding a forwarded link join with any account; that trade-off needs a decision before it is built.
3. **`is_staff` still reads every private file** in `apps/files/policies.py` and `selectors.for_actor`, while `apps/accounts/staff.py` says `is_staff` is not authority. Tokens are minted only by owning views, so exposure needs a token, but the two rules disagree. Needs a security decision (and a look at Django-admin use) before changing.
4. **A reviewer who is also a member of the company** can verify it. The SRS says nothing about conflicts of interest.
5. **Message reports.** SRS §21 defers the staff moderation queue explicitly ("a screen with no operator"). `ConversationReport` rows are written and are visible in Django admin only. Kept in the backlog; not built.

## Remaining in this section (not fixed)

1. **Domain proof** (§19 flow "domain proof, then documents"; `CompanyDomain` model) has no service or screen.
2. **No notice to a removed member**, and no notice to the inviter when an invitation is accepted (`company.member_removed` / `member_joined` have no audience). Not stated as a Must.
3. **Auth pages' contrast** result above (pre-existing, timing-dependent) needs a stable reproduction, then a fix in the auth layout.
4. A Viewer sees the "Post a job" call to action on the overview (Jobs section; the job service refuses without the grant). Noted for the Jobs backlog.
5. Company registration asks only for name and website; legal name, industry and location are on the profile screen. The SRS does not require them at registration.
6. Uploads stay "being checked" in development unless a Celery worker is running (`celery -A config worker --queues=default,files --pool=solo`).

## Backlog carried forward (not re-verified in this loop)

Data export omits original uploaded files; short talent-search queries match almost everyone; visibility vs surviving pool/invitation grants needs a rule; candidates cannot list/unblock blocked companies; Redis rate limits not re-tested; messaging attachments/edit/delete not reachable and no staff message-report screen; Interview Parquet scoring error, single-rater evaluation, limited question pools, unverified real JaaS/microphone/screen-reader checks; anomaly day boundary in UTC.

## Manual-only (added to the consolidated checklist)

- 1.3f real invitation email and link survival; 1.3g real verification-decision notification; 1.3h ClamAV scan of a real certificate and an EICAR file (**Blocked**: needs clamd); 1.3i two moderators in two browsers.
- 2.5d–2.5f screen reader on the verification screen, team screen and staff queues.

## Postscript — verification completeness (2026-09-20)

This report describes the loop of 2026-09-14 and is unchanged above. One thing it did not cover has since been added; full detail in [`Loop-Closure-Ledger.md`](Loop-Closure-Ledger.md) §10 (VER-01).

**What the workflow now says about "enough".** `VERIFICATION_DOCUMENT_KINDS` has always held two kinds — company registration certificate, PAN or VAT certificate — and the employer screen has always asked for both in prose. Nothing computed whether both had arrived, so the company could not see which one was outstanding and the reviewer's queue listed what had been sent and nothing about what had not.

`companies.services.company` now answers that question in one place:

- `REQUIRED_DOCUMENT_KINDS` — the two kinds, in the order the screen asks;
- `missing_document_kinds(verification)` — the required kinds with no *usable* file. A quarantined or failed scan does not count (a reviewer cannot open it); a file still being scanned does (it is on its way);
- `submission_is_complete(verification)`;
- `document_checklist(verification)` — label and state per kind, so the employer screen and the staff queue cannot disagree.

The helpers read `verification.documents.all()`, so they cost nothing extra where the documents are already prefetched: the queue selector prefetches `documents__attachment`, and the employer screen now reads its verification through `selectors.verification_for(company=...)`, which does the same.

**It is advisory, not a gate.** `submit_verification` accepts exactly what it accepted before — one document is still enough to submit. Refusing an incomplete submission would shut out a company that genuinely holds only one certificate, and the workflow already has the right state for "not enough yet": the reviewer asks for more information with a reason, which keeps it as the same attempt (D-02). What changed is that nobody is now led to believe an incomplete submission is complete.

No state, transition, permission, notification or history behaviour changed. Regression tests: `apps/companies/tests/test_verification_workflow.py` (29), covering registration through each decision, the completeness rules, what each side is shown, and that neither `is_staff` alone nor a company owner can reach or decide anything on the queue.
