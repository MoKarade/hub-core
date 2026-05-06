"""Endpoints /v1/steam/* — gaming hub via Steam Web API.

Setup Marc :
1. Cree une API key gratuite sur https://steamcommunity.com/dev/apikey
   (besoin d'un compte Steam, prend 30 sec)
2. Trouve son SteamID64 sur https://steamid.io/ (URL profil Steam contient l'ID)
3. Renseigne STEAM_API_KEY + STEAM_USER_ID dans hub-core/.env
4. POST /v1/steam/sync (manuel ou via cron _job_steam 6h auto)

L'API Steam ne donne pas les sessions individuelles — on snapshot le total
playtime + last_played + playtime_2weeks puis on calcule les deltas pour
deduire les sessions ("tu as joue 90 min hier").
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.db.models import SteamGame, SteamPlaySnapshot
from src.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/steam", tags=["steam"])

STEAM_API = "https://api.steampowered.com"


# ─────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────


class SyncResponse(BaseModel):
    games_in_library: int
    games_played_2w: int
    snapshots_created: int
    duration_seconds: float


class GameOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    appid: int
    name: str
    icon_url: str | None
    playtime_forever_min: int = 0
    playtime_2weeks_min: int = 0
    last_played_at: datetime | None = None


class SessionDelta(BaseModel):
    """Delta entre 2 snapshots = session deduite."""

    appid: int
    name: str
    started_around: datetime
    ended_around: datetime
    duration_min: int


class StatsResponse(BaseModel):
    total_games: int
    total_playtime_hours: float
    games_played_2w: int
    top_games: list[dict[str, Any]]
    last_played: dict[str, Any] | None


# ─────────────────────────────────────────────────────────────────────────
# Steam API helpers
# ─────────────────────────────────────────────────────────────────────────


async def _get_owned_games(client: httpx.AsyncClient, settings: Settings) -> list[dict[str, Any]]:
    r = await client.get(
        f"{STEAM_API}/IPlayerService/GetOwnedGames/v1/",
        params={
            "key": settings.steam_api_key,
            "steamid": settings.steam_user_id,
            "include_appinfo": "true",
            "include_played_free_games": "true",
            "format": "json",
        },
    )
    r.raise_for_status()
    return r.json().get("response", {}).get("games", []) or []


async def _get_recently_played(
    client: httpx.AsyncClient, settings: Settings
) -> list[dict[str, Any]]:
    r = await client.get(
        f"{STEAM_API}/IPlayerService/GetRecentlyPlayedGames/v1/",
        params={
            "key": settings.steam_api_key,
            "steamid": settings.steam_user_id,
            "format": "json",
        },
    )
    r.raise_for_status()
    return r.json().get("response", {}).get("games", []) or []


def _icon_url(appid: int, icon_hash: str | None) -> str | None:
    if not icon_hash:
        return None
    return (
        f"https://media.steampowered.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg"
    )


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────


@router.post("/sync", response_model=SyncResponse)
async def sync_steam(
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SyncResponse:
    """Pull library Steam + snapshot playtime + dedup recent_2weeks."""
    if not settings.steam_api_key or not settings.steam_user_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "STEAM_API_KEY / STEAM_USER_ID non configures dans .env. "
            "Cf. https://steamcommunity.com/dev/apikey + https://steamid.io/",
        )

    start = time.monotonic()
    snapshot_at = datetime.now(UTC)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            owned = await _get_owned_games(client, settings)
        except httpx.HTTPStatusError as exc:
            logger.error("steam_owned_failed status=%d", exc.response.status_code)
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Steam GetOwnedGames a echoue : {exc.response.status_code}. "
                "Verifie ta steam_user_id et que ton profil est public.",
            ) from exc

        try:
            recent_2w = {g["appid"]: g for g in await _get_recently_played(client, settings)}
        except httpx.HTTPStatusError:
            recent_2w = {}

    if not owned:
        return SyncResponse(
            games_in_library=0,
            games_played_2w=0,
            snapshots_created=0,
            duration_seconds=round(time.monotonic() - start, 2),
        )

    # Upsert SteamGame + snapshot UNIQUEMENT pour les jeux avec playtime > 0
    appids = [g["appid"] for g in owned]
    existing_games = (
        (await db.execute(select(SteamGame).where(SteamGame.appid.in_(appids)))).scalars().all()
    )
    by_appid = {g.appid: g for g in existing_games}

    snapshots_created = 0
    games_played_2w = 0

    for g in owned:
        appid = g["appid"]
        name = (g.get("name") or "?")[:300]
        icon = _icon_url(appid, g.get("img_icon_url"))

        game = by_appid.get(appid)
        if game is None:
            game = SteamGame(appid=appid, name=name, icon_url=icon)
            db.add(game)
            await db.flush()
            by_appid[appid] = game
        else:
            # Update name / icon si change
            game.name = name
            if icon:
                game.icon_url = icon

        # Snapshot uniquement si jeu joue (playtime_forever > 0) pour eviter
        # de spammer la table avec les 1000 jeux Humble Bundle jamais lances
        playtime_total = int(g.get("playtime_forever", 0) or 0)
        if playtime_total <= 0:
            continue

        recent = recent_2w.get(appid, {})
        playtime_2w = int(recent.get("playtime_2weeks", 0) or 0)
        last_played_ts = g.get("rtime_last_played", 0) or 0
        last_played = datetime.fromtimestamp(last_played_ts, tz=UTC) if last_played_ts > 0 else None

        # Dedup : si on a deja un snapshot avec exactement le meme playtime
        # (rien n'a change), on skip pour eviter les doublons
        last_snap = (
            await db.execute(
                select(SteamPlaySnapshot)
                .where(SteamPlaySnapshot.game_id == game.id)
                .order_by(desc(SteamPlaySnapshot.snapshot_at))
                .limit(1)
            )
        ).scalar_one_or_none()

        if last_snap and last_snap.playtime_forever_min == playtime_total:
            continue  # rien n'a change depuis le dernier snapshot

        db.add(
            SteamPlaySnapshot(
                game_id=game.id,
                snapshot_at=snapshot_at,
                playtime_forever_min=playtime_total,
                playtime_2weeks_min=playtime_2w,
                last_played_at=last_played,
            )
        )
        snapshots_created += 1
        if playtime_2w > 0:
            games_played_2w += 1

    await db.commit()

    return SyncResponse(
        games_in_library=len(owned),
        games_played_2w=games_played_2w,
        snapshots_created=snapshots_created,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("/games", response_model=list[GameOut])
async def list_games(
    db: Annotated[AsyncSession, Depends(get_db)],
    only_played: bool = True,
    limit: int = 100,
) -> list[GameOut]:
    """Liste les jeux + last snapshot data (playtime cumule)."""
    games = (
        (await db.execute(select(SteamGame).order_by(SteamGame.name).limit(limit))).scalars().all()
    )

    out: list[GameOut] = []
    for g in games:
        last = (
            await db.execute(
                select(SteamPlaySnapshot)
                .where(SteamPlaySnapshot.game_id == g.id)
                .order_by(desc(SteamPlaySnapshot.snapshot_at))
                .limit(1)
            )
        ).scalar_one_or_none()

        playtime_total = last.playtime_forever_min if last else 0
        if only_played and playtime_total <= 0:
            continue

        out.append(
            GameOut(
                id=g.id,
                appid=g.appid,
                name=g.name,
                icon_url=g.icon_url,
                playtime_forever_min=playtime_total,
                playtime_2weeks_min=last.playtime_2weeks_min if last else 0,
                last_played_at=last.last_played_at if last else None,
            )
        )

    out.sort(key=lambda x: -x.playtime_forever_min)
    return out


@router.get("/sessions", response_model=list[SessionDelta])
async def list_sessions(
    db: Annotated[AsyncSession, Depends(get_db)],
    since_days: int = 30,
    limit: int = 50,
) -> list[SessionDelta]:
    """Deduit les sessions de jeu via les deltas entre snapshots successifs.

    Pour chaque jeu, on parcourt ses snapshots ordonnes par date et on emet
    1 SessionDelta a chaque fois que playtime_forever augmente.
    """
    cutoff = datetime.now(UTC) - timedelta(days=since_days)

    # Pour chaque jeu : tous les snapshots depuis cutoff
    games = (await db.execute(select(SteamGame))).scalars().all()
    sessions: list[SessionDelta] = []

    for game in games:
        snaps = (
            (
                await db.execute(
                    select(SteamPlaySnapshot)
                    .where(SteamPlaySnapshot.game_id == game.id)
                    .where(SteamPlaySnapshot.snapshot_at >= cutoff)
                    .order_by(SteamPlaySnapshot.snapshot_at.asc())
                )
            )
            .scalars()
            .all()
        )
        if len(snaps) < 2:
            continue
        for prev, curr in zip(snaps[:-1], snaps[1:], strict=False):
            delta = curr.playtime_forever_min - prev.playtime_forever_min
            if delta > 0:
                sessions.append(
                    SessionDelta(
                        appid=game.appid,
                        name=game.name,
                        started_around=prev.snapshot_at,
                        ended_around=curr.snapshot_at,
                        duration_min=delta,
                    )
                )

    sessions.sort(key=lambda s: s.ended_around, reverse=True)
    return sessions[:limit]


@router.get("/stats", response_model=StatsResponse)
async def steam_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StatsResponse:
    """Stats globales : total heures jouees, jeux joues 2 semaines, top 5."""
    games = (await db.execute(select(SteamGame))).scalars().all()
    total_games = len(games)

    if not games:
        return StatsResponse(
            total_games=0,
            total_playtime_hours=0,
            games_played_2w=0,
            top_games=[],
            last_played=None,
        )

    # Pour chaque jeu, son dernier snapshot
    top: list[dict[str, Any]] = []
    total_minutes = 0
    games_2w = 0
    last_played: dict[str, Any] | None = None
    last_played_dt: datetime | None = None

    for g in games:
        last = (
            await db.execute(
                select(SteamPlaySnapshot)
                .where(SteamPlaySnapshot.game_id == g.id)
                .order_by(desc(SteamPlaySnapshot.snapshot_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if not last or last.playtime_forever_min <= 0:
            continue
        total_minutes += last.playtime_forever_min
        if last.playtime_2weeks_min > 0:
            games_2w += 1
        top.append(
            {
                "appid": g.appid,
                "name": g.name,
                "playtime_min": last.playtime_forever_min,
                "playtime_2weeks_min": last.playtime_2weeks_min,
                "icon_url": g.icon_url,
            }
        )
        if last.last_played_at:
            last_at = (
                last.last_played_at
                if last.last_played_at.tzinfo
                else last.last_played_at.replace(tzinfo=UTC)
            )
            if last_played_dt is None or last_at > last_played_dt:
                last_played_dt = last_at
                last_played = {
                    "appid": g.appid,
                    "name": g.name,
                    "last_played_at": last_at.isoformat(),
                    "icon_url": g.icon_url,
                }

    top.sort(key=lambda x: -x["playtime_min"])

    return StatsResponse(
        total_games=total_games,
        total_playtime_hours=round(total_minutes / 60, 1),
        games_played_2w=games_2w,
        top_games=top[:10],
        last_played=last_played,
    )


@router.get("/status")
async def steam_status(
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Etat du connecteur."""
    n_games = (await db.execute(select(func.count()).select_from(SteamGame))).scalar_one() or 0
    n_snaps = (
        await db.execute(select(func.count()).select_from(SteamPlaySnapshot))
    ).scalar_one() or 0
    return {
        "configured": bool(settings.steam_api_key and settings.steam_user_id),
        "games_in_db": int(n_games),
        "snapshots_in_db": int(n_snaps),
    }
