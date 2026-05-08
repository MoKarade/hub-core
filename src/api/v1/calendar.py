"""Endpoint /v1/calendar - ingest et lecture des evenements Google Calendar."""

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
from src.db.models import CalendarEvent
from src.db.session import get_db
from src.services.oauth_google import get_valid_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["calendar"])
_OWNER_EMAIL: str = get_settings().hub_owner_email

GCAL_API = "https://www.googleapis.com/calendar/v3"


class CalSyncRequest(BaseModel):
    user_email: str = Field(default=_OWNER_EMAIL)
    days_back: int = Field(default=365, ge=1, le=10000)
    days_forward: int = Field(default=180, ge=0, le=10000)
    max_results_per_calendar: int = Field(default=2500, ge=1, le=10000)


class CalSyncResponse(BaseModel):
    calendars_synced: int
    events_ingested: int
    events_updated: int
    errors: int
    duration_seconds: float


class CalEventItem(BaseModel):
    id: UUID
    gcal_id: str
    calendar_id: str
    summary: str | None
    location: str | None
    start_at: datetime
    end_at: datetime
    all_day: bool
    organizer_email: str | None
    attendees: list[str]
    status: str | None
    html_link: str | None


async def _resolve_token(db: AsyncSession, user_email: str) -> str:
    for service in ("calendar", "all"):
        try:
            return await get_valid_access_token(db, service=service, user_email=user_email)
        except RuntimeError:
            continue
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Pas de token Calendar pour {user_email}")


def _parse_gcal_datetime(d: dict[str, Any]) -> tuple[datetime, bool]:
    """Parse {dateTime: '2026-04-30T14:00:00-04:00'} ou {date: '2026-04-30'}."""
    if "dateTime" in d:
        return datetime.fromisoformat(d["dateTime"]), False
    if "date" in d:
        # all-day event
        dt = datetime.fromisoformat(d["date"]).replace(tzinfo=UTC)
        return dt, True
    raise ValueError(f"Bad gcal datetime: {d}")


def _parse_event(ev: dict[str, Any], calendar_id: str) -> dict[str, Any]:
    start_at, all_day = _parse_gcal_datetime(ev.get("start", {}))
    end_at, _ = _parse_gcal_datetime(ev.get("end", {"date": ev.get("start", {}).get("date")}))
    organizer = (ev.get("organizer") or {}).get("email")
    attendees_raw = ev.get("attendees") or []
    attendees = [a.get("email") for a in attendees_raw if a.get("email")]
    return {
        "gcal_id": ev["id"],
        "calendar_id": calendar_id,
        "summary": ev.get("summary"),
        "description": ev.get("description"),
        "location": ev.get("location"),
        "start_at": start_at,
        "end_at": end_at,
        "all_day": all_day,
        "organizer_email": organizer,
        "attendees": attendees,
        "status": ev.get("status"),
        "html_link": ev.get("htmlLink"),
        "recurring_event_id": ev.get("recurringEventId"),
    }


@router.post("/sync", response_model=CalSyncResponse)
async def sync_calendar(
    payload: CalSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CalSyncResponse:
    start = time.monotonic()
    access_token = await _resolve_token(db, payload.user_email)

    time_min = (datetime.now(UTC) - timedelta(days=payload.days_back)).isoformat()
    time_max = (datetime.now(UTC) + timedelta(days=payload.days_forward)).isoformat()

    cal_count = 0
    ingested = 0
    updated = 0
    errors = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(
                f"{GCAL_API}/users/me/calendarList",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            r.raise_for_status()
            calendars = r.json().get("items", [])
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"calendarList failed: {e.response.status_code}",
            ) from e

        for cal in calendars:
            cal_id = cal.get("id")
            if not cal_id:
                continue
            cal_count += 1
            page_token: str | None = None
            fetched = 0
            while fetched < payload.max_results_per_calendar:
                params: dict[str, Any] = {
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": "true",
                    "maxResults": min(2500, payload.max_results_per_calendar - fetched),
                    "orderBy": "startTime",
                }
                if page_token:
                    params["pageToken"] = page_token
                try:
                    r = await client.get(
                        f"{GCAL_API}/calendars/{cal_id}/events",
                        headers={"Authorization": f"Bearer {access_token}"},
                        params=params,
                    )
                    r.raise_for_status()
                    data = r.json()
                except httpx.HTTPStatusError as e:
                    logger.warning(
                        "gcal_events_failed: cal=%s status=%d", cal_id, e.response.status_code
                    )
                    errors += 1
                    break

                for ev in data.get("items", []):
                    try:
                        parsed = _parse_event(ev, cal_id)
                    except Exception as e:
                        logger.warning("event_parse_failed: id=%s err=%r", ev.get("id"), e)
                        errors += 1
                        continue

                    existing = (
                        await db.execute(
                            select(CalendarEvent).where(CalendarEvent.gcal_id == parsed["gcal_id"])
                        )
                    ).scalar_one_or_none()

                    if existing:
                        for k, v in parsed.items():
                            setattr(existing, k, v)
                        updated += 1
                    else:
                        db.add(CalendarEvent(user_email=payload.user_email, **parsed))
                        ingested += 1

                    fetched += 1
                    if fetched >= payload.max_results_per_calendar:
                        break

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            await db.commit()

    return CalSyncResponse(
        calendars_synced=cal_count,
        events_ingested=ingested,
        events_updated=updated,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("/events", response_model=list[CalEventItem])
async def list_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CalEventItem]:
    if since is None:
        since = datetime.now(UTC) - timedelta(days=30)
    if until is None:
        until = datetime.now(UTC) + timedelta(days=180)

    stmt = (
        select(CalendarEvent)
        .where(CalendarEvent.start_at >= since, CalendarEvent.start_at <= until)
        .order_by(CalendarEvent.start_at)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(CalendarEvent.summary.ilike(like))
    stmt = stmt.limit(limit).offset(offset)

    rows = (await db.execute(stmt)).scalars().all()
    return [
        CalEventItem(
            id=r.id,
            gcal_id=r.gcal_id,
            calendar_id=r.calendar_id,
            summary=r.summary,
            location=r.location,
            start_at=r.start_at,
            end_at=r.end_at,
            all_day=r.all_day,
            organizer_email=r.organizer_email,
            attendees=r.attendees or [],
            status=r.status,
            html_link=r.html_link,
        )
        for r in rows
    ]


class CalStats(BaseModel):
    total: int
    upcoming: int
    past_30d: int
    by_calendar: list[dict[str, Any]]


@router.get("/stats", response_model=CalStats)
async def calendar_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> CalStats:
    now = datetime.now(UTC)
    total = (await db.execute(select(func.count(CalendarEvent.id)))).scalar() or 0
    upcoming = (
        await db.execute(select(func.count(CalendarEvent.id)).where(CalendarEvent.start_at >= now))
    ).scalar() or 0
    past_30d = (
        await db.execute(
            select(func.count(CalendarEvent.id)).where(
                CalendarEvent.start_at >= now - timedelta(days=30),
                CalendarEvent.start_at < now,
            )
        )
    ).scalar() or 0
    by_cal_q = (
        select(CalendarEvent.calendar_id, func.count(CalendarEvent.id).label("count"))
        .group_by(CalendarEvent.calendar_id)
        .order_by(desc("count"))
        .limit(10)
    )
    by_cal = [{"calendar_id": r[0], "count": int(r[1])} for r in (await db.execute(by_cal_q)).all()]
    return CalStats(total=total, upcoming=upcoming, past_30d=past_30d, by_calendar=by_cal)
