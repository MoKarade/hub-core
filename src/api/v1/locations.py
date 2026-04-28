"""Endpoints de gestion des points de localisation (Google Maps Timeline + futur).

Sprint Phase 2 :
- POST /v1/locations/points : insertion idempotente (dedup_hash)
- GET /v1/locations/points : listing avec filtres temporels et bbox geographique
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import LocationPoint
from src.db.session import get_db

router = APIRouter(prefix="/locations", tags=["locations"])


# ----------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------


class LocationPointCreate(BaseModel):
    timestamp_utc: datetime
    latitude: Decimal = Field(..., ge=-90, le=90)
    longitude: Decimal = Field(..., ge=-180, le=180)
    accuracy_m: int | None = Field(default=None, ge=0)
    altitude_m: int | None = None
    activity_type: str | None = Field(default=None, max_length=30)
    source: str = Field(..., examples=["google_takeout_timeline"])
    source_file: str | None = None
    latitude_e7: int
    longitude_e7: int
    dedup_hash: str = Field(..., min_length=64, max_length=64)


class LocationPointRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    timestamp_utc: datetime
    latitude: Decimal
    longitude: Decimal
    accuracy_m: int | None
    altitude_m: int | None
    activity_type: str | None
    source: str
    source_file: str | None
    dedup_hash: str
    created_at: datetime


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@router.post(
    "/points",
    response_model=LocationPointRead,
    status_code=status.HTTP_201_CREATED,
    summary="Inserer un point GPS (idempotent par dedup_hash)",
)
async def create_location_point(
    payload: LocationPointCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LocationPoint:
    existing = (
        await db.execute(select(LocationPoint).where(LocationPoint.dedup_hash == payload.dedup_hash))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    pt = LocationPoint(**payload.model_dump())
    db.add(pt)
    await db.commit()
    await db.refresh(pt)
    return pt


@router.get(
    "/points",
    response_model=list[LocationPointRead],
    summary="Lister les points GPS avec filtres optionnels (date + bbox + activity)",
)
async def list_location_points(
    db: Annotated[AsyncSession, Depends(get_db)],
    start: datetime | None = Query(default=None, description="Timestamp UTC debut"),
    end: datetime | None = Query(default=None, description="Timestamp UTC fin"),
    start_date: date | None = Query(default=None, description="Date debut (alternative a start)"),
    end_date: date | None = Query(default=None, description="Date fin (alternative a end)"),
    min_lat: float | None = Query(default=None, ge=-90, le=90),
    max_lat: float | None = Query(default=None, ge=-90, le=90),
    min_lng: float | None = Query(default=None, ge=-180, le=180),
    max_lng: float | None = Query(default=None, ge=-180, le=180),
    activity_type: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
) -> list[LocationPoint]:
    q = (
        select(LocationPoint)
        .order_by(LocationPoint.timestamp_utc.desc())
        .limit(limit)
        .offset(offset)
    )
    if start is not None:
        q = q.where(LocationPoint.timestamp_utc >= start)
    if end is not None:
        q = q.where(LocationPoint.timestamp_utc <= end)
    if start_date is not None:
        q = q.where(LocationPoint.timestamp_utc >= datetime.combine(start_date, datetime.min.time()))
    if end_date is not None:
        q = q.where(LocationPoint.timestamp_utc <= datetime.combine(end_date, datetime.max.time()))
    if min_lat is not None:
        q = q.where(LocationPoint.latitude >= Decimal(str(min_lat)))
    if max_lat is not None:
        q = q.where(LocationPoint.latitude <= Decimal(str(max_lat)))
    if min_lng is not None:
        q = q.where(LocationPoint.longitude >= Decimal(str(min_lng)))
    if max_lng is not None:
        q = q.where(LocationPoint.longitude <= Decimal(str(max_lng)))
    if activity_type is not None:
        q = q.where(LocationPoint.activity_type == activity_type)
    if source is not None:
        q = q.where(LocationPoint.source == source)

    return list((await db.execute(q)).scalars().all())


@router.get(
    "/points/{point_id}",
    response_model=LocationPointRead,
)
async def get_location_point(
    point_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LocationPoint:
    pt = await db.get(LocationPoint, point_id)
    if pt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Point introuvable")
    return pt
