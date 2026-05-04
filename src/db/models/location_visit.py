"""Modele LocationVisit : visite semantique Google Timeline (semanticSegments).

Chaque segment 'visit' dans le nouveau format Timeline.json devient une ligne ici.
Le segment 'timelinePath' continue d'aller dans location_points.
Le segment 'activity' est capture dans location_activities.

Idempotence : dedup_hash = sha256(start_time_iso + lat_e7 + lng_e7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class LocationVisit(Base):
    """Une visite semantique (lieu, domicile, travail, commerce, etc.)."""

    __tablename__ = "location_visits"
    __table_args__ = (
        UniqueConstraint("dedup_hash", name="uq_location_visit_dedup"),
        Index("ix_location_visit_start", "start_time"),
        Index("ix_location_visit_semantic", "semantic_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Fenetre temporelle
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tz_offset_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Lieu
    lat: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    lng: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    lat_e7: Mapped[int] = mapped_column()
    lng_e7: Mapped[int] = mapped_column()

    # Semantique Google
    place_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    semantic_type: Mapped[str | None] = mapped_column(
        String(60), nullable=True
    )  # HOME, WORK, SEARCHED_ADDRESS, UNKNOWN_PLACE_TYPE, etc.
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Source
    source: Mapped[str] = mapped_column(String(50), default="google_timeline")
    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    @property
    def duration_minutes(self) -> float:
        return (self.end_time - self.start_time).total_seconds() / 60

    def __repr__(self) -> str:
        return (
            f"<LocationVisit {self.start_time.date()} "
            f"({self.semantic_type or 'UNKNOWN'}) {self.lat},{self.lng}>"
        )


class LocationActivity(Base):
    """Segment d'activite de transport (WALKING, IN_VEHICLE, CYCLING, etc.)."""

    __tablename__ = "location_activities"
    __table_args__ = (
        UniqueConstraint("dedup_hash", name="uq_location_activity_dedup"),
        Index("ix_location_activity_start", "start_time"),
        Index("ix_location_activity_type", "activity_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tz_offset_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    activity_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    distance_meters: Mapped[float | None] = mapped_column(Float, nullable=True)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)

    start_lat: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    start_lng: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    end_lat: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)
    end_lng: Mapped[Decimal | None] = mapped_column(Numeric(10, 7), nullable=True)

    source: Mapped[str] = mapped_column(String(50), default="google_timeline")
    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
