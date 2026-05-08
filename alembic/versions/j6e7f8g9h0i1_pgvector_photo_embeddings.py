"""pgvector_photo_embeddings

Revision ID: j6e7f8g9h0i1
Revises: i5d6e7f8g9h0
Create Date: 2026-05-08 11:00:00.000000

Bascule `photo_embeddings.embedding` de JSON vers `vector(512)` (pgvector) +
ajout d'un index HNSW pour la recherche cosine en temps O(log n).

Postgres-only : sur SQLite (PC dessin14 venv local), no-op. Le code de
recherche detecte le dialect en runtime et fallback sur numpy si pas pgvector.

Si la table contient deja des donnees, la conversion JSON -> vector se fait
via cast `text::vector` (le format text de pgvector accepte `[1.0,2.0,...]`
qui est aussi le format JSON list).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "j6e7f8g9h0i1"
down_revision: str | None = "i5d6e7f8g9h0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 512


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite et autres : on garde la colonne JSON. La recherche reste en
        # numpy cote applicatif.
        return

    # 1. Active l'extension (idempotent)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. Convertit la colonne JSON -> vector(512) via cast text intermediaire
    # Le cast jsonb -> text donne `[1.0, 2.0, ...]` que pgvector accepte
    op.execute(
        sa.text(
            f"ALTER TABLE photo_embeddings "
            f"ALTER COLUMN embedding TYPE vector({EMBEDDING_DIM}) "
            f"USING embedding::text::vector({EMBEDDING_DIM})"
        )
    )

    # 3. Index HNSW pour cosine similarity search
    # m=16, ef_construction=64 : valeurs par defaut pgvector pour <100k vecteurs
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_photo_embeddings_hnsw "
        "ON photo_embeddings USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP INDEX IF EXISTS ix_photo_embeddings_hnsw")
    # Retour en JSON : cast vector -> text -> jsonb
    op.execute(
        "ALTER TABLE photo_embeddings "
        "ALTER COLUMN embedding TYPE jsonb "
        "USING embedding::text::jsonb"
    )
