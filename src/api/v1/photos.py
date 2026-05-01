"""Endpoint /v1/photos - ingest Google Photos metadata (Phase 3c).

On stocke uniquement les metadonnees (date, dimensions, type) - pas les bytes.
Les URLs base_url sont temporaires (~60min), refresh via API si besoin.

API : Photos Library
- mediaItems.list (page de 100, max 25k retours apres lesquels on doit
  utiliser nextPageToken)
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Photo
from src.db.session import get_db
from src.services.oauth_google import get_valid_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/photos", tags=["photos"])

PHOTOS_API = "https://photoslibrary.googleapis.com/v1"


class PhotosSyncRequest(BaseModel):
    user_email: str = Field(default="marc.richard4@gmail.com")
    max_results: int = Field(default=2000, ge=1, le=100000)


class PhotosSyncResponse(BaseModel):
    ingested: int
    updated: int
    errors: int
    duration_seconds: float


class PhotoItem(BaseModel):
    id: UUID
    media_id: str
    filename: str | None
    mime_type: str | None
    creation_time: datetime
    width: int | None
    height: int | None
    is_video: bool
    base_url: str | None
    product_url: str | None


async def _resolve_token(db: AsyncSession, user_email: str) -> str:
    for service in ("photos", "all"):
        try:
            return await get_valid_access_token(db, service=service, user_email=user_email)
        except RuntimeError:
            continue
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Pas de token Photos pour {user_email}")


def _parse_photo(item: dict[str, Any]) -> dict[str, Any]:
    meta = item.get("mediaMetadata", {})
    creation = meta.get("creationTime")
    creation_time = (
        datetime.fromisoformat(creation.replace("Z", "+00:00")) if creation else datetime.now(UTC)
    )
    photo_meta = meta.get("photo", {}) or {}
    video_meta = meta.get("video", {}) or {}
    is_video = "video" in meta
    return {
        "media_id": item["id"],
        "filename": item.get("filename"),
        "mime_type": item.get("mimeType"),
        "description": item.get("description"),
        "creation_time": creation_time,
        "width": int(meta["width"]) if meta.get("width") else None,
        "height": int(meta["height"]) if meta.get("height") else None,
        "is_video": is_video,
        "video_duration_ms": int(video_meta.get("durationMillis"))
        if video_meta.get("durationMillis")
        else None,
        "camera_make": photo_meta.get("cameraMake") or video_meta.get("cameraMake"),
        "camera_model": photo_meta.get("cameraModel") or video_meta.get("cameraModel"),
        "base_url": item.get("baseUrl"),
        "product_url": item.get("productUrl"),
    }


@router.post("/sync", response_model=PhotosSyncResponse)
async def sync_photos(
    payload: PhotosSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PhotosSyncResponse:
    start = time.monotonic()
    access_token = await _resolve_token(db, payload.user_email)

    ingested = 0
    updated = 0
    errors = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        page_token: str | None = None
        fetched = 0
        while fetched < payload.max_results:
            params: dict[str, Any] = {
                "pageSize": min(100, payload.max_results - fetched),
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                r = await client.get(
                    f"{PHOTOS_API}/mediaItems",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Photos list failed: {e.response.status_code}",
                ) from e

            items = data.get("mediaItems", [])
            for item in items:
                try:
                    parsed = _parse_photo(item)
                except Exception as e:
                    logger.warning("photo_parse_failed: id=%s err=%r", item.get("id"), e)
                    errors += 1
                    continue
                existing = (
                    await db.execute(select(Photo).where(Photo.media_id == parsed["media_id"]))
                ).scalar_one_or_none()
                if existing:
                    for k, v in parsed.items():
                        setattr(existing, k, v)
                    updated += 1
                else:
                    db.add(Photo(user_email=payload.user_email, **parsed))
                    ingested += 1
                fetched += 1
                if fetched >= payload.max_results:
                    break

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        await db.commit()

    return PhotosSyncResponse(
        ingested=ingested,
        updated=updated,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("", response_model=list[PhotoItem])
async def list_photos(
    db: Annotated[AsyncSession, Depends(get_db)],
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    is_video: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PhotoItem]:
    stmt = select(Photo).order_by(desc(Photo.creation_time))
    if since:
        stmt = stmt.where(Photo.creation_time >= since)
    if until:
        stmt = stmt.where(Photo.creation_time <= until)
    if is_video is not None:
        stmt = stmt.where(Photo.is_video == is_video)
    stmt = stmt.limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        PhotoItem(
            id=p.id,
            media_id=p.media_id,
            filename=p.filename,
            mime_type=p.mime_type,
            creation_time=p.creation_time,
            width=p.width,
            height=p.height,
            is_video=p.is_video,
            base_url=p.base_url,
            product_url=p.product_url,
        )
        for p in rows
    ]


class PhotosStats(BaseModel):
    total: int
    photos: int
    videos: int
    total_pixels: int
    by_year: list[dict[str, Any]]
    by_camera: list[dict[str, Any]]


@router.get("/stats", response_model=PhotosStats)
async def photos_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> PhotosStats:
    total = (await db.execute(select(func.count(Photo.id)))).scalar() or 0
    photos = (
        await db.execute(select(func.count(Photo.id)).where(Photo.is_video.is_(False)))
    ).scalar() or 0
    videos = (
        await db.execute(select(func.count(Photo.id)).where(Photo.is_video.is_(True)))
    ).scalar() or 0
    total_pixels = (
        await db.execute(select(func.coalesce(func.sum(Photo.width * Photo.height), 0)))
    ).scalar() or 0

    is_sqlite = "sqlite" in str(db.bind.dialect.name) if db.bind else False
    year_expr = (
        func.strftime("%Y", Photo.creation_time)
        if is_sqlite
        else func.to_char(Photo.creation_time, "YYYY")
    )
    by_year_q = (
        select(year_expr.label("year"), func.count(Photo.id).label("count"))
        .group_by("year")
        .order_by("year")
    )
    by_year = [
        {"year": r[0], "count": int(r[1])} for r in (await db.execute(by_year_q)).all() if r[0]
    ]
    by_cam_q = (
        select(Photo.camera_model, func.count(Photo.id).label("count"))
        .where(Photo.camera_model.isnot(None))
        .group_by(Photo.camera_model)
        .order_by(desc("count"))
        .limit(10)
    )
    by_camera = [{"camera": r[0], "count": int(r[1])} for r in (await db.execute(by_cam_q)).all()]
    return PhotosStats(
        total=total,
        photos=photos,
        videos=videos,
        total_pixels=int(total_pixels),
        by_year=by_year,
        by_camera=by_camera,
    )
