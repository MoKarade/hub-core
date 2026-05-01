"""Endpoint /v1/photos - ingest Google Photos via Picker API (Phase 3c).

CONTEXTE 2025 : Google a depercier photoslibrary.readonly pour les nouvelles
apps. Les apps creees apres mars 2025 recoivent 403 sur mediaItems.list,
meme avec scope correct. Solution officielle Google = Picker API.

Workflow Picker :
1. POST /v1/photos/picker/start -> cree session, retourne pickerUri
2. User redirige vers pickerUri (UI Google), pick photos, click Done
3. Frontend poll /v1/photos/picker/status/{session_id} jusqu'a mediaItemsSet=true
4. POST /v1/photos/picker/import/{session_id} -> recupere les mediaItems picks
   et stocke en DB (idempotent par media_id)

Avantages :
- Marche pour les nouvelles apps sans Google Verification
- Free, scopes auto-approves
- User controle exactement quoi importer (vie privee respectee)

Ancien endpoint /v1/photos/sync (Library API) garde pour les apps verifiees
mais aboutit a 403 sur les nouvelles - on log puis recommend Picker.
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
PICKER_API = "https://photospicker.googleapis.com/v1"


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
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None
    camera_make: str | None = None
    camera_model: str | None = None


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
            # Parse tous les items, puis batch-fetch existants en 1 query (vs N+1)
            parsed_list: list[dict[str, Any]] = []
            for item in items:
                try:
                    parsed_list.append(_parse_photo(item))
                except Exception as e:
                    logger.warning("photo_parse_failed: id=%s err=%r", item.get("id"), e)
                    errors += 1
            if not parsed_list:
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
                continue

            ids = [p["media_id"] for p in parsed_list]
            existing_rows = (
                (await db.execute(select(Photo).where(Photo.media_id.in_(ids)))).scalars().all()
            )
            existing_map = {p.media_id: p for p in existing_rows}

            for parsed in parsed_list:
                existing = existing_map.get(parsed["media_id"])
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
            latitude=p.latitude,
            longitude=p.longitude,
            location_name=p.location_name,
            camera_make=p.camera_make,
            camera_model=p.camera_model,
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


class GpsEnrichRequest(BaseModel):
    user_email: str = Field(default="marc.richard4@gmail.com")
    max_photos: int = Field(default=100, ge=1, le=10000)
    do_geocode: bool = Field(default=True, description="Reverse geocode chaque GPS via Nominatim")


class GpsEnrichResponse(BaseModel):
    processed: int
    with_gps: int
    geocoded: int
    errors: int
    duration_seconds: float


@router.post("/enrich-gps", response_model=GpsEnrichResponse)
async def enrich_gps(
    payload: GpsEnrichRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GpsEnrichResponse:
    """Pour chaque photo SANS GPS, telecharge les bytes + parse EXIF + extrait GPS.
    Optionnellement reverse geocode pour location_name humain.
    """
    import asyncio as aio
    import time

    from src.services.photo_gps import (
        download_photo_bytes,
        extract_gps_from_bytes,
        reverse_geocode,
    )

    start = time.monotonic()
    access_token = await _resolve_token(db, payload.user_email)

    # Photos sans lat/lng, ayant un base_url
    rows = (
        (
            await db.execute(
                select(Photo)
                .where(
                    Photo.user_email == payload.user_email,
                    Photo.latitude.is_(None),
                    Photo.base_url.isnot(None),
                )
                .limit(payload.max_photos)
            )
        )
        .scalars()
        .all()
    )

    processed = 0
    with_gps = 0
    geocoded = 0
    errors = 0

    for photo in rows:
        processed += 1
        try:
            bytes_data = await download_photo_bytes(photo.base_url, access_token)
            lat, lng, alt, exif = extract_gps_from_bytes(bytes_data)
            photo.exif_data = exif if exif else None
            if lat is not None and lng is not None:
                photo.latitude = lat
                photo.longitude = lng
                photo.altitude_m = alt
                with_gps += 1
                if payload.do_geocode:
                    # Rate limit Nominatim : 1 req/sec - on attend 1.1s entre chaque
                    name = await reverse_geocode(lat, lng)
                    if name:
                        photo.location_name = name
                        geocoded += 1
                    await aio.sleep(1.1)
        except Exception as e:
            logger.warning("enrich_gps_photo_failed: id=%s err=%r", photo.media_id, e)
            errors += 1

    await db.commit()

    return GpsEnrichResponse(
        processed=processed,
        with_gps=with_gps,
        geocoded=geocoded,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("/thumb/{media_id}")
async def photo_thumbnail(
    media_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    size: Annotated[int, Query(ge=64, le=2048)] = 200,
):
    """Proxy authentifie pour les thumbnails Picker API (qui necessitent Bearer token).
    Le browser ne peut pas charger directement Picker baseUrl, on doit proxifier.
    """
    from fastapi.responses import Response

    photo = (await db.execute(select(Photo).where(Photo.media_id == media_id))).scalar_one_or_none()
    if not photo or not photo.base_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo introuvable")

    access_token = await _resolve_token(db, photo.user_email)
    # Picker baseUrl + suffix pour resize : =w{size}-h{size}-c (crop centered)
    url = f"{photo.base_url}=w{size}-h{size}-c"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Si baseUrl expire (1h), il faut re-pick via Picker API.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Thumbnail load failed: {e.response.status_code} (baseUrl peut-etre expire)",
        ) from e

    return Response(
        content=r.content,
        media_type=r.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


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


# ============================================================================
# Picker API (solution 2025 - Library API restreinte aux apps verifiees)
# ============================================================================


class PickerStartResponse(BaseModel):
    session_id: str
    picker_uri: str
    """URL ou rediriger l'user pour qu'il pick ses photos."""

    expire_time: str | None = None


class PickerStatusResponse(BaseModel):
    session_id: str
    media_items_set: bool
    """True quand l'user a fini de picker."""

    picker_uri: str | None = None
    expire_time: str | None = None


class PickerImportResponse(BaseModel):
    session_id: str
    ingested: int
    updated: int
    errors: int
    duration_seconds: float


@router.post("/picker/start", response_model=PickerStartResponse)
async def picker_start(
    db: Annotated[AsyncSession, Depends(get_db)],
    user_email: Annotated[str, Query()] = "marc.richard4@gmail.com",
) -> PickerStartResponse:
    """Cree une session Picker. L'user redirige vers picker_uri pour pick."""
    access_token = await _resolve_token(db, user_email)
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(
                f"{PICKER_API}/sessions",
                headers={"Authorization": f"Bearer {access_token}"},
                json={},
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Picker session create failed: {e.response.status_code} {e.response.text[:200]}",
            ) from e
    return PickerStartResponse(
        session_id=data["id"],
        picker_uri=data["pickerUri"],
        expire_time=data.get("expireTime"),
    )


@router.get("/picker/status/{session_id}", response_model=PickerStatusResponse)
async def picker_status(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_email: Annotated[str, Query()] = "marc.richard4@gmail.com",
) -> PickerStatusResponse:
    """Poll le status d'une session : True quand user a fini de picker."""
    access_token = await _resolve_token(db, user_email)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(
                f"{PICKER_API}/sessions/{session_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Picker session get failed: {e.response.status_code}",
            ) from e
    return PickerStatusResponse(
        session_id=data["id"],
        media_items_set=bool(data.get("mediaItemsSet", False)),
        picker_uri=data.get("pickerUri"),
        expire_time=data.get("expireTime"),
    )


def _parse_picker_item(item: dict[str, Any]) -> dict[str, Any]:
    """Parse mediaItem retourne par Picker API (format different de Library)."""
    media_file = item.get("mediaFile", {}) or {}
    meta = media_file.get("mediaFileMetadata", {}) or {}
    photo_meta = meta.get("photoMetadata", {}) or {}
    video_meta = meta.get("videoMetadata", {}) or {}
    is_video = "videoMetadata" in meta

    creation_str = item.get("createTime") or meta.get("creationTime")
    creation_time = (
        datetime.fromisoformat(creation_str.replace("Z", "+00:00"))
        if creation_str
        else datetime.now(UTC)
    )
    return {
        "media_id": item["id"],
        "filename": media_file.get("filename"),
        "mime_type": media_file.get("mimeType"),
        "description": None,
        "creation_time": creation_time,
        "width": int(meta["width"]) if meta.get("width") else None,
        "height": int(meta["height"]) if meta.get("height") else None,
        "is_video": is_video,
        "video_duration_ms": int(video_meta.get("durationMillis"))
        if video_meta.get("durationMillis")
        else None,
        "camera_make": photo_meta.get("cameraMake") or video_meta.get("cameraMake"),
        "camera_model": photo_meta.get("cameraModel") or video_meta.get("cameraModel"),
        "base_url": media_file.get("baseUrl"),
        "product_url": None,  # Picker n'expose pas productUrl
    }


@router.post("/picker/import/{session_id}", response_model=PickerImportResponse)
async def picker_import(
    session_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_email: Annotated[str, Query()] = "marc.richard4@gmail.com",
) -> PickerImportResponse:
    """Importe les photos selectionnees dans la session Picker.

    A appeler APRES que /picker/status retourne mediaItemsSet=true.
    """
    start = time.monotonic()
    access_token = await _resolve_token(db, user_email)

    ingested = 0
    updated = 0
    errors = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"sessionId": session_id, "pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            try:
                r = await client.get(
                    f"{PICKER_API}/mediaItems",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Picker mediaItems list failed: {e.response.status_code}",
                ) from e

            # Parse tous les items, puis batch-fetch existants en 1 query (vs N+1)
            parsed_list: list[dict[str, Any]] = []
            for item in data.get("mediaItems", []):
                try:
                    parsed_list.append(_parse_picker_item(item))
                except Exception as e:
                    logger.warning("picker_parse_failed: id=%s err=%r", item.get("id"), e)
                    errors += 1

            if parsed_list:
                ids = [p["media_id"] for p in parsed_list]
                existing_rows = (
                    (await db.execute(select(Photo).where(Photo.media_id.in_(ids)))).scalars().all()
                )
                existing_map = {p.media_id: p for p in existing_rows}
                for parsed in parsed_list:
                    existing = existing_map.get(parsed["media_id"])
                    if existing:
                        for k, v in parsed.items():
                            setattr(existing, k, v)
                        updated += 1
                    else:
                        db.add(Photo(user_email=user_email, **parsed))
                        ingested += 1

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        await db.commit()

    return PickerImportResponse(
        session_id=session_id,
        ingested=ingested,
        updated=updated,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )
