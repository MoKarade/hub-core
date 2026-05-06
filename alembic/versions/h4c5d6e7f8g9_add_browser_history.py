"""add_browser_history

Revision ID: h4c5d6e7f8g9
Revises: g3b4c5d6e7f8
Create Date: 2026-05-06 19:00:00.000000

Table browser_history : visites URL ingerees depuis l'export Chrome/Firefox/etc.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h4c5d6e7f8g9"
down_revision: str | None = "g3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="chrome"),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("visited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("visit_duration_s", sa.Integer(), nullable=True),
        sa.Column("transition", sa.String(length=30), nullable=True),
        sa.Column("dedup_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_hash", name="uq_browser_history_dedup"),
    )
    op.create_index("ix_browser_history_source", "browser_history", ["source"])
    op.create_index("ix_browser_history_external_id", "browser_history", ["external_id"])
    op.create_index("ix_browser_history_domain", "browser_history", ["domain"])
    op.create_index("ix_browser_history_visited_at", "browser_history", ["visited_at"])
    op.create_index("ix_browser_history_dedup_hash", "browser_history", ["dedup_hash"])


def downgrade() -> None:
    op.drop_index("ix_browser_history_dedup_hash", table_name="browser_history")
    op.drop_index("ix_browser_history_visited_at", table_name="browser_history")
    op.drop_index("ix_browser_history_domain", table_name="browser_history")
    op.drop_index("ix_browser_history_external_id", table_name="browser_history")
    op.drop_index("ix_browser_history_source", table_name="browser_history")
    op.drop_table("browser_history")
