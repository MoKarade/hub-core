"""Modele LocationAddress : cache reverse-geocoding par cellule de grille fine.

On ne fait PAS un row par visit (13k+ visites identiques au meme lieu) mais un
row par cellule de grille de ~11m (lat_e4, lng_e4 = round(lat * 10000)).

Permet de geocoder ~3000 cellules uniques au lieu de 13k visites individuelles,
respectant la rate-limit Nominatim (1 req/sec).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class LocationAddress(Base):
    """Cache d'adresses geocodees par cellule de grille (resolution ~11m)."""

    __tablename__ = "location_addresses"
    __table_args__ = (
        UniqueConstraint("lat_e4", "lng_e4", name="uq_location_address_cell"),
        Index("ix_location_address_country", "country"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Cellule de grille : lat * 10000 (precision ~11m)
    lat_e4: Mapped[int] = mapped_column(Integer, nullable=False)
    lng_e4: Mapped[int] = mapped_column(Integer, nullable=False)

    # Coords du centre de la cellule (ou de la 1ere visite dans la cellule)
    lat: Mapped[float] = mapped_column(nullable=False)
    lng: Mapped[float] = mapped_column(nullable=False)

    # Champs Nominatim
    display_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    house_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    road: Mapped[str | None] = mapped_column(String(255), nullable=True)
    suburb: Mapped[str | None] = mapped_column(String(120), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postcode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    osm_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    osm_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Statut du geocodage
    status: Mapped[str] = mapped_column(String(20), default="ok")  # 'ok', 'failed', 'no_result'
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    geocoded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def short_label(self) -> str | None:
        """Label compact pour tooltips : 'rue, ville' ou 'POI, ville'."""
        parts = []
        if self.road:
            if self.house_number:
                parts.append(f"{self.house_number} {self.road}")
            else:
                parts.append(self.road)
        if self.city:
            parts.append(self.city)
        if not parts and self.country:
            parts.append(self.country)
        return ", ".join(parts) if parts else None

    def __repr__(self) -> str:
        return f"<LocationAddress {self.lat_e4},{self.lng_e4} {self.short_label() or 'no_address'}>"
