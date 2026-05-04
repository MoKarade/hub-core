"""add_named_places_trip_notes

Revision ID: c9e5d3f2a1b6
Revises: b8d4c2e1a3f5
Create Date: 2026-05-04 21:00:00.000000

Tables Marc-personalisees :
- named_places : lieux nommes (Maison parents, Chalet, etc.)
- trip_notes   : notes libres sur les voyages, ancrees par start_date
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e5d3f2a1b6"
down_revision: str | None = "b8d4c2e1a3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "named_places",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("lat", sa.Numeric(10, 7), nullable=False),
        sa.Column("lng", sa.Numeric(10, 7), nullable=False),
        sa.Column("radius_m", sa.Float(), nullable=False, server_default="200"),
        sa.Column("semantic_type", sa.String(length=50), nullable=True),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column("icon", sa.String(length=40), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_named_place_semantic", "named_places", ["semantic_type"])

    op.create_table(
        "trip_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("color", sa.String(length=20), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("start_date", name="uq_trip_note_start_date"),
    )
    op.create_index("ix_trip_note_start_date", "trip_notes", ["start_date"])


def downgrade() -> None:
    op.drop_index("ix_trip_note_start_date", table_name="trip_notes")
    op.drop_table("trip_notes")
    op.drop_index("ix_named_place_semantic", table_name="named_places")
    op.drop_table("named_places")
