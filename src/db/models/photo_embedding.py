"""Modele PhotoEmbedding : embedding CLIP 512-d par photo (Phase 7+).

Stocke 1 vecteur par photo. Permet la recherche semantique : on embed la query
text, calcule cosine similarity, retourne top-K.

Postgres : `vector(512)` (pgvector) avec index HNSW pour search O(log n).
SQLite : fallback JSON list[float] (search numpy en memoire). Voir `EmbeddingType`.

Dimension figee a 512 (ViT-B-32). Pour ViT-L-14 (768) ou autre, prevoir une
nouvelle table photo_embeddings_v2 ou adapter la migration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.types import EmbeddingType

EMBEDDING_DIM = 512


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

    # Postgres: vector(512) + index HNSW (search natif <=> cosine)
    # SQLite: JSON list[float] (fallback numpy)
    embedding: Mapped[list[float]] = mapped_column(EmbeddingType(EMBEDDING_DIM), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<PhotoEmbedding photo={self.photo_id} model={self.model_name}>"
