"""phase3_oauth_tokens

Revision ID: a1b2c3d4e5f6
Revises: 5044ada9f866
Create Date: 2026-04-30 14:00:00.000000

Crée la table oauth_tokens pour stocker les tokens OAuth chiffrés (Google APIs).
Phase 3+ : permettra l'ingest Gmail/Photos/Drive/Calendar/Fit/People/Tasks.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "5044ada9f866"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("service", sa.String(length=50), nullable=False),
        sa.Column("user_email", sa.String(length=255), nullable=False),
        sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("token_type", sa.String(length=20), nullable=False, server_default="Bearer"),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "provider",
            "service",
            "user_email",
            name="uq_oauth_provider_service_user",
        ),
    )
    op.create_index(
        op.f("ix_oauth_tokens_provider"),
        "oauth_tokens",
        ["provider"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth_tokens_service"),
        "oauth_tokens",
        ["service"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_tokens_service"), table_name="oauth_tokens")
    op.drop_index(op.f("ix_oauth_tokens_provider"), table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
