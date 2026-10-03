"""Resume ingestion events (SRS §13.2)."""

from __future__ import annotations

from apps.notifications.events import register

register(
    "resume.uploaded",
    schema_version=1,
    required={"upload_public_id", "profile_public_id", "mime"},
    description="A signed-in candidate uploaded a CV.",
)
register(
    "resume.guest_uploaded",
    schema_version=1,
    required={"upload_public_id", "mime"},
    description=(
        "An anonymous visitor uploaded a CV. Carries no profile because there is "
        "none, and no content because nothing has read the file (ADR-A32)."
    ),
)
register(
    "resume.claimed",
    schema_version=1,
    required={"upload_public_id", "profile_public_id"},
    description="A guest upload was claimed by an account. Processing may now begin.",
)
register(
    "resume.parsed",
    schema_version=1,
    required={"upload_public_id", "engine_version", "skills_found", "experience_found"},
    description=(
        "A CV was read into structured data. Counts only — resume text never "
        "enters the outbox, which has a different retention (DEVELOPER.md §2.13). "
        "The owner is told, so leaving the progress page loses nothing (CVP-04)."
    ),
    preference_group="cv_insights",
    audience="account_owner",
    title="Your CV has been read",
    body="{skills_found} skills and {experience_found} roles found. Review them before anything is added.",
    # A re-read of the same file is not a second piece of news.
    dedupe="resume.parsed:{upload_public_id}",
)

register(
    "resume.document_created",
    schema_version=1,
    required={"document_public_id", "profile_public_id", "kind"},
    description="A candidate started a CV, blank or from an upload.",
)
register(
    "resume.version_created",
    schema_version=1,
    required={"document_public_id", "version_no", "reason", "sha256"},
    description=(
        "A version was finalized by one of the four permitted acts. Carries the hash, "
        "never the content: the outbox has a different retention from the version."
    ),
)
register(
    "resume.version_redacted",
    schema_version=1,
    required={"document_public_id", "version_no"},
    description="A version's content was replaced by a tombstone under erasure (ADR-A38).",
)
register(
    "resume.rendered",
    schema_version=1,
    required={"document_public_id", "version_no", "format", "sha256"},
    description=(
        "A version was rendered to a file. Carries the digest of the rendered bytes, "
        "which is what ADR-A8 means by the hash of the file submitted."
    ),
)
register(
    "resume.analysed",
    schema_version=1,
    required={"document_public_id", "version_no", "score", "algorithm_version", "findings"},
    description=(
        "A version was analysed. Carries the score, the scorer version and a count — "
        "never a finding, because findings quote the CV and the outbox has a different "
        "retention from the analysis. `algorithm_version` travels with the score so a "
        "consumer comparing two scores can tell whether they are comparable."
    ),
)
