"""Modele BrowserHistory : 1 visite URL trace via export Chrome/Firefox.

Chrome stocke l'historique en SQLite local. Le connecteur hub-ingest copie le
fichier (locked si Chrome tourne) puis lit. Idempotent par (source, external_id)
ou hash sur (url, visited_at).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class BrowserHistory(Base):
    """1 visite URL dans le navigateur."""

    __tablename__ = "browser_history"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    source: Mapped[str] = mapped_column(String(20), nullable=False, default="chrome", index=True)
    """chrome | firefox | edge | brave"""

    # ID stable de la source (Chrome visit id) pour dedup
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)

    visited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Duree de visite en secondes (Chrome stocke en microseconds)
    visit_duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Type de transition Chrome (link, typed, bookmark, generated, ...) — utile pour insights
    transition: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Hash dedup naturel : sha256(url + visited_at iso)
    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<BrowserHistory {self.domain} {self.visited_at}>"
