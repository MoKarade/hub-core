"""Modele StreamingActivity : 1 visionnage de film/episode trace via Trakt.tv.

Trakt agrege Netflix, Prime, Disney+, Crunchyroll, Plex, Jellyfin, etc. Marc
installe la browser extension Trakt qui scrobble automatiquement ses
visionnages, puis on pull la history via OAuth.

`external_id` est l'ID Trakt unique (ou autre source futur). Garantit
l'idempotence : 2 syncs back-to-back = 0 doublon.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class StreamingActivity(Base):
    """1 visionnage trace par Trakt (ou autre source streaming)."""

    __tablename__ = "streaming_activities"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Source : 'trakt' principalement, futur 'plex' / 'jellyfin' / 'manual'
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="trakt", index=True)

    # ID externe (Trakt history ID), stable, idempotent
    external_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)

    # 'movie' | 'episode'
    item_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Pour episodes uniquement
    show_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)

    runtime_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    genres: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Liste de genres separee par virgule (drama,thriller)."""

    # Sur quelle plateforme (Netflix, Prime, Disney+, etc.) — Trakt ne donne pas
    # toujours, on essaie de detecter via l'extension scrobbler
    platform: Mapped[str | None] = mapped_column(String(50), nullable=True)

    watched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        if self.item_type == "episode":
            return f"<StreamingActivity {self.show_title} S{self.season}E{self.episode}>"
        return f"<StreamingActivity {self.title} ({self.year})>"
