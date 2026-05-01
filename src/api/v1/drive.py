"""Endpoint /v1/drive - ingest Google Drive files metadata (Phase 3c)."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import DriveFile
from src.db.session import get_db
from src.services.oauth_google import get_valid_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/drive", tags=["drive"])

DRIVE_API = "https://www.googleapis.com/drive/v3"


class DriveSyncRequest(BaseModel):
    user_email: str = Field(default="marc.richard4@gmail.com")
    max_results: int = Field(default=2000, ge=1, le=100000)


class DriveSyncResponse(BaseModel):
    ingested: int
    updated: int
    errors: int
    duration_seconds: float


class DriveFileItem(BaseModel):
    id: UUID
    drive_id: str
    name: str | None
    mime_type: str
    size_bytes: int | None
    starred: bool
    is_shared: bool
    owner_email: str | None
    modified_time: datetime | None
    web_view_link: str | None


async def _resolve_token(db: AsyncSession, user_email: str) -> str:
    for service in ("drive", "all"):
        try:
            return await get_valid_access_token(db, service=service, user_email=user_email)
        except RuntimeError:
            continue
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Pas de token Drive pour {user_email}")


def _parse_file(f: dict[str, Any]) -> dict[str, Any]:
    owners = f.get("owners") or []
    owner_email = owners[0].get("emailAddress") if owners else None
    parents = f.get("parents") or []
    return {
        "drive_id": f["id"],
        "name": f.get("name"),
        "mime_type": f.get("mimeType", "unknown"),
        "size_bytes": int(f["size"]) if f.get("size") else None,
        "starred": bool(f.get("starred", False)),
        "trashed": bool(f.get("trashed", False)),
        "is_shared": bool(f.get("shared", False)),
        "owner_email": owner_email,
        "created_time": datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
        if f.get("createdTime")
        else None,
        "modified_time": datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))
        if f.get("modifiedTime")
        else None,
        "web_view_link": f.get("webViewLink"),
        "parents": ",".join(parents) if parents else None,
    }


@router.post("/sync", response_model=DriveSyncResponse)
async def sync_drive(
    payload: DriveSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DriveSyncResponse:
    start = time.monotonic()
    access_token = await _resolve_token(db, payload.user_email)

    fields = (
        "nextPageToken,files(id,name,mimeType,size,starred,trashed,shared,"
        "owners(emailAddress),createdTime,modifiedTime,webViewLink,parents)"
    )

    ingested = 0
    updated = 0
    errors = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        page_token: str | None = None
        fetched = 0
        while fetched < payload.max_results:
            params: dict[str, Any] = {
                "pageSize": min(1000, payload.max_results - fetched),
                "fields": fields,
                "orderBy": "modifiedTime desc",
                "q": "trashed = false",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                r = await client.get(
                    f"{DRIVE_API}/files",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Drive list failed: {e.response.status_code}",
                ) from e

            for f in data.get("files", []):
                try:
                    parsed = _parse_file(f)
                except Exception as e:
                    logger.warning("drive_parse_failed: id=%s err=%r", f.get("id"), e)
                    errors += 1
                    continue
                existing = (
                    await db.execute(
                        select(DriveFile).where(DriveFile.drive_id == parsed["drive_id"])
                    )
                ).scalar_one_or_none()
                if existing:
                    for k, v in parsed.items():
                        setattr(existing, k, v)
                    updated += 1
                else:
                    db.add(DriveFile(user_email=payload.user_email, **parsed))
                    ingested += 1
                fetched += 1
                if fetched >= payload.max_results:
                    break

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        await db.commit()

    return DriveSyncResponse(
        ingested=ingested,
        updated=updated,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


class DriveFileFull(DriveFileItem):
    """Detail enrichi pour la navigation folder."""

    parents: str | None = None
    """IDs parents separes par virgule (text serialise)."""


@router.get("/files", response_model=list[DriveFileFull])
async def list_files(
    db: Annotated[AsyncSession, Depends(get_db)],
    mime_type: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    starred: Annotated[bool | None, Query()] = None,
    parent_id: Annotated[
        str | None, Query(description="ID Drive du parent. 'root' = racine.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DriveFileFull]:
    stmt = (
        select(DriveFile)
        .where(DriveFile.trashed.is_(False))
        .order_by(
            # Folders d'abord (puis files)
            desc(DriveFile.mime_type == "application/vnd.google-apps.folder"),
            desc(DriveFile.modified_time),
        )
    )
    if mime_type:
        stmt = stmt.where(DriveFile.mime_type == mime_type)
    if q:
        stmt = stmt.where(DriveFile.name.ilike(f"%{q}%"))
    if starred is not None:
        stmt = stmt.where(DriveFile.starred == starred)
    if parent_id is not None:
        # Match : parents contient l'ID (CSV)
        if parent_id == "root":
            # Racine = pas de parent OU parent inconnu (orphelins)
            # On ne peut pas vraiment savoir le rootFolderId sans appel API,
            # donc on liste tout pour l'instant
            pass
        else:
            stmt = stmt.where(
                or_(
                    DriveFile.parents == parent_id,
                    DriveFile.parents.like(f"{parent_id},%"),
                    DriveFile.parents.like(f"%,{parent_id},%"),
                    DriveFile.parents.like(f"%,{parent_id}"),
                )
            )
    stmt = stmt.limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        DriveFileFull(
            id=f.id,
            drive_id=f.drive_id,
            name=f.name,
            mime_type=f.mime_type,
            size_bytes=f.size_bytes,
            starred=f.starred,
            is_shared=f.is_shared,
            owner_email=f.owner_email,
            modified_time=f.modified_time,
            web_view_link=f.web_view_link,
            parents=f.parents,
        )
        for f in rows
    ]


@router.get("/file/{drive_id}", response_model=DriveFileFull)
async def get_file_detail(
    drive_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DriveFileFull:
    f = (
        await db.execute(select(DriveFile).where(DriveFile.drive_id == drive_id))
    ).scalar_one_or_none()
    if not f:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fichier introuvable")
    return DriveFileFull(
        id=f.id,
        drive_id=f.drive_id,
        name=f.name,
        mime_type=f.mime_type,
        size_bytes=f.size_bytes,
        starred=f.starred,
        is_shared=f.is_shared,
        owner_email=f.owner_email,
        modified_time=f.modified_time,
        web_view_link=f.web_view_link,
        parents=f.parents,
    )


class DriveStats(BaseModel):
    total: int
    starred: int
    shared: int
    total_size_bytes: int
    by_mime: list[dict[str, Any]]


@router.get("/stats", response_model=DriveStats)
async def drive_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> DriveStats:
    base = select(func.count(DriveFile.id)).where(DriveFile.trashed.is_(False))
    total = (await db.execute(base)).scalar() or 0
    starred = (await db.execute(base.where(DriveFile.starred.is_(True)))).scalar() or 0
    shared = (await db.execute(base.where(DriveFile.is_shared.is_(True)))).scalar() or 0
    total_size = (
        await db.execute(
            select(func.coalesce(func.sum(DriveFile.size_bytes), 0)).where(
                DriveFile.trashed.is_(False)
            )
        )
    ).scalar() or 0
    by_mime_q = (
        select(DriveFile.mime_type, func.count(DriveFile.id).label("count"))
        .where(DriveFile.trashed.is_(False))
        .group_by(DriveFile.mime_type)
        .order_by(desc("count"))
        .limit(15)
    )
    by_mime = [{"mime_type": r[0], "count": int(r[1])} for r in (await db.execute(by_mime_q)).all()]
    return DriveStats(
        total=total,
        starred=starred,
        shared=shared,
        total_size_bytes=int(total_size),
        by_mime=by_mime,
    )
