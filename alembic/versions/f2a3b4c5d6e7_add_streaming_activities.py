"""add_streaming_activities

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-06 17:00:00.000000

Table streaming_activities : history Trakt.tv (Netflix / Prime / Disney+ /
Crunchyroll / Plex / etc.) agregee via OAuth Trakt.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "streaming_activities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="trakt"),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("item_type", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("show_title", sa.String(length=300), nullable=True),
        sa.Column("season", sa.Integer(), nullable=True),
        sa.Column("episode", sa.Integer(), nullable=True),
        sa.Column("runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("genres", sa.Text(), nullable=True),
        sa.Column("platform", sa.String(length=50), nullable=True),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", name="uq_streaming_external_id"),
    )
    op.create_index(
        "ix_streaming_activities_external_id",
        "streaming_activities",
        ["external_id"],
        unique=False,
    )
    op.create_index(
        "ix_streaming_activities_source",
        "streaming_activities",
        ["source"],
        unique=False,
    )
    op.create_index(
        "ix_streaming_activities_item_type",
        "streaming_activities",
        ["item_type"],
        unique=False,
    )
    op.create_index(
        "ix_streaming_activities_watched_at",
        "streaming_activities",
        ["watched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_streaming_activities_watched_at", table_name="streaming_activities")
    op.drop_index("ix_streaming_activities_item_type", table_name="streaming_activities")
    op.drop_index("ix_streaming_activities_source", table_name="streaming_activities")
    op.drop_index("ix_streaming_activities_external_id", table_name="streaming_activities")
    op.drop_table("streaming_activities")
