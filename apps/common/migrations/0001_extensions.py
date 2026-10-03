"""
Enable the PostgreSQL extensions the platform depends on (ADR-A2).

`common` owns this migration because it owns no tables of its own and is a
dependency of every other app, so the extensions exist before any migration
that needs them runs.

  vector      similarity search over profile and job embeddings   (P13)
  pg_trgm     trigram search for typo tolerance and skill aliases (P2, P7)
  btree_gist  exclusion constraints mixing equality with ranges, which is what
              makes interview double-booking impossible            (P15)

They are created here rather than at the phase that first uses them because
CREATE EXTENSION needs elevated privileges: doing it once, at the bottom of the
dependency graph, keeps every later migration runnable by the application role.
"""

from django.contrib.postgres.operations import BtreeGistExtension, TrigramExtension
from django.db import migrations


class VectorExtension(migrations.operations.special.RunSQL):
    """CREATE EXTENSION vector.

    django.contrib.postgres ships operations for the contrib extensions but not
    for pgvector, which is third-party. RunSQL with IF NOT EXISTS is the whole
    of what CreateExtension does, and avoids taking a dependency on the pgvector
    Python package before any model has a vector field (that arrives in P13).
    """

    def __init__(self):
        super().__init__(
            sql="CREATE EXTENSION IF NOT EXISTS vector;",
            reverse_sql="DROP EXTENSION IF EXISTS vector;",
        )


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        TrigramExtension(),
        BtreeGistExtension(),
        VectorExtension(),
    ]
