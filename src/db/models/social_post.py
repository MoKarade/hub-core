"""Modele SocialPost : posts Facebook / Instagram ingere depuis exports manuels.

Pourquoi pas API ?
  - Facebook Graph API : restreint depuis Cambridge Analytica (2018).
    Pour lire ton propre profil, Meta exige une "App Review" payante et
    impossible a obtenir pour usage personnel.
  - Instagram Basic Display API : meme situation, deprecated en cours,
    remplace par Instagram Graph API qui requiert un compte Business +
    review Meta.

Solution : Meta Account Center > Download Your Information.
  - Facebook : https://accountscenter.facebook.com/info_and_permissions/dyi
  - Instagram : meme URL, choisir "Instagram"
  - Format JSON, ~200 MB, livre en 1-2 jours

Workflow Marc :
  1. Telecharge l'export tous les 1-3 mois
  2. Dezippe dans C:\hub\inbox\meta-export-YYYY-MM\
  3. POST /v1/social/import-export {"path": "..."} -> ingere

Cette table est minimaliste mais extensible. Champs unifies entre FB et IG.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class SocialPost(Base):
    __tablename__ = "social_posts"
    __table_args__ = (
        Index("ix_social_posted", "platform", "posted_at"),
        Index("ix_social_user", "user_email", "platform", "posted_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_email: Mapped[str] = mapped_column(String(255), index=True)

    platform: Mapped[str] = mapped_column(String(20), index=True)
    """'facebook' | 'instagram' | 'messenger'"""

    external_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    """ID unique fourni par Meta, deduplique sur re-import."""

    post_type: Mapped[str] = mapped_column(String(30), index=True)
    """'post' | 'photo' | 'video' | 'story' | 'message' | 'reel' | 'comment' | 'reaction'"""

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Texte du post / caption photo / message."""

    media_urls: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    """Liste des chemins relatifs vers les fichiers media dans l'export
    (les images/videos sont a cote du JSON, on les reference par path)."""

    place: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Lieu tagge dans le post (FB/IG affichent souvent ca)."""

    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    """Personnes taggees + hashtags."""

    reactions_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    """Date d'origine du post / message."""

    raw_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """JSON complet de l'export pour pouvoir re-jouer / extraire plus tard."""

    source_export: Mapped[str] = mapped_column(Text)
    """Chemin du dossier d'export d'ou ca vient (pour audit / retraitement)."""

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
