"""
The database's half of ADR-A8: a finalized version cannot be updated.

`ResumeVersion.save()` already refuses, but a model guard only binds code that
goes through the ORM. An employer's copy of a CV must be unchangeable by a data
fix, a shell session, a bulk `.update()` or a future migration written by
somebody who has not read the ADR — so the rule lives in the database, where it
binds every one of them.

**Exactly one transition is permitted** (ADR-A38): `redacted_at IS NULL` to
`redacted_at IS NOT NULL`. In that transition `content`, `content_schema_version`,
`redaction_reason` and `redacted_by` may change and **every other column must be
byte-identical**. Without this carve-out, privacy erasure was impossible: the row
holds contact details, could not be updated, and could not be deleted while an
application referenced it.

Redaction is single-use. A second attempt is an update of an already-redacted
row, which the first branch rejects.
"""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION resumes_version_immutable()
RETURNS TRIGGER AS $$
BEGIN
    -- The only permitted transition: an unredacted row becoming redacted.
    IF OLD.redacted_at IS NOT NULL OR NEW.redacted_at IS NULL THEN
        RAISE EXCEPTION
            'resume version %.% is finalized and cannot be updated',
            OLD.document_id, OLD.version_no
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Redaction replaces the content and nothing else. Every evidentiary
    -- column must survive byte-identical, or the hash stops proving anything.
    IF NEW.document_id IS DISTINCT FROM OLD.document_id
        OR NEW.version_no IS DISTINCT FROM OLD.version_no
        OR NEW.sha256 IS DISTINCT FROM OLD.sha256
        OR NEW.template_snapshot IS DISTINCT FROM OLD.template_snapshot
        OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
        OR NEW.created_reason IS DISTINCT FROM OLD.created_reason
        OR NEW.change_note IS DISTINCT FROM OLD.change_note
        OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at
    THEN
        RAISE EXCEPTION
            'redacting resume version %.% may only replace its content',
            OLD.document_id, OLD.version_no
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS resumes_version_immutable_trigger ON resumes_resumeversion;

CREATE TRIGGER resumes_version_immutable_trigger
    BEFORE UPDATE ON resumes_resumeversion
    FOR EACH ROW
    EXECUTE FUNCTION resumes_version_immutable();
"""

REVERSE = """
DROP TRIGGER IF EXISTS resumes_version_immutable_trigger ON resumes_resumeversion;
DROP FUNCTION IF EXISTS resumes_version_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("resumes", "0002_resume_documents_and_versions"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
    ]
