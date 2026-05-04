"""Endpoints CRUD pour named_places (lieux nommes Marc) et trip_notes."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import NamedPlace, TripNote
from src.db.session import get_db

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/locations", tags=["places"])


# ─── NamedPlace schemas ──────────────────────────────────────────────────────


class NamedPlaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    lat: Decimal = Field(..., ge=-90, le=90)
    lng: Decimal = Field(..., ge=-180, le=180)
    radius_m: float = Field(default=200, ge=10, le=50000)
    semantic_type: str | None = None
    color: str | None = None
    icon: str | None = None
    notes: str | None = None


class NamedPlaceUpdate(BaseModel):
    name: str | None = None
    lat: Decimal | None = None
    lng: Decimal | None = None
    radius_m: float | None = None
    semantic_type: str | None = None
    color: str | None = None
    icon: str | None = None
    notes: str | None = None


class NamedPlaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    lat: Decimal
    lng: Decimal
    radius_m: float
    semantic_type: str | None
    color: str | None
    icon: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


# ─── NamedPlace endpoints ────────────────────────────────────────────────────


@router.get("/named-places", response_model=list[NamedPlaceRead])
async def list_named_places(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[NamedPlace]:
    rows = (
        (await db.execute(select(NamedPlace).order_by(NamedPlace.created_at.desc())))
        .scalars()
        .all()
    )
    return list(rows)


@router.post(
    "/named-places",
    response_model=NamedPlaceRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_named_place(
    payload: NamedPlaceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NamedPlace:
    place = NamedPlace(**payload.model_dump())
    db.add(place)
    await db.commit()
    await db.refresh(place)
    return place


@router.patch("/named-places/{place_id}", response_model=NamedPlaceRead)
async def update_named_place(
    place_id: UUID,
    payload: NamedPlaceUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NamedPlace:
    place = await db.get(NamedPlace, place_id)
    if place is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lieu introuvable")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(place, k, v)
    await db.commit()
    await db.refresh(place)
    return place


@router.delete("/named-places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_named_place(
    place_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    place = await db.get(NamedPlace, place_id)
    if place is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lieu introuvable")
    await db.delete(place)
    await db.commit()


# ─── TripNote schemas ────────────────────────────────────────────────────────


class TripNoteUpsert(BaseModel):
    start_date: date  # cle naturelle d'un trip
    end_date: date | None = None
    title: str | None = None
    content: str = ""
    rating: int | None = Field(default=None, ge=1, le=5)
    color: str | None = None


class TripNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    start_date: date
    end_date: date | None
    title: str | None
    content: str
    rating: int | None
    color: str | None
    created_at: datetime
    updated_at: datetime


# ─── TripNote endpoints ──────────────────────────────────────────────────────


@router.get("/trip-notes", response_model=list[TripNoteRead])
async def list_trip_notes(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TripNote]:
    rows = (await db.execute(select(TripNote).order_by(TripNote.start_date.desc()))).scalars().all()
    return list(rows)


@router.put("/trip-notes", response_model=TripNoteRead)
async def upsert_trip_note(
    payload: TripNoteUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TripNote:
    """Upsert par start_date : si la note existe deja, met a jour, sinon cree."""
    existing = (
        await db.execute(select(TripNote).where(TripNote.start_date == payload.start_date))
    ).scalar_one_or_none()
    if existing is None:
        note = TripNote(**payload.model_dump())
        db.add(note)
    else:
        for k, v in payload.model_dump().items():
            setattr(existing, k, v)
        note = existing
    await db.commit()
    await db.refresh(note)
    return note


@router.delete("/trip-notes/{start_date}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_trip_note(
    start_date: date,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    await db.execute(sa_delete(TripNote).where(TripNote.start_date == start_date))
    await db.commit()
