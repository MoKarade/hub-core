"""Modeles Steam : SteamGame (1 par jeu possede) + SteamPlaySnapshot (snapshot daily).

Steam Web API ne fournit pas les sessions individuelles (privacy). On a :
- GetOwnedGames -> playtime_forever (minutes total) + playtime_2weeks (recent)
- GetRecentlyPlayedGames -> idem mais filtres aux jeux joues recemment

Strategie : on fait des snapshots periodiques + on calcule les deltas pour
deduire les sessions ("tu as joue 90 min hier a Stardew Valley").
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class SteamGame(Base):
    """1 jeu possede par Marc sur Steam (catalogue local)."""

    __tablename__ = "steam_games"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Steam appid (unique sur la plateforme)
    appid: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    icon_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

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
        return f"<SteamGame {self.name} ({self.appid})>"


class SteamPlaySnapshot(Base):
    """1 snapshot du temps de jeu cumule pour 1 jeu a 1 instant."""

    __tablename__ = "steam_play_snapshots"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    game_id: Mapped[UUID] = mapped_column(
        ForeignKey("steam_games.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Minutes cumulees totales (depuis l'achat)
    playtime_forever_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Minutes derniers 2 semaines (champ recent_2weeks Steam API)
    playtime_2weeks_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Last played timestamp (Steam rstime_last_played)
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<SteamPlaySnapshot game={self.game_id} "
            f"total={self.playtime_forever_min}min @{self.snapshot_at}>"
        )
