"""
Cover letters gain AI attribution and a public identifier (P14).

`public_id` is added in three steps rather than the one `makemigrations`
generated, and the reason is a real failure rather than caution. `AddField`
with `unique=True` and a callable default evaluates that default **once** for
the whole `ALTER TABLE`, so every existing row receives the same UUID and the
unique index cannot be created. Every other model in this project got its
`public_id` in an initial migration, where the table is empty and the problem
cannot arise; this is the first time one has been added to a populated table.

So: add it nullable and non-unique, fill it per row, then tighten it. The
backfill iterates rather than using one `UPDATE`, because the point is that
each row gets its own value.
"""

import apps.common.uuid7
import django.db.models.deletion
from django.db import migrations, models


def fill_public_ids(apps, schema_editor):
    """One UUIDv7 per existing letter."""
    CoverLetter = apps.get_model("applications", "CoverLetter")
    for pk in CoverLetter.objects.filter(public_id__isnull=True).values_list("pk", flat=True):
        CoverLetter.objects.filter(pk=pk).update(public_id=apps.common.uuid7.uuid7())


def drop_public_ids(apps, schema_editor):
    """Reverse of the backfill: the column goes away with the field."""


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0002_alter_airun_status"),
        ("applications", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="coverletter",
            name="ai_run",
            field=models.ForeignKey(
                blank=True,
                help_text="The call that drafted this, when one did (ADR-A11).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="cover_letters",
                to="ai.airun",
            ),
        ),
        migrations.AddField(
            model_name="coverletter",
            name="source",
            field=models.CharField(
                choices=[
                    ("written", "Written by the candidate"),
                    ("ai_draft", "Drafted by AI, unedited"),
                    ("ai_edited", "Drafted by AI, then edited"),
                ],
                default="written",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="coverletter",
            name="tone",
            field=models.CharField(
                blank=True,
                choices=[
                    ("direct", "Direct and plain"),
                    ("warm", "Warm"),
                    ("formal", "Formal"),
                ],
                max_length=16,
            ),
        ),
        # --- public_id, in three steps (see the module docstring) -----------
        migrations.AddField(
            model_name="coverletter",
            name="public_id",
            field=models.UUIDField(default=apps.common.uuid7.uuid7, editable=False, null=True),
        ),
        migrations.RunPython(fill_public_ids, drop_public_ids, elidable=True),
        migrations.AlterField(
            model_name="coverletter",
            name="public_id",
            field=models.UUIDField(default=apps.common.uuid7.uuid7, editable=False, unique=True),
        ),
    ]
