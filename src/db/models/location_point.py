"""Modele LocationPoint : un point de localisation GPS (Google Maps Timeline ou autre).

Sprint Phase 2 : on stocke un snapshot ponctuel (timestamp + lat/lng + accuracy).
Pas de notion de "trajet" / "visite" pour l'instant — c'est une derivation qu'on
calculera plus tard si besoin.

Idempotence : `(source, timestamp_utc, latitude_e7, longitude_e7)` est unique.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class LocationPoint(Base):
    __tablename__ = "location_points"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Identite temporelle
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    """Date/heure UTC du point GPS."""

    # Coordonnees
    # Stockees en Decimal pour precision (lat/lng en degres : 8 decimales = ~1mm)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    """Latitude en degres decimaux ([-90, 90])."""

    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    """Longitude en degres decimaux ([-180, 180])."""

    accuracy_m: Mapped[int | None] = mapped_column(nullable=True)
    """Rayon d'incertitude en metres (Google fournit). NULL si inconnu."""

    altitude_m: Mapped[int | None] = mapped_column(nullable=True)
    """Altitude en metres (optionnel)."""

    activity_type: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    """Type d'activite detecte ('still', 'walking', 'cycling', 'driving', etc.). NULL si inconnu."""

    # Metadonnees source
    source: Mapped[str] = mapped_column(String(50), index=True)
    """Source du point ('google_takeout_timeline', 'manual_pin', etc.)."""

    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Nom du fichier d'origine."""

    # Pour idempotence : entier pre-conversion (latE7 = lat * 1e7)
    latitude_e7: Mapped[int] = mapped_column(BigInteger)
    longitude_e7: Mapped[int] = mapped_column(BigInteger)

    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<LocationPoint {self.timestamp_utc.isoformat()} "
            f"({self.latitude}, {self.longitude}) ±{self.accuracy_m}m>"
        )
