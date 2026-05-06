"""Modele PhotoEmbedding : embedding CLIP 512-d par photo (Phase 7+).

Stocke 1 vecteur par photo. Permet la recherche semantique : on embed la query
text, calcule cosine similarity contre toutes les rows, retourne top-K.

Pas de pgvector pour rester DB-agnostic (SQLite + Postgres). Le vecteur est
stocke en JSON. Pour <10k photos, la recherche en numpy est rapide (<200ms).
Pour scaler au-dela : ajouter pgvector + index HNSW (ADR futur).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class PhotoEmbedding(Base):
    """1 embedding CLIP par photo. UNIQUE par photo_id."""

    __tablename__ = "photo_embeddings"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    photo_id: Mapped[UUID] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )

    # Modele CLIP utilise pour generer l'embedding. Permet de re-embedder si
    # on change de modele (ex: upgrade ViT-B-32 -> ViT-L-14).
    model_name: Mapped[str] = mapped_column(String(50), nullable=False, default="ViT-B-32")
    """Format: open_clip model name (ex: 'ViT-B-32', 'ViT-L-14')."""

    # Vecteur 512-d (ViT-B-32) ou 768-d (ViT-L-14) en JSON list[float]
    embedding: Mapped[list[float]] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<PhotoEmbedding photo={self.photo_id} model={self.model_name}>"
