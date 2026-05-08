"""add_steam

Revision ID: i5d6e7f8g9h0
Revises: h4c5d6e7f8g9
Create Date: 2026-05-06 20:00:00.000000

Tables Steam : steam_games (catalogue) + steam_play_snapshots (snapshots periodiques).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "i5d6e7f8g9h0"
down_revision: str | None = "h4c5d6e7f8g9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "steam_games",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("appid", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("icon_url", sa.String(length=500), nullable=True),
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
        sa.UniqueConstraint("appid", name="uq_steam_games_appid"),
    )
    op.create_index("ix_steam_games_appid", "steam_games", ["appid"])

    op.create_table(
        "steam_play_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("playtime_forever_min", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("playtime_2weeks_min", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_played_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["steam_games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_steam_play_snapshots_game_id", "steam_play_snapshots", ["game_id"]
    )
    op.create_index(
        "ix_steam_play_snapshots_snapshot_at", "steam_play_snapshots", ["snapshot_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_steam_play_snapshots_snapshot_at", table_name="steam_play_snapshots")
    op.drop_index("ix_steam_play_snapshots_game_id", table_name="steam_play_snapshots")
    op.drop_table("steam_play_snapshots")
    op.drop_index("ix_steam_games_appid", table_name="steam_games")
    op.drop_table("steam_games")
