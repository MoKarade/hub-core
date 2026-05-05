"""Modele NewsArticle : articles de presse via RSS Google News (Phase 6).

Source par defaut : https://news.google.com/rss?hl=fr-CA&gl=CA&ceid=CA%3Afr
(actualites Quebec en francais, gratuit, sans auth, sans limite de quota).

Idempotence : guid (Google News fournit un GUID unique par article).
Pas de body texte stocke (RSS ne fournit que le titre + extrait + lien).
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (Index("ix_news_published", "published_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    guid: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    """Identifiant unique fourni par le flux RSS (Google News -> URL deduppee)."""

    title: Mapped[str] = mapped_column(Text)
    """Titre de l'article (souvent prefixe par la source : 'TVA - ...')."""

    link: Mapped[str] = mapped_column(Text)
    """URL canonique de l'article (clickable -> redirige vers la source)."""

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Extrait court (~200 chars) fourni par RSS, parfois HTML."""

    source: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    """Nom de la source de presse (Le Devoir, Radio-Canada, La Presse...).
    Extrait du champ <source> du RSS Google News."""

    category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    """Categorie : actualites|sports|business|tech|sante|monde. Optional, parse heuristique."""

    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    """URL de l'image associee (rare dans Google News RSS, plus dispo dans certains feeds)."""

    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    """Date de publication (header Date du flux)."""

    feed_url: Mapped[str] = mapped_column(Text)
    """URL du feed RSS d'ou vient l'article (pour multi-feeds futur)."""

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
        return f"<NewsArticle title={self.title[:40]!r} source={self.source!r}>"
