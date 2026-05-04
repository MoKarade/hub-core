"""Modeles NamedPlace et TripNote : annotations Marc sur ses lieux/voyages."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, Float, Index, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class NamedPlace(Base):
    """Lieux nommes par Marc (Maison parents, Chalet, Gym, etc.).

    Toute visite a moins de `radius_m` metres herite optionnellement de ce nom
    (lookup cote frontend via grille spatiale).
    """

    __tablename__ = "named_places"
    __table_args__ = (
        Index("ix_named_place_semantic", "semantic_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    lat: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    lng: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    radius_m: Mapped[float] = mapped_column(Float, default=200, nullable=False)
    semantic_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)  # hex
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)   # lucide name
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class TripNote(Base):
    """Note libre sur un voyage identifie par sa date de debut.

    Les voyages sont calcules dynamiquement par /v1/locations/trips, il n'y a
    pas de table 'trips'. On ancre les notes sur start_date qui agit comme cle.
    """

    __tablename__ = "trip_notes"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rating: Mapped[int | None] = mapped_column(nullable=True)  # 1-5 etoiles
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
