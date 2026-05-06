"""add_photo_ml_tables

Revision ID: g3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-05-06 18:00:00.000000

Tables ML pour les photos (Phase 7+) :
- photo_embeddings : 1 vecteur CLIP 512-d par photo (semantic search)
- face_clusters : groupes de visages similaires (= 1 personne)
- photo_faces : 1 visage detecte avec encoding 128-d (dlib)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── photo_embeddings ─────────────────────────────────────────────────
    op.create_table(
        "photo_embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("model_name", sa.String(length=50), nullable=False, server_default="ViT-B-32"),
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("photo_id", name="uq_photo_embedding_photo"),
    )
    op.create_index(
        "ix_photo_embeddings_photo_id",
        "photo_embeddings",
        ["photo_id"],
        unique=False,
    )

    # ── face_clusters ────────────────────────────────────────────────────
    # Cree avant photo_faces car PhotoFace.cluster_id reference face_clusters
    op.create_table(
        "face_clusters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("photo_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_face_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_face_clusters_name", "face_clusters", ["name"], unique=False)

    # ── photo_faces ──────────────────────────────────────────────────────
    op.create_table(
        "photo_faces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("bbox", sa.JSON(), nullable=False),
        sa.Column("encoding", sa.JSON(), nullable=False),
        sa.Column("cluster_id", sa.Uuid(), nullable=True),
        sa.Column("model_name", sa.String(length=50), nullable=False, server_default="dlib_hog"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["photo_id"], ["photos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["cluster_id"], ["face_clusters.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_photo_faces_photo_id", "photo_faces", ["photo_id"], unique=False)
    op.create_index(
        "ix_photo_faces_cluster_id", "photo_faces", ["cluster_id"], unique=False
    )

    # FK retardee : face_clusters.sample_face_id -> photo_faces.id
    # On utilise ALTER TABLE pour ajouter la FK une fois les 2 tables creees
    # (use_alter=True dans le modele permet ca)
    with op.batch_alter_table("face_clusters") as batch_op:
        batch_op.create_foreign_key(
            "fk_face_clusters_sample_face_id",
            "photo_faces",
            ["sample_face_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("face_clusters") as batch_op:
        batch_op.drop_constraint(
            "fk_face_clusters_sample_face_id", type_="foreignkey"
        )
    op.drop_index("ix_photo_faces_cluster_id", table_name="photo_faces")
    op.drop_index("ix_photo_faces_photo_id", table_name="photo_faces")
    op.drop_table("photo_faces")
    op.drop_index("ix_face_clusters_name", table_name="face_clusters")
    op.drop_table("face_clusters")
    op.drop_index("ix_photo_embeddings_photo_id", table_name="photo_embeddings")
    op.drop_table("photo_embeddings")
