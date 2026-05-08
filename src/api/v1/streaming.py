"""Endpoints /v1/streaming/* — Trakt.tv hub (Phase 6).

Trakt.tv agrege Netflix / Prime / Disney+ / Crunchyroll / Plex / Jellyfin / etc.
Marc installe la browser extension Trakt qui scrobble automatiquement ses
visionnages. Le hub pull la history via OAuth 2.0.

Setup Marc :
1. Creer une app sur https://trakt.tv/oauth/applications
2. Renseigner trakt_client_id + trakt_client_secret dans .env de hub-core
3. Aller sur /v1/streaming/connect → redirige vers Trakt → autoriser
4. Trakt callback /v1/streaming/oauth/callback → stocke token chiffre en DB
5. POST /v1/streaming/sync (manuel ou via cron _job_streaming) pull history

Pas de streaming en direct via Trakt API (pas dispo). Marc doit faire le sync
periodiquement (cron 12h par defaut) ou manuellement.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.core.crypto import decrypt_str, encrypt_str
from src.db.models import StreamingActivity
from src.db.models.oauth_token import OAuthToken
from src.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/streaming", tags=["streaming"])
_OWNER_EMAIL: str = get_settings().hub_owner_email

TRAKT_BASE = "https://api.trakt.tv"
TRAKT_AUTH_URL = "https://api.trakt.tv/oauth/authorize"
TRAKT_TOKEN_URL = "https://api.trakt.tv/oauth/token"


# ─────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────


class SyncRequest(BaseModel):
    user_email: str = Field(default=_OWNER_EMAIL)
    days_back: int = Field(default=30, ge=1, le=3650)
    max_results: int = Field(default=1000, ge=1, le=10000)


class SyncResponse(BaseModel):
    ingested: int
    updated: int
    errors: int
    duration_seconds: float


class StreamingActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    external_id: str
    item_type: str
    title: str
    year: int | None
    show_title: str | None
    season: int | None
    episode: int | None
    runtime_minutes: int | None
    genres: str | None
    platform: str | None
    watched_at: datetime


class StreamingStats(BaseModel):
    total_activities: int
    total_movies: int
    total_episodes: int
    total_runtime_hours: float
    top_shows: list[dict[str, Any]]  # [{show_title, count}]
    by_month: list[dict[str, Any]]  # [{month: '2026-04', count}]


# ─────────────────────────────────────────────────────────────────────────
# Helpers OAuth
# ─────────────────────────────────────────────────────────────────────────


async def _load_trakt_token(db: AsyncSession, user_email: str) -> OAuthToken:
    """Recupere le token Trakt pour un user. 401 si pas de token."""
    stmt = select(OAuthToken).where(
        OAuthToken.provider == "trakt",
        OAuthToken.service == "trakt",
        OAuthToken.user_email == user_email,
        OAuthToken.revoked_at.is_(None),
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"Pas de token Trakt pour {user_email}. "
            "Lance GET /v1/streaming/connect pour autoriser.",
        )
    return row


async def _get_valid_trakt_access_token(
    db: AsyncSession,
    settings: Settings,
    user_email: str,
) -> str:
    """Retourne un access_token Trakt valide. Refresh auto si expire."""
    row = await _load_trakt_token(db, user_email)

    if not row.is_expired:
        return decrypt_str(row.access_token_encrypted)

    # Token expire — refresh
    if row.refresh_token_encrypted is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Token Trakt expire et pas de refresh_token. Re-autorise via /v1/streaming/connect.",
        )

    refresh_token = decrypt_str(row.refresh_token_encrypted)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            TRAKT_TOKEN_URL,
            json={
                "refresh_token": refresh_token,
                "client_id": settings.trakt_client_id,
                "client_secret": settings.trakt_client_secret,
                "redirect_uri": settings.trakt_redirect_uri,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code >= 400:
            logger.warning("trakt_refresh_failed status=%s body=%s", r.status_code, r.text[:200])
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Refresh Trakt a echoue. Re-autorise via /v1/streaming/connect.",
            )
        data = r.json()

    new_access = data["access_token"]
    new_refresh = data.get("refresh_token", refresh_token)
    expires_in = data.get("expires_in", 7776000)  # 90j Trakt default

    row.access_token_encrypted = encrypt_str(new_access)
    row.refresh_token_encrypted = encrypt_str(new_refresh)
    row.token_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    row.last_refreshed_at = datetime.now(UTC)
    await db.commit()
    return new_access


# ─────────────────────────────────────────────────────────────────────────
# OAuth flow
# ─────────────────────────────────────────────────────────────────────────


@router.get("/connect")
async def connect_trakt(settings: Annotated[Settings, Depends(get_settings)]) -> RedirectResponse:
    """Redirige vers Trakt pour autoriser le hub.

    Le flow continue vers /oauth/callback qui echange le code contre un token.
    """
    if not settings.trakt_client_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "TRAKT_CLIENT_ID non configure dans .env. "
            "Cree une app sur https://trakt.tv/oauth/applications.",
        )

    auth_url = (
        f"{TRAKT_AUTH_URL}?response_type=code"
        f"&client_id={settings.trakt_client_id}"
        f"&redirect_uri={settings.trakt_redirect_uri}"
    )
    return RedirectResponse(url=auth_url, status_code=status.HTTP_302_FOUND)


@router.get("/oauth/callback")
async def trakt_callback(
    code: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    user_email: str = _OWNER_EMAIL,
) -> dict[str, str]:
    """Callback OAuth Trakt : echange le code contre un access_token + stocke en DB."""
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "TRAKT_CLIENT_ID / TRAKT_CLIENT_SECRET non configures.",
        )

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            TRAKT_TOKEN_URL,
            json={
                "code": code,
                "client_id": settings.trakt_client_id,
                "client_secret": settings.trakt_client_secret,
                "redirect_uri": settings.trakt_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if r.status_code >= 400:
            logger.warning(
                "trakt_token_exchange_failed status=%s body=%s", r.status_code, r.text[:200]
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Echange token Trakt a echoue : {r.status_code}",
            )
        data = r.json()

    access = data["access_token"]
    refresh = data.get("refresh_token")
    expires_in = data.get("expires_in", 7776000)
    scopes = data.get("scope", "").split() if data.get("scope") else []

    # Upsert OAuthToken
    stmt = select(OAuthToken).where(
        OAuthToken.provider == "trakt",
        OAuthToken.service == "trakt",
        OAuthToken.user_email == user_email,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    if existing:
        existing.access_token_encrypted = encrypt_str(access)
        if refresh:
            existing.refresh_token_encrypted = encrypt_str(refresh)
        existing.token_expires_at = expires_at
        existing.scopes = scopes
        existing.revoked_at = None
        existing.last_refreshed_at = datetime.now(UTC)
    else:
        db.add(
            OAuthToken(
                provider="trakt",
                service="trakt",
                user_email=user_email,
                access_token_encrypted=encrypt_str(access),
                refresh_token_encrypted=(encrypt_str(refresh) if refresh else None),
                token_expires_at=expires_at,
                scopes=scopes,
                token_type="Bearer",
            )
        )
    await db.commit()
    return {"status": "connected", "user_email": user_email}


# ─────────────────────────────────────────────────────────────────────────
# Sync history
# ─────────────────────────────────────────────────────────────────────────


@router.post("/sync", response_model=SyncResponse)
async def sync_streaming(
    payload: SyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SyncResponse:
    """Pull la history Trakt depuis days_back jours et upsert en DB."""
    start = time.monotonic()
    access_token = await _get_valid_trakt_access_token(db, settings, payload.user_email)

    start_at = (datetime.now(UTC) - timedelta(days=payload.days_back)).isoformat()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "trakt-api-version": "2",
        "trakt-api-key": settings.trakt_client_id,
        "Content-Type": "application/json",
    }

    ingested = 0
    updated = 0
    errors = 0

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        # Trakt history endpoint, paginé
        page = 1
        while ingested + updated < payload.max_results:
            try:
                r = await client.get(
                    f"{TRAKT_BASE}/sync/history",
                    params={
                        "start_at": start_at,
                        "limit": min(100, payload.max_results - (ingested + updated)),
                        "page": page,
                    },
                )
                r.raise_for_status()
                items = r.json()
            except httpx.HTTPStatusError as exc:
                logger.error("trakt_history_failed status=%s", exc.response.status_code)
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Trakt history a echoue : {exc.response.status_code}",
                ) from exc

            if not items:
                break

            # Batch fetch existing pour eviter N+1
            external_ids = [str(it["id"]) for it in items if it.get("id")]
            existing_rows = (
                (
                    await db.execute(
                        select(StreamingActivity).where(
                            StreamingActivity.external_id.in_(external_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            existing_map = {r.external_id: r for r in existing_rows}

            for item in items:
                try:
                    parsed = _parse_trakt_history_item(item)
                except Exception as e:
                    logger.warning("trakt_parse_failed id=%s err=%r", item.get("id"), e)
                    errors += 1
                    continue

                existing = existing_map.get(parsed["external_id"])
                if existing:
                    for k, v in parsed.items():
                        setattr(existing, k, v)
                    updated += 1
                else:
                    db.add(StreamingActivity(**parsed))
                    ingested += 1

            page += 1
            if len(items) < 100:
                break

        await db.commit()

    duration = round(time.monotonic() - start, 2)
    logger.info(
        "trakt_sync_done ingested=%d updated=%d errors=%d duration=%.2fs",
        ingested,
        updated,
        errors,
        duration,
    )
    return SyncResponse(
        ingested=ingested, updated=updated, errors=errors, duration_seconds=duration
    )


def _parse_trakt_history_item(item: dict[str, Any]) -> dict[str, Any]:
    """Parse 1 item Trakt history → dict pret pour DB."""
    item_type = item.get("type", "movie")  # 'movie' | 'episode'
    watched_at_str = item["watched_at"]
    watched_at = datetime.fromisoformat(watched_at_str.replace("Z", "+00:00"))

    base = {
        "external_id": str(item["id"]),
        "source": "trakt",
        "item_type": item_type,
        "watched_at": watched_at,
    }

    if item_type == "episode":
        ep = item.get("episode", {})
        show = item.get("show", {})
        base.update(
            {
                "title": ep.get("title", "?"),
                "show_title": show.get("title"),
                "year": show.get("year"),
                "season": ep.get("season"),
                "episode": ep.get("number"),
                "runtime_minutes": ep.get("runtime"),
            }
        )
    else:  # movie
        movie = item.get("movie", {})
        base.update(
            {
                "title": movie.get("title", "?"),
                "year": movie.get("year"),
                "runtime_minutes": movie.get("runtime"),
                "show_title": None,
                "season": None,
                "episode": None,
            }
        )

    # Genres si presents
    genres_list = item.get("show", {}).get("genres") or item.get("movie", {}).get("genres") or []
    if genres_list:
        base["genres"] = ",".join(genres_list)
    return base


# ─────────────────────────────────────────────────────────────────────────
# Read endpoints
# ─────────────────────────────────────────────────────────────────────────


@router.get("/history", response_model=list[StreamingActivityOut])
async def list_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    item_type: str | None = None,  # 'movie' | 'episode'
    since_days: int | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[StreamingActivityOut]:
    """Liste la history streaming, plus recente d'abord."""
    stmt = select(StreamingActivity).order_by(desc(StreamingActivity.watched_at))
    if item_type:
        stmt = stmt.where(StreamingActivity.item_type == item_type)
    if since_days:
        cutoff = datetime.now(UTC) - timedelta(days=since_days)
        stmt = stmt.where(StreamingActivity.watched_at >= cutoff)
    stmt = stmt.limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [StreamingActivityOut.model_validate(r) for r in rows]


@router.get("/stats", response_model=StreamingStats)
async def streaming_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingStats:
    """Stats globales : totaux, top shows, repartition mensuelle."""
    total = (
        await db.execute(select(func.count()).select_from(StreamingActivity))
    ).scalar_one() or 0
    movies = (
        await db.execute(
            select(func.count())
            .select_from(StreamingActivity)
            .where(StreamingActivity.item_type == "movie")
        )
    ).scalar_one() or 0
    episodes = (
        await db.execute(
            select(func.count())
            .select_from(StreamingActivity)
            .where(StreamingActivity.item_type == "episode")
        )
    ).scalar_one() or 0

    total_runtime_min = (
        await db.execute(select(func.sum(StreamingActivity.runtime_minutes)))
    ).scalar() or 0

    # Top shows (episodes only)
    top_shows_q = (
        select(StreamingActivity.show_title, func.count().label("c"))
        .where(StreamingActivity.show_title.is_not(None))
        .group_by(StreamingActivity.show_title)
        .order_by(desc("c"))
        .limit(10)
    )
    top_rows = (await db.execute(top_shows_q)).all()
    top_shows = [{"show_title": r[0], "count": r[1]} for r in top_rows]

    # By month (12 derniers)
    cutoff_12mo = datetime.now(UTC) - timedelta(days=365)
    by_month_q = (
        select(
            func.strftime("%Y-%m", StreamingActivity.watched_at).label("ym"),
            func.count().label("c"),
        )
        .where(StreamingActivity.watched_at >= cutoff_12mo)
        .group_by("ym")
        .order_by("ym")
    )
    try:
        by_month_rows = (await db.execute(by_month_q)).all()
        by_month = [{"month": r[0], "count": r[1]} for r in by_month_rows]
    except Exception:
        # Postgres : strftime n'existe pas, fallback to_char
        by_month = []  # TODO Postgres-compat (tronqué pour dev SQLite-first)

    return StreamingStats(
        total_activities=total,
        total_movies=movies,
        total_episodes=episodes,
        total_runtime_hours=round(total_runtime_min / 60, 1) if total_runtime_min else 0,
        top_shows=top_shows,
        by_month=by_month,
    )


@router.get("/status")
async def streaming_status(
    db: Annotated[AsyncSession, Depends(get_db)],
    user_email: str = _OWNER_EMAIL,
) -> dict[str, Any]:
    """Etat du connecteur : token present, expire, etc."""
    stmt = select(OAuthToken).where(
        OAuthToken.provider == "trakt",
        OAuthToken.service == "trakt",
        OAuthToken.user_email == user_email,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return {"connected": False, "reason": "no token"}
    return {
        "connected": not row.is_revoked and row.is_usable,
        "expires_at": row.token_expires_at.isoformat(),
        "is_expired": row.is_expired,
        "has_refresh": row.refresh_token_encrypted is not None,
        "last_refreshed_at": row.last_refreshed_at.isoformat() if row.last_refreshed_at else None,
        "scopes": row.scopes,
    }
