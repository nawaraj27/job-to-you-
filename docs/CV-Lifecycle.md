# CV lifecycle: saved CVs, unsaved drafts, deletion

Implements tracker rows CV-1 to CV-5 (`docs/requirements/Kaarya-Refinement-Tracker.md`).

## Three states

| State | How it is recognised | Listed where | Removed when |
|---|---|---|---|
| **Saved CV** | `ResumeDocument.saved_at` is set | "Your saved CVs" (CV hub), "Your CVs" (dashboard) | Only when the candidate deletes or archives it |
| **Unsaved draft** | `saved_at` is null, not archived | "Unsaved drafts" (CV hub and dashboard), with a removal date | Automatically after `RESUME_DRAFT_RETENTION_DAYS` untouched, or when deleted |
| **Archived** | `archived_at` is set | "Archived" (CV hub) | Never expires; restorable |

A document becomes saved the first time a version is finalised:
`services.versions.finalize` calls `_mark_saved` before anything else. That
happens on **Save version**, on applying with the CV, on downloading it, and
on running an analysis (each of those works on a frozen version). `finalize` is the only
code that creates a `ResumeVersion`, so "has a version" implies "saved".

Opening a template, uploading a file and choosing **Edit as a CV**, starting an
Assisted CV or duplicating a CV all create an **unsaved draft**. Nothing about
a draft is shown to employers.

Migration `resumes/0013_document_saved_at` set `saved_at = created_at` for
every document that already existed, so nobody's CV changed state on upgrade.

## Limits

| Limit | Value | Where |
|---|---|---|
| Unsaved drafts at once | 5 (`MAX_DRAFTS`) | `services.documents._guard_drafts` |
| CVs in total (saved + drafts, not deleted) | 20 (`MAX_DOCUMENTS`) | `services.documents._guard_count` |
| Draft retention | 14 days (`RESUME_DRAFT_RETENTION_DAYS`, env) | `config/settings/base.py` |

At the draft limit the candidate is told to save or delete one first; nothing
is removed for them.

## Expiry

- Beat entry `purge-abandoned-cv-drafts` runs `apps.resumes.tasks.purge_abandoned_drafts`
  daily at 03:20 on the `default` queue.
- It removes a document only if it is unsaved, not archived, has **no
  version**, and neither the document nor its draft has been touched since
  the cutoff. Each row is re-read under `select_for_update`, so a draft saved
  while the task runs is skipped.
- Each removal is audited as `resume.draft_expired`.
- If the expired draft was the candidate's main CV, the newest saved CV takes
  over as main (`_promote_a_saved_cv`).
- The task is idempotent; a second run removes nothing.

The removal date is shown on every draft ("Removed on 16 Oct 2026 unless you
save it"), in the editor status line, and is computed by
`services.documents.draft_expires_on` from the same setting.

## Deleting

- Every delete asks first (`hx-confirm`, or `data-confirm` for plain forms).
- A document **with** a version is soft-deleted (ADR-A12): an application may
  point at one of its versions, and an employer's copy must not disappear.
- A document **without** a version is removed outright, with its draft,
  snapshots and conversation. No application can refer to it.
- Either way, the old editor, assistant and print URLs stop working: the
  ownership policy finds no live document and the views redirect to the CV hub.

## Tests

- `apps/resumes/tests/test_cv_lifecycle.py`: saved vs draft listing, the draft
  limit, expiry (cutoff, saved and archived untouched, versioned untouched,
  idempotency, primary hand-over), delete with and without versions, stale
  URLs, a document missing its draft row.
- `apps/resumes/tests/test_documents.py`, `test_policies.py`: soft delete of
  versioned CVs, the 20-CV cap on saved CVs, deleted CVs not editable.
- Browser: Journey A (draft created, deleted through the confirm dialog, old
  URL redirects to the hub), Journey B (an Assisted CV becomes saved on Save
  version).
