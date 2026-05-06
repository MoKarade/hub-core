"""add_push_subscriptions

Revision ID: d4f7e9a2c5b1
Revises: c9e5d3f2a1b6
Create Date: 2026-05-06 10:00:00.000000

Table push_subscriptions pour les notifications Web Push (PWA).
Remplace ntfy.sh par des notifs natives envoyees par l'app du hub elle-meme.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f7e9a2c5b1"
down_revision: str | None = "457269cc18e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(length=255), nullable=False),
        sa.Column("auth", sa.String(length=50), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("label", sa.String(length=80), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint", name="uq_push_subscription_endpoint"),
    )
    op.create_index(
        "ix_push_subscription_endpoint",
        "push_subscriptions",
        ["endpoint"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_push_subscription_endpoint", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
