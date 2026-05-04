"""add_location_addresses

Revision ID: b8d4c2e1a3f5
Revises: 93cf1ed7b005
Create Date: 2026-05-04 18:00:00.000000

Cache de reverse-geocoding par cellule de grille (lat_e4, lng_e4).
Chaque cellule = ~11m, donc on geocode ~3000 cellules au lieu de 13k visites.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "b8d4c2e1a3f5"
down_revision: str | None = "93cf1ed7b005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "location_addresses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lat_e4", sa.Integer(), nullable=False),
        sa.Column("lng_e4", sa.Integer(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("display_name", sa.String(length=500), nullable=True),
        sa.Column("house_number", sa.String(length=20), nullable=True),
        sa.Column("road", sa.String(length=255), nullable=True),
        sa.Column("suburb", sa.String(length=120), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("postcode", sa.String(length=20), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("osm_type", sa.String(length=20), nullable=True),
        sa.Column("osm_id", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ok"),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column(
            "geocoded_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lat_e4", "lng_e4", name="uq_location_address_cell"),
    )
    op.create_index(
        "ix_location_address_country", "location_addresses", ["country"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_location_address_country", table_name="location_addresses")
    op.drop_table("location_addresses")
