"""add_removal_requests

Revision ID: e1f2a3b4c5d6
Revises: d4f7e9a2c5b1
Create Date: 2026-05-06 16:00:00.000000

Table removal_requests : tracker des demandes Loi 25 / PIPEDA / RGPD que Marc
envoie aux entreprises pour exercer ses droits (acces, suppression).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d4f7e9a2c5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "removal_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("company_email", sa.String(length=200), nullable=True),
        sa.Column("company_url", sa.String(length=500), nullable=True),
        sa.Column("request_type", sa.String(length=20), nullable=False, server_default="deletion"),
        sa.Column("legal_basis", sa.String(length=20), nullable=False, server_default="loi25"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="draft"),
        sa.Column("subject", sa.String(length=300), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_removal_requests_company_name",
        "removal_requests",
        ["company_name"],
        unique=False,
    )
    op.create_index(
        "ix_removal_requests_status",
        "removal_requests",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_removal_requests_status", table_name="removal_requests")
    op.drop_index("ix_removal_requests_company_name", table_name="removal_requests")
    op.drop_table("removal_requests")
