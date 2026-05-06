"""Modele PhotoFace + FaceCluster : detection + clustering visages (Phase 7+).

Workflow :
1. Pour chaque Photo, detecter les visages → 1 PhotoFace par visage detecte
   - bbox = boite englobante (top, right, bottom, left)
   - encoding = vecteur 128-d (face_recognition / dlib)
2. DBSCAN sur tous les encodings → assigne cluster_id a chaque PhotoFace
3. Marc nomme les clusters (FaceCluster.name = "Marc", "Sophie", etc.)
4. Recherche : `GET /v1/photos/by-face/{cluster_id}`

Pas de pgvector ici non plus : encoding stocke en JSON. Pour <5000 visages,
DBSCAN python est rapide. Au-dela, basculer sur faiss + clustering distribue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class FaceCluster(Base):
    """Un cluster de visages similaires (= 1 personne, idealement)."""

    __tablename__ = "face_clusters"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    """Nom donne par Marc (ex: 'Marc', 'Sophie'). None tant que pas nomme."""

    photo_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Nb de visages dans ce cluster (mis a jour par le job clustering)."""

    sample_face_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("photo_faces.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    """1 face representative pour afficher dans l'UI thumbnail."""

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

    def __repr__(self) -> str:
        return f"<FaceCluster {self.name or 'unnamed'} ({self.photo_count} faces)>"


class PhotoFace(Base):
    """Un visage detecte dans 1 photo.

    1 photo peut avoir N visages. Chaque visage a un encoding 128-d et
    eventuellement un cluster_id (apres clustering).
    """

    __tablename__ = "photo_faces"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    photo_id: Mapped[UUID] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Bounding box (top, right, bottom, left) en JSON list[int]
    bbox: Mapped[list[int]] = mapped_column(JSON, nullable=False)

    # Encoding 128-d (face_recognition / dlib) en JSON list[float]
    encoding: Mapped[list[float]] = mapped_column(JSON, nullable=False)

    cluster_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("face_clusters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Modele utilise (face_recognition est seul pour l'instant, mais on peut
    # ajouter insightface ou autre)
    model_name: Mapped[str] = mapped_column(String(50), nullable=False, default="dlib_hog")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<PhotoFace photo={self.photo_id} cluster={self.cluster_id}>"
