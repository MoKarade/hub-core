"""Modele Photo : metadata Google Photos (Phase 3c).

On stocke les metadonnees only (pas les bytes : trop lourd, ils restent chez
Google). Permet recherche par date, dimensions, type media. Recherche
semantique ('photos de mes vacances') = Phase 3c+ avec CLIP embeddings.

Phase 3c+ : enrichissement GPS / faces (cf. champs lat/lng/exif_data/faces).
Necessite de telecharger les bytes des photos via Picker baseUrl + bearer,
extraire l'EXIF avec exifread/Pillow.ExifTags, parser GPSInfo, persister.
Pour le face recognition : modele local face-api.js (browser) ou face_recognition
(Python, dlib) cote backend.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class Photo(Base):
    __tablename__ = "photos"
    __table_args__ = (
        Index("ix_photos_user_creation", "user_email", "creation_time"),
        Index("ix_photos_geo", "latitude", "longitude"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_email: Mapped[str] = mapped_column(String(255), index=True)
    media_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    """ID Google Photos (idempotence resync)."""

    filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    creation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    """Quand la photo a ete prise (mediaMetadata.creationTime)."""

    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_video: Mapped[bool] = mapped_column(Boolean, default=False)
    video_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    camera_make: Mapped[str | None] = mapped_column(String(100), nullable=True)
    camera_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    """URL temporaire pour afficher (expire ~60min). Refresh via API si besoin."""

    product_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    """URL pour ouvrir dans photos.google.com (permanent)."""

    # === Phase 3c+ : GPS ===========================================
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Latitude GPS extraite de l'EXIF (Phase 3c+). NULL si pas geolocalise."""

    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Longitude GPS extraite de l'EXIF."""

    altitude_m: Mapped[float | None] = mapped_column(Float, nullable=True)

    location_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Nom geocode reverse (ex: 'Lévis, QC, Canada'). Phase 3c+ via Nominatim."""

    # === Phase 3c+ : EXIF + faces (preparation, vide pour l'instant) ===
    exif_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """EXIF complet (ISO, aperture, exposure, lens, etc.) - Phase 3c+."""

    faces_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Nombre de visages detectes - Phase 7+ (face_recognition ou clip)."""

    faces_data: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    """Liste {bbox, embedding, person_id?} - Phase 7+."""

    clip_embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    """CLIP embedding pour search semantique - Phase 7+ (en pgvector idealement)."""

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
