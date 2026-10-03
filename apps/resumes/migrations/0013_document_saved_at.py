"""
`ResumeDocument.saved_at`: saved CVs apart from unsaved drafts (docs/CV-Lifecycle.md).

Every CV that exists when this runs is treated as saved. The distinction is
new, and a candidate must not find that a CV they have been working on became
an expiring draft because of a deployment. Only CVs created from now on start
as drafts.
"""

from django.db import migrations, models
from django.db.models import F


def grandfather_existing(apps, schema_editor):
    ResumeDocument = apps.get_model("resumes", "ResumeDocument")
    ResumeDocument.objects.filter(saved_at__isnull=True).update(saved_at=F("created_at"))


class Migration(migrations.Migration):
    dependencies = [
        ("resumes", "0012_document_latest_improvement_job"),
    ]

    operations = [
        migrations.AddField(
            model_name="resumedocument",
            name="saved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(grandfather_existing, migrations.RunPython.noop),
    ]
