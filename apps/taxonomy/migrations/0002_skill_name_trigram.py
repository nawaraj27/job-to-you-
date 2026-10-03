"""
The trigram index resolution's fuzzy fallback scans.

Depends on `common.0001_extensions` explicitly: `gin_trgm_ops` does not exist
until `CREATE EXTENSION pg_trgm` has run, and on a fresh database Django is free
to apply these two apps in either order. Without the dependency the migration
passes locally — where the extension is already there — and fails on every new
environment, which is the worst place to find out.
"""

import django.contrib.postgres.indexes
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("taxonomy", "0001_initial"),
        ("common", "0001_extensions"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="skill",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["name"], name="skill_name_trgm_idx", opclasses=["gin_trgm_ops"]
            ),
        ),
    ]
