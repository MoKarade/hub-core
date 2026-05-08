"""Endpoint /v1/youtube - ingest YouTube activity (Phase 6)."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.db.models import YouTubeActivity
from src.db.session import get_db
from src.services.oauth_google import get_valid_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/youtube", tags=["youtube"])
_OWNER_EMAIL: str = get_settings().hub_owner_email

YT_API = "https://www.googleapis.com/youtube/v3"


class YTSyncRequest(BaseModel):
    user_email: str = Field(default=_OWNER_EMAIL)
    days_back: int = Field(default=90, ge=1, le=3650)
    max_results: int = Field(default=500, ge=1, le=10000)


class YTSyncResponse(BaseModel):
    activities_ingested: int
    activities_updated: int
    errors: int
    duration_seconds: float


class YTActivityItem(BaseModel):
    id: UUID
    activity_id: str
    activity_type: str
    video_id: str | None
    video_title: str | None
    channel_title: str | None
    thumbnail_url: str | None
    published_at: datetime


async def _resolve_token(db: AsyncSession, user_email: str) -> str:
    for service in ("youtube", "all"):
        try:
            return await get_valid_access_token(db, service=service, user_email=user_email)
        except RuntimeError:
            continue
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Pas de token YouTube pour {user_email}")


@router.post("/sync", response_model=YTSyncResponse)
async def sync_youtube(
    payload: YTSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> YTSyncResponse:
    start = time.monotonic()
    access_token = await _resolve_token(db, payload.user_email)

    published_after = (datetime.now(UTC) - timedelta(days=payload.days_back)).isoformat()

    ingested = 0
    updated = 0
    errors = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        page_token: str | None = None
        fetched = 0
        while fetched < payload.max_results:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails",
                "mine": "true",
                "maxResults": min(50, payload.max_results - fetched),
                "publishedAfter": published_after,
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                r = await client.get(
                    f"{YT_API}/activities",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"YouTube activities failed: {e.response.status_code} {e.response.text[:200]}",
                ) from e

            for item in data.get("items", []):
                snippet = item.get("snippet", {}) or {}
                content_details = item.get("contentDetails", {}) or {}

                activity_type = snippet.get("type", "unknown")

                # Le video_id depend du type d'activite
                video_id = None
                upload_info = content_details.get("upload", {}) or {}
                like_info = content_details.get("like", {}) or {}
                fav_info = content_details.get("favorite", {}) or {}
                video_id = (
                    upload_info.get("videoId")
                    or like_info.get("resourceId", {}).get("videoId")
                    or fav_info.get("resourceId", {}).get("videoId")
                )

                published = snippet.get("publishedAt")
                if not published:
                    errors += 1
                    continue
                published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))

                thumbs = snippet.get("thumbnails", {}) or {}
                thumb = thumbs.get("medium", {}) or thumbs.get("default", {}) or {}

                parsed = {
                    "activity_id": item["id"],
                    "activity_type": activity_type,
                    "video_id": video_id,
                    "video_title": snippet.get("title"),
                    "channel_id": snippet.get("channelId"),
                    "channel_title": snippet.get("channelTitle"),
                    "description": snippet.get("description"),
                    "thumbnail_url": thumb.get("url"),
                    "published_at": published_at,
                }
                existing = (
                    await db.execute(
                        select(YouTubeActivity).where(
                            YouTubeActivity.activity_id == parsed["activity_id"]
                        )
                    )
                ).scalar_one_or_none()
                if existing:
                    for k, v in parsed.items():
                        setattr(existing, k, v)
                    updated += 1
                else:
                    db.add(YouTubeActivity(user_email=payload.user_email, **parsed))
                    ingested += 1
                fetched += 1
                if fetched >= payload.max_results:
                    break

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        await db.commit()

    return YTSyncResponse(
        activities_ingested=ingested,
        activities_updated=updated,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("/activities", response_model=list[YTActivityItem])
async def list_activities(
    db: Annotated[AsyncSession, Depends(get_db)],
    activity_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[YTActivityItem]:
    stmt = select(YouTubeActivity).order_by(desc(YouTubeActivity.published_at))
    if activity_type:
        stmt = stmt.where(YouTubeActivity.activity_type == activity_type)
    stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        YTActivityItem(
            id=a.id,
            activity_id=a.activity_id,
            activity_type=a.activity_type,
            video_id=a.video_id,
            video_title=a.video_title,
            channel_title=a.channel_title,
            thumbnail_url=a.thumbnail_url,
            published_at=a.published_at,
        )
        for a in rows
    ]


class YTStats(BaseModel):
    total: int
    by_type: list[dict[str, Any]]
    top_channels: list[dict[str, Any]]


@router.get("/stats", response_model=YTStats)
async def youtube_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> YTStats:
    total = (await db.execute(select(func.count(YouTubeActivity.id)))).scalar() or 0
    by_type_q = (
        select(YouTubeActivity.activity_type, func.count(YouTubeActivity.id).label("count"))
        .group_by(YouTubeActivity.activity_type)
        .order_by(desc("count"))
    )
    by_type = [{"type": r[0], "count": int(r[1])} for r in (await db.execute(by_type_q)).all()]
    top_q = (
        select(YouTubeActivity.channel_title, func.count(YouTubeActivity.id).label("count"))
        .where(YouTubeActivity.channel_title.isnot(None))
        .group_by(YouTubeActivity.channel_title)
        .order_by(desc("count"))
        .limit(15)
    )
    top_channels = [{"channel": r[0], "count": int(r[1])} for r in (await db.execute(top_q)).all()]
    return YTStats(total=total, by_type=by_type, top_channels=top_channels)
