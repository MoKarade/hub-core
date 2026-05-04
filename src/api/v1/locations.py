"""Endpoints de gestion des points de localisation (Google Maps Timeline + futur).

- POST /v1/locations/points         : insertion idempotente (dedup_hash)
- GET  /v1/locations/points         : listing avec filtres temporels et bbox
- POST /v1/locations/ingest-file    : ingestion bulk Timeline.json (tous formats)
- GET  /v1/locations/visits         : liste des visites semantiques
- GET  /v1/locations/stats          : stats globales (pays, lieux, duree)

Formats Timeline.json supportes :
  (A) Ancien : {"locations": [{latitudeE7, longitudeE7, timestamp/timestampMs, ...}]}
  (B) Nouveau : {"semanticSegments": [{visit|timelinePath|activity, ...}]}
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, inspect, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.events import broadcast
from src.db.models import LocationActivity, LocationAddress, LocationPoint, LocationVisit
from src.db.session import SessionLocal, get_db

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/locations", tags=["locations"])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers parsing
# ─────────────────────────────────────────────────────────────────────────────

# Gere les encodages: "50.64°", "50.64Â°", "50.64 ° " etc.
_DEG_RE = re.compile(r"\xc2?\xb0|°|Â°")


def _clean_deg(s: str) -> str:
    return _DEG_RE.sub("", s).strip()


def _parse_latlng(s: str) -> tuple[Decimal, Decimal, int, int]:
    """
    Parse 'lat°, lng°' dans tous les encodages rencontres dans les exports Google.
    Exemples : '50.6428844°, 2.9832603°' / '50.6428844Â°, 2.9832603Â°'
    """
    s = _clean_deg(s)
    # Separateur peut etre ', ' ou ' '
    s = s.replace(",", " ")
    parts = s.split()
    if len(parts) < 2:
        raise ValueError(f"Cannot parse latlng: {s!r}")
    lat_f = float(parts[0])
    lng_f = float(parts[1])
    if not (-90 <= lat_f <= 90) or not (-180 <= lng_f <= 180):
        raise ValueError(f"Out-of-range coords: {lat_f}, {lng_f}")
    lat = Decimal(str(round(lat_f, 7)))
    lng = Decimal(str(round(lng_f, 7)))
    lat_e7 = round(lat_f * 1e7)
    lng_e7 = round(lng_f * 1e7)
    return lat, lng, lat_e7, lng_e7


def _parse_ts(s: str | int | float) -> datetime:
    """
    Parse timestamp Google :
    - int/float  → millisecondes epoch
    - str digits → millisecondes epoch
    - str ISO    → ISO 8601 (avec ou sans offset)
    """
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s / 1000.0, tz=UTC)
    s = str(s).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s) / 1000.0, tz=UTC)
    # ISO 8601
    raw = s.rstrip("Z")
    if "." in raw:
        head, frac = raw.split(".", 1)
        offset = ""
        for sep in ("+", "-"):
            if sep in frac:
                i = frac.index(sep)
                offset, frac = frac[i:], frac[:i]
                break
        frac = (frac + "000000")[:6]
        raw = f"{head}.{frac}{offset}"
    if not re.search(r"[+-]\d{2}:\d{2}$", raw):
        raw += "+00:00"
    return datetime.fromisoformat(raw)


def _dedup(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Parsers par format
# ─────────────────────────────────────────────────────────────────────────────

def _parse_old_format(data: dict, source_file: str) -> dict[str, list[dict]]:
    """Format historique : {"locations": [{latitudeE7, longitudeE7, ...}]}"""
    points: list[dict] = []
    for loc in data.get("locations", []):
        try:
            ts_raw = loc.get("timestamp") or loc.get("timestampMs")
            if ts_raw is None:
                continue
            ts = _parse_ts(ts_raw)
            lat_e7 = int(loc["latitudeE7"])
            lng_e7 = int(loc["longitudeE7"])
            lat = (Decimal(lat_e7) / Decimal(10_000_000)).quantize(Decimal("0.0000001"))
            lng = (Decimal(lng_e7) / Decimal(10_000_000)).quantize(Decimal("0.0000001"))
            accuracy = loc.get("accuracy")
            # Activite principale
            act_type = None
            for grp in (loc.get("activity") or loc.get("activitys") or []):
                items = grp.get("activity", [])
                if items:
                    best = max(items, key=lambda x: x.get("confidence", 0))
                    act_type = str(best.get("type", "")).lower() or None
                    break
            dh = _dedup("path", ts.isoformat(), str(lat_e7), str(lng_e7))
            points.append({
                "timestamp_utc": ts,
                "latitude": lat,
                "longitude": lng,
                "latitude_e7": lat_e7,
                "longitude_e7": lng_e7,
                "accuracy_m": int(accuracy) if accuracy is not None else None,
                "altitude_m": int(loc["altitude"]) if "altitude" in loc else None,
                "activity_type": act_type,
                "source": "google_timeline",
                "source_file": source_file,
                "dedup_hash": dh,
            })
        except Exception as exc:
            logger.debug("old_format_skip", error=str(exc)[:80])
    return {"visits": [], "points": points, "activities": []}


def _parse_semantic_format(data: dict, source_file: str) -> dict[str, list[dict]]:
    """Nouveau format : {"semanticSegments": [{visit|timelinePath|activity}]}"""
    visits: list[dict] = []
    points: list[dict] = []
    activities: list[dict] = []

    for seg in data.get("semanticSegments", []):
        try:
            start = _parse_ts(seg["startTime"])
            end = _parse_ts(seg["endTime"])
            tz_off = seg.get("startTimeTimezoneUtcOffsetMinutes")

            # ── VISIT ────────────────────────────────────────────────────────
            if "visit" in seg:
                v = seg["visit"]
                cand = v.get("topCandidate", {})
                loc_str = cand.get("placeLocation", {}).get("latLng", "")
                if not loc_str:
                    continue
                lat, lng, lat_e7, lng_e7 = _parse_latlng(loc_str)
                dh = _dedup("visit", start.isoformat(), str(lat_e7), str(lng_e7))
                visits.append({
                    "start_time": start,
                    "end_time": end,
                    "tz_offset_minutes": tz_off,
                    "lat": lat,
                    "lng": lng,
                    "lat_e7": lat_e7,
                    "lng_e7": lng_e7,
                    "place_id": cand.get("placeId"),
                    "semantic_type": cand.get("semanticType"),
                    "probability": cand.get("probability"),
                    "source": "google_timeline",
                    "dedup_hash": dh,
                })

            # ── PATH ─────────────────────────────────────────────────────────
            elif "timelinePath" in seg:
                for pt in seg["timelinePath"]:
                    try:
                        lat, lng, lat_e7, lng_e7 = _parse_latlng(pt["point"])
                        ts = _parse_ts(pt["time"])
                        dh = _dedup("path", ts.isoformat(), str(lat_e7), str(lng_e7))
                        points.append({
                            "timestamp_utc": ts,
                            "latitude": lat,
                            "longitude": lng,
                            "latitude_e7": lat_e7,
                            "longitude_e7": lng_e7,
                            "accuracy_m": None,
                            "altitude_m": None,
                            "activity_type": None,
                            "source": "google_timeline",
                            "source_file": source_file,
                            "dedup_hash": dh,
                        })
                    except Exception as exc:
                        logger.debug("path_point_skip", error=str(exc)[:80])

            # ── ACTIVITY ──────────────────────────────────────────────────────
            elif "activity" in seg:
                act = seg["activity"]
                cand = act.get("topCandidate", {})
                s_lat = s_lng = e_lat = e_lng = None
                if (sl := act.get("start", {}).get("latLng")):
                    try:
                        s_lat, s_lng, _, _ = _parse_latlng(sl)
                    except Exception:
                        pass
                if (el := act.get("end", {}).get("latLng")):
                    try:
                        e_lat, e_lng, _, _ = _parse_latlng(el)
                    except Exception:
                        pass
                dh = _dedup(
                    "activity",
                    start.isoformat(),
                    cand.get("type", ""),
                    str(act.get("distanceMeters", "")),
                )
                activities.append({
                    "start_time": start,
                    "end_time": end,
                    "tz_offset_minutes": tz_off,
                    "activity_type": cand.get("type"),
                    "distance_meters": act.get("distanceMeters"),
                    "probability": cand.get("probability"),
                    "start_lat": s_lat,
                    "start_lng": s_lng,
                    "end_lat": e_lat,
                    "end_lng": e_lng,
                    "source": "google_timeline",
                    "dedup_hash": dh,
                })
        except Exception as exc:
            logger.debug("segment_skip", error=str(exc)[:100])

    return {"visits": visits, "points": points, "activities": activities}


def _auto_detect_and_parse(data: dict, source_file: str) -> dict[str, list[dict]]:
    """Detecte le format et parse en consequence."""
    if "semanticSegments" in data:
        logger.info("timeline_format_detected", fmt="semanticSegments_2024")
        return _parse_semantic_format(data, source_file)
    elif "locations" in data:
        logger.info("timeline_format_detected", fmt="locations_classic")
        return _parse_old_format(data, source_file)
    else:
        raise ValueError(f"Format Timeline.json non reconnu. Cles: {list(data.keys())[:5]}")


# ─────────────────────────────────────────────────────────────────────────────
# Bulk insert helpers — compatible SQLite + PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────

_BATCH = 500


async def _is_sqlite(db: AsyncSession) -> bool:
    raw = await db.run_sync(lambda sync_s: sync_s.get_bind().dialect.name)
    return raw == "sqlite"


async def _upsert_ignore(db: AsyncSession, model: Any, rows: list[dict]) -> tuple[int, int]:
    """INSERT OR IGNORE (SQLite) / INSERT ON CONFLICT DO NOTHING (PG). Retourne (inserted, skipped)."""
    if not rows:
        return 0, 0

    from sqlalchemy import insert as sa_insert

    is_sq = await _is_sqlite(db)
    inserted = skipped = 0

    for i in range(0, len(rows), _BATCH):
        batch = rows[i : i + _BATCH]
        if is_sq:
            stmt = sa_insert(model).prefix_with("OR IGNORE").values(batch)
        else:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = pg_insert(model).values(batch).on_conflict_do_nothing()

        result = await db.execute(stmt)
        await db.commit()
        rc = result.rowcount if result.rowcount >= 0 else len(batch)
        inserted += rc
        skipped += len(batch) - rc

    return inserted, skipped


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────


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


class LocationVisitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    start_time: datetime
    end_time: datetime
    lat: Decimal
    lng: Decimal
    semantic_type: str | None
    place_id: str | None
    probability: float | None
    tz_offset_minutes: int | None
    source: str
    created_at: datetime


class IngestFileRequest(BaseModel):
    file_path: str = Field(..., description="Chemin absolu vers Timeline.json")


class IngestFileResponse(BaseModel):
    visits_inserted: int
    visits_skipped: int
    points_inserted: int
    points_skipped: int
    activities_inserted: int
    activities_skipped: int
    segments_total: int
    duration_seconds: float
    format_detected: str


class LocationStats(BaseModel):
    total_visits: int
    unique_places: int
    home_visits: int
    work_visits: int
    earliest_date: str | None
    latest_date: str | None
    total_path_points: int
    total_activities: int


# ─────────────────────────────────────────────────────────────────────────────
# Routes — points (existant)
# ─────────────────────────────────────────────────────────────────────────────


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
        await db.execute(
            select(LocationPoint).where(LocationPoint.dedup_hash == payload.dedup_hash)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    pt = LocationPoint(**payload.model_dump())
    db.add(pt)
    await db.commit()
    await db.refresh(pt)
    await broadcast(
        "new_location",
        {"timestamp_utc": pt.timestamp_utc.isoformat(), "activity_type": pt.activity_type},
    )
    return pt


@router.get(
    "/points",
    response_model=list[LocationPointRead],
    summary="Lister les points GPS avec filtres optionnels",
)
async def list_location_points(
    db: Annotated[AsyncSession, Depends(get_db)],
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    min_lat: float | None = Query(default=None, ge=-90, le=90),
    max_lat: float | None = Query(default=None, ge=-90, le=90),
    min_lng: float | None = Query(default=None, ge=-180, le=180),
    max_lng: float | None = Query(default=None, ge=-180, le=180),
    activity_type: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=500000),
    offset: int = Query(default=0, ge=0),
) -> list[LocationPoint]:
    q = (
        select(LocationPoint)
        .order_by(LocationPoint.timestamp_utc.desc())
        .limit(limit)
        .offset(offset)
    )
    if start:
        q = q.where(LocationPoint.timestamp_utc >= start)
    if end:
        q = q.where(LocationPoint.timestamp_utc <= end)
    if start_date:
        q = q.where(LocationPoint.timestamp_utc >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        q = q.where(LocationPoint.timestamp_utc <= datetime.combine(end_date, datetime.max.time()))
    if min_lat is not None:
        q = q.where(LocationPoint.latitude >= Decimal(str(min_lat)))
    if max_lat is not None:
        q = q.where(LocationPoint.latitude <= Decimal(str(max_lat)))
    if min_lng is not None:
        q = q.where(LocationPoint.longitude >= Decimal(str(min_lng)))
    if max_lng is not None:
        q = q.where(LocationPoint.longitude <= Decimal(str(max_lng)))
    if activity_type:
        q = q.where(LocationPoint.activity_type == activity_type)
    if source:
        q = q.where(LocationPoint.source == source)
    return list((await db.execute(q)).scalars().all())


@router.get("/points/{point_id}", response_model=LocationPointRead)
async def get_location_point(
    point_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LocationPoint:
    pt = await db.get(LocationPoint, point_id)
    if pt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Point introuvable")
    return pt


# ─────────────────────────────────────────────────────────────────────────────
# Routes — visites sémantiques
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/visits",
    response_model=list[LocationVisitRead],
    summary="Lister les visites semantiques",
)
async def list_visits(
    db: Annotated[AsyncSession, Depends(get_db)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    semantic_type: str | None = Query(default=None, description="HOME, WORK, SEARCHED_ADDRESS, etc."),
    min_lat: float | None = Query(default=None, ge=-90, le=90),
    max_lat: float | None = Query(default=None, ge=-90, le=90),
    min_lng: float | None = Query(default=None, ge=-180, le=180),
    max_lng: float | None = Query(default=None, ge=-180, le=180),
    limit: int = Query(default=200, ge=1, le=500000),
    offset: int = Query(default=0, ge=0),
) -> list[LocationVisit]:
    q = (
        select(LocationVisit)
        .order_by(LocationVisit.start_time.desc())
        .limit(limit)
        .offset(offset)
    )
    if start_date:
        q = q.where(LocationVisit.start_time >= datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC))
    if end_date:
        # end_date inclus
        import calendar
        last_day = calendar.monthrange(end_date.year, end_date.month)[1]
        next_day = end_date.replace(day=end_date.day) if end_date.day < last_day else end_date
        q = q.where(LocationVisit.start_time < datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=UTC))
    if semantic_type:
        q = q.where(LocationVisit.semantic_type == semantic_type)
    if min_lat is not None:
        q = q.where(LocationVisit.lat >= Decimal(str(min_lat)))
    if max_lat is not None:
        q = q.where(LocationVisit.lat <= Decimal(str(max_lat)))
    if min_lng is not None:
        q = q.where(LocationVisit.lng >= Decimal(str(min_lng)))
    if max_lng is not None:
        q = q.where(LocationVisit.lng <= Decimal(str(max_lng)))
    return list((await db.execute(q)).scalars().all())


@router.get("/stats", response_model=LocationStats, summary="Stats globales des locations")
async def get_location_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LocationStats:
    total_visits = (await db.execute(select(func.count()).select_from(LocationVisit))).scalar_one()
    unique_places = (
        await db.execute(
            select(func.count(func.distinct(LocationVisit.place_id))).where(
                LocationVisit.place_id.is_not(None)
            )
        )
    ).scalar_one()
    home_visits = (
        await db.execute(
            select(func.count()).select_from(LocationVisit).where(LocationVisit.semantic_type == "HOME")
        )
    ).scalar_one()
    work_visits = (
        await db.execute(
            select(func.count()).select_from(LocationVisit).where(LocationVisit.semantic_type == "WORK")
        )
    ).scalar_one()
    earliest = (await db.execute(select(func.min(LocationVisit.start_time)))).scalar_one()
    latest = (await db.execute(select(func.max(LocationVisit.start_time)))).scalar_one()
    total_points = (
        await db.execute(
            select(func.count()).select_from(LocationPoint).where(LocationPoint.source == "google_timeline")
        )
    ).scalar_one()
    total_activities = (
        await db.execute(select(func.count()).select_from(LocationActivity))
    ).scalar_one()

    return LocationStats(
        total_visits=total_visits,
        unique_places=unique_places,
        home_visits=home_visits,
        work_visits=work_visits,
        earliest_date=earliest.date().isoformat() if earliest else None,
        latest_date=latest.date().isoformat() if latest else None,
        total_path_points=total_points,
        total_activities=total_activities,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Route — ingestion bulk Timeline.json (tous formats)
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/ingest-file",
    response_model=IngestFileResponse,
    summary="Ingere un Timeline.json Google depuis le disque (format auto-detecte)",
)
async def ingest_timeline_file(
    payload: IngestFileRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> IngestFileResponse:
    path = Path(payload.file_path)
    if not path.exists():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Fichier introuvable: {path}")
    if path.suffix.lower() != ".json":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Le fichier doit etre un .json")

    t0 = datetime.now(UTC)
    size_mb = round(path.stat().st_size / 1e6, 1)
    logger.info("timeline_ingest_start", file=str(path), size_mb=size_mb)

    # ── Lecture (ijson si dispo pour streaming, sinon json standard) ──────────
    fmt_detected = "unknown"
    try:
        import ijson  # type: ignore

        # On detecte d'abord le format en lisant le debut du fichier
        with path.open("rb") as f:
            for prefix, event, value in ijson.parse(f):
                if prefix in ("semanticSegments", "semanticSegments.item"):
                    fmt_detected = "semanticSegments_2024"
                    break
                elif prefix in ("locations", "locations.item"):
                    fmt_detected = "locations_classic"
                    break

        if fmt_detected == "semanticSegments_2024":
            segments_raw = []
            with path.open("rb") as f:
                for seg in ijson.items(f, "semanticSegments.item"):
                    segments_raw.append(seg)
            data = {"semanticSegments": segments_raw}
        elif fmt_detected == "locations_classic":
            locs = []
            with path.open("rb") as f:
                for loc in ijson.items(f, "locations.item"):
                    locs.append(loc)
            data = {"locations": locs}
        else:
            # Fallback JSON complet
            with path.open(encoding="utf-8", errors="replace") as f:
                data = json.load(f)

    except ImportError:
        logger.warning("ijson_not_available_fallback_json")
        with path.open(encoding="utf-8", errors="replace") as f:
            data = json.load(f)

    # ── Parse selon format ────────────────────────────────────────────────────
    try:
        parsed = _auto_detect_and_parse(data, path.name)
        fmt_detected = (
            "semanticSegments_2024" if "semanticSegments" in data else "locations_classic"
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    logger.info(
        "timeline_parsed",
        visits=len(parsed["visits"]),
        points=len(parsed["points"]),
        activities=len(parsed["activities"]),
        format=fmt_detected,
    )

    # ── Insert bulk ────────────────────────────────────────────────────────────
    v_ins, v_skip = await _upsert_ignore(db, LocationVisit, parsed["visits"])
    p_ins, p_skip = await _upsert_ignore(db, LocationPoint, parsed["points"])
    a_ins, a_skip = await _upsert_ignore(db, LocationActivity, parsed["activities"])

    duration = (datetime.now(UTC) - t0).total_seconds()
    total = len(parsed["visits"]) + len(parsed["points"]) + len(parsed["activities"])

    logger.info(
        "timeline_ingest_done",
        visits_inserted=v_ins,
        points_inserted=p_ins,
        activities_inserted=a_ins,
        duration_s=round(duration, 1),
    )
    await broadcast("timeline_ingested", {"visits": v_ins, "points": p_ins, "activities": a_ins})

    return IngestFileResponse(
        visits_inserted=v_ins,
        visits_skipped=v_skip,
        points_inserted=p_ins,
        points_skipped=p_skip,
        activities_inserted=a_ins,
        activities_skipped=a_skip,
        segments_total=total,
        duration_seconds=round(duration, 2),
        format_detected=fmt_detected,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routes — stats enrichies
# ─────────────────────────────────────────────────────────────────────────────


class ActivityTypeStats(BaseModel):
    activity_type: str
    count: int
    total_distance_km: float
    total_duration_minutes: int


class YearStats(BaseModel):
    year: int
    visits: int
    home_visits: int
    work_visits: int


@router.get(
    "/activity-stats",
    response_model=list[ActivityTypeStats],
    summary="Stats par type d'activite (count + distance totale + duree)",
)
async def get_activity_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ActivityTypeStats]:
    from sqlalchemy import case

    rows = (
        await db.execute(
            select(
                LocationActivity.activity_type,
                func.count().label("count"),
                func.coalesce(func.sum(LocationActivity.distance_meters), 0).label("total_m"),
                func.coalesce(
                    func.sum(
                        func.cast(
                            func.strftime("%s", LocationActivity.end_time) -
                            func.strftime("%s", LocationActivity.start_time),
                            sa.Integer,
                        )
                    ),
                    0,
                ).label("total_s"),
            )
            .group_by(LocationActivity.activity_type)
            .order_by(func.count().desc())
        )
    ).all()

    return [
        ActivityTypeStats(
            activity_type=r.activity_type or "UNKNOWN_ACTIVITY_TYPE",
            count=r.count,
            total_distance_km=round(r.total_m / 1000, 1),
            total_duration_minutes=r.total_s // 60,
        )
        for r in rows
    ]


@router.get(
    "/visits-by-year",
    response_model=list[YearStats],
    summary="Nombre de visites par annee",
)
async def get_visits_by_year(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[YearStats]:
    # SQLite: strftime('%Y', ...) / PostgreSQL: extract(year from ...)
    is_sq = await _is_sqlite(db)
    if is_sq:
        year_expr = func.strftime("%Y", LocationVisit.start_time)
    else:
        year_expr = func.extract("year", LocationVisit.start_time)

    rows = (
        await db.execute(
            select(
                year_expr.label("year"),
                func.count().label("visits"),
                func.sum(
                    sa.case((LocationVisit.semantic_type == "HOME", 1), else_=0)
                ).label("home"),
                func.sum(
                    sa.case((LocationVisit.semantic_type == "WORK", 1), else_=0)
                ).label("work"),
            )
            .group_by(year_expr)
            .order_by(year_expr)
        )
    ).all()

    return [
        YearStats(year=int(r.year), visits=r.visits, home_visits=r.home, work_visits=r.work)
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Routes — gestion lieux (retag + patch)
# ─────────────────────────────────────────────────────────────────────────────


class RetagRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitude du lieu en degres")
    lng: float = Field(..., ge=-180, le=180, description="Longitude du lieu en degres")
    radius_m: float = Field(default=300, ge=1, le=50000, description="Rayon de retag en metres")
    semantic_type: str = Field(..., description="HOME, WORK, SEARCHED_ADDRESS, ALIASED_LOCATION, UNKNOWN")


class RetagResponse(BaseModel):
    updated: int
    semantic_type: str
    lat: float
    lng: float
    radius_m: float


class VisitPatch(BaseModel):
    semantic_type: str = Field(..., description="Nouveau type semantique")


def _bbox(lat: float, lng: float, radius_m: float) -> tuple[float, float, float, float]:
    """Retourne (min_lat, max_lat, min_lng, max_lng) pour un cercle approxime par un carre."""
    import math
    delta_lat = radius_m / 111_000
    delta_lng = radius_m / (111_000 * max(math.cos(math.radians(lat)), 0.001))
    return lat - delta_lat, lat + delta_lat, lng - delta_lng, lng + delta_lng


@router.post(
    "/retag",
    response_model=RetagResponse,
    summary="Retagger toutes les visites dans un rayon autour d'un point (ex: definir domicile)",
)
async def retag_visits(
    payload: RetagRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RetagResponse:
    min_lat, max_lat, min_lng, max_lng = _bbox(payload.lat, payload.lng, payload.radius_m)

    # Recup les visites dans la bbox
    rows = (
        await db.execute(
            select(LocationVisit.id)
            .where(LocationVisit.lat >= Decimal(str(round(min_lat, 7))))
            .where(LocationVisit.lat <= Decimal(str(round(max_lat, 7))))
            .where(LocationVisit.lng >= Decimal(str(round(min_lng, 7))))
            .where(LocationVisit.lng <= Decimal(str(round(max_lng, 7))))
        )
    ).scalars().all()

    if not rows:
        return RetagResponse(updated=0, semantic_type=payload.semantic_type,
                             lat=payload.lat, lng=payload.lng, radius_m=payload.radius_m)

    # Update en batch
    ids = list(rows)
    await db.execute(
        sa_update(LocationVisit)
        .where(LocationVisit.id.in_(ids))
        .values(semantic_type=payload.semantic_type)
    )
    await db.commit()

    logger.info(
        "visits_retagged",
        count=len(ids),
        semantic_type=payload.semantic_type,
        lat=payload.lat,
        lng=payload.lng,
        radius_m=payload.radius_m,
    )

    return RetagResponse(
        updated=len(ids),
        semantic_type=payload.semantic_type,
        lat=payload.lat,
        lng=payload.lng,
        radius_m=payload.radius_m,
    )


@router.patch(
    "/visits/{visit_id}",
    response_model=LocationVisitRead,
    summary="Modifier le type semantique d'une visite individuelle",
)
async def patch_visit(
    visit_id: UUID,
    payload: VisitPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LocationVisit:
    visit = await db.get(LocationVisit, visit_id)
    if visit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Visite introuvable")
    visit.semantic_type = payload.semantic_type
    await db.commit()
    await db.refresh(visit)
    return visit


# ─────────────────────────────────────────────────────────────────────────────
# Routes — explorer (place-stats, day, trips, geocode)
# ─────────────────────────────────────────────────────────────────────────────


class PlaceStatsResponse(BaseModel):
    total_visits: int
    total_duration_minutes: int
    first_visit: datetime | None
    last_visit: datetime | None
    semantic_type_breakdown: dict[str, int]
    avg_duration_minutes: float
    visits: list[LocationVisitRead]  # max 50 most recent


@router.get(
    "/place-stats",
    response_model=PlaceStatsResponse,
    summary="Statistiques des visites pres d'un point (count + frequence + dates)",
)
async def get_place_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_m: float = Query(default=100, ge=10, le=10000),
) -> PlaceStatsResponse:
    min_lat, max_lat, min_lng, max_lng = _bbox(lat, lng, radius_m)

    rows = (
        await db.execute(
            select(LocationVisit)
            .where(LocationVisit.lat >= Decimal(str(round(min_lat, 7))))
            .where(LocationVisit.lat <= Decimal(str(round(max_lat, 7))))
            .where(LocationVisit.lng >= Decimal(str(round(min_lng, 7))))
            .where(LocationVisit.lng <= Decimal(str(round(max_lng, 7))))
            .order_by(LocationVisit.start_time.desc())
            .limit(500)
        )
    ).scalars().all()

    visits_list = list(rows)
    if not visits_list:
        return PlaceStatsResponse(
            total_visits=0, total_duration_minutes=0,
            first_visit=None, last_visit=None,
            semantic_type_breakdown={}, avg_duration_minutes=0,
            visits=[],
        )

    durations_sec = [
        max(0, (v.end_time - v.start_time).total_seconds()) for v in visits_list
    ]
    total_min = sum(durations_sec) / 60
    avg_min = total_min / len(visits_list) if visits_list else 0

    type_counts: dict[str, int] = {}
    for v in visits_list:
        key = v.semantic_type or "UNKNOWN"
        type_counts[key] = type_counts.get(key, 0) + 1

    return PlaceStatsResponse(
        total_visits=len(visits_list),
        total_duration_minutes=int(total_min),
        first_visit=min(v.start_time for v in visits_list),
        last_visit=max(v.start_time for v in visits_list),
        semantic_type_breakdown=type_counts,
        avg_duration_minutes=round(avg_min, 1),
        visits=visits_list[:50],
    )


class LocationActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    start_time: datetime
    end_time: datetime
    activity_type: str | None
    distance_meters: float | None
    probability: float | None
    start_lat: Decimal | None
    start_lng: Decimal | None
    end_lat: Decimal | None
    end_lng: Decimal | None


class DaySummary(BaseModel):
    date: str
    visits_count: int
    activities_count: int
    points_count: int
    total_distance_km: float
    total_duration_minutes: int
    semantic_type_breakdown: dict[str, int]
    activity_breakdown: dict[str, dict[str, float]]


class DayResponse(BaseModel):
    summary: DaySummary
    visits: list[LocationVisitRead]
    activities: list[LocationActivityRead]
    points: list[LocationPointRead]


@router.get(
    "/day",
    response_model=DayResponse,
    summary="Tout ce qui s'est passe une journee precise (visites + activites + path)",
)
async def get_day(
    db: Annotated[AsyncSession, Depends(get_db)],
    target_date: date = Query(..., alias="date"),
) -> DayResponse:
    start_dt = datetime.combine(target_date, datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(target_date, datetime.max.time(), tzinfo=UTC)

    visits = list((
        await db.execute(
            select(LocationVisit)
            .where(LocationVisit.start_time >= start_dt)
            .where(LocationVisit.start_time <= end_dt)
            .order_by(LocationVisit.start_time)
        )
    ).scalars().all())

    activities = list((
        await db.execute(
            select(LocationActivity)
            .where(LocationActivity.start_time >= start_dt)
            .where(LocationActivity.start_time <= end_dt)
            .order_by(LocationActivity.start_time)
        )
    ).scalars().all())

    points = list((
        await db.execute(
            select(LocationPoint)
            .where(LocationPoint.timestamp_utc >= start_dt)
            .where(LocationPoint.timestamp_utc <= end_dt)
            .where(LocationPoint.source == "google_timeline")
            .order_by(LocationPoint.timestamp_utc)
            .limit(2000)
        )
    ).scalars().all())

    # Aggregations
    type_counts: dict[str, int] = {}
    for v in visits:
        key = v.semantic_type or "UNKNOWN"
        type_counts[key] = type_counts.get(key, 0) + 1

    act_breakdown: dict[str, dict[str, float]] = {}
    total_dist_m = 0.0
    total_dur_s = 0.0
    for a in activities:
        key = a.activity_type or "UNKNOWN_ACTIVITY_TYPE"
        existing = act_breakdown.setdefault(key, {"count": 0, "distance_km": 0, "minutes": 0})
        existing["count"] += 1
        if a.distance_meters:
            existing["distance_km"] += round(a.distance_meters / 1000, 2)
            total_dist_m += a.distance_meters
        dur_s = max(0, (a.end_time - a.start_time).total_seconds())
        existing["minutes"] += round(dur_s / 60, 1)
        total_dur_s += dur_s

    summary = DaySummary(
        date=target_date.isoformat(),
        visits_count=len(visits),
        activities_count=len(activities),
        points_count=len(points),
        total_distance_km=round(total_dist_m / 1000, 2),
        total_duration_minutes=int(total_dur_s / 60),
        semantic_type_breakdown=type_counts,
        activity_breakdown=act_breakdown,
    )

    return DayResponse(summary=summary, visits=visits, activities=activities, points=points)


class Trip(BaseModel):
    start_date: str
    end_date: str
    duration_days: int
    visit_count: int
    activity_count: int
    total_distance_km: float
    max_distance_from_home_km: float
    destinations: list[dict[str, Any]]  # top 5 lieux visites
    name: str | None = None  # nom auto-genere depuis top destination + adresses
    primary_country: str | None = None
    primary_city: str | None = None


class TripsResponse(BaseModel):
    home_lat: float
    home_lng: float
    home_radius_km: float
    min_duration_hours: int
    trips: list[Trip]


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distance haversine en km entre 2 points lat/lng."""
    import math
    R = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@router.get(
    "/trips",
    response_model=TripsResponse,
    summary="Voyages auto-detectes (periodes loin du domicile pendant >X heures)",
)
async def get_trips(
    db: Annotated[AsyncSession, Depends(get_db)],
    home_lat: float | None = Query(default=None, description="Lat domicile (sinon auto-detect)"),
    home_lng: float | None = Query(default=None, description="Lng domicile (sinon auto-detect)"),
    home_radius_km: float = Query(default=50, ge=1, le=500, description="Rayon home en km"),
    min_duration_hours: int = Query(default=24, ge=6, le=720, description="Duree minimum d'un voyage"),
    min_distance_km: float = Query(default=100, ge=10, le=10000, description="Distance min depuis home"),
    home_recency_months: int | None = Query(
        default=24, ge=1, le=240,
        description="Pour auto-detect home : on regarde les HOME visits des N derniers mois "
                    "(0 ou null = tout l'historique). Defaut 24 mois pour gerer les demenagements.",
    ),
) -> TripsResponse:
    # 1. Determine home reference si pas fourni
    if home_lat is None or home_lng is None:
        # Filtre par recence pour gerer les demenagements
        from datetime import timedelta
        q = (
            select(LocationVisit.lat, LocationVisit.lng, LocationVisit.start_time)
            .where(LocationVisit.semantic_type.in_(["HOME", "INFERRED_HOME"]))
            .order_by(LocationVisit.start_time.desc())
            .limit(5000)
        )
        if home_recency_months and home_recency_months > 0:
            cutoff = datetime.now(UTC) - timedelta(days=home_recency_months * 30)
            # Mais on n'a peut-etre pas de donnees recentes : on prend les N mois precedant
            # la DERNIERE visite enregistree, pas par rapport a aujourd'hui.
            latest = (
                await db.execute(
                    select(func.max(LocationVisit.start_time))
                    .where(LocationVisit.semantic_type.in_(["HOME", "INFERRED_HOME"]))
                )
            ).scalar_one_or_none()
            if latest:
                cutoff = latest - timedelta(days=home_recency_months * 30)
                q = q.where(LocationVisit.start_time >= cutoff)

        home_visits = list((await db.execute(q)).all())

        # Si trop peu de HOME recents, on retombe sur tout l'historique
        if len(home_visits) < 5:
            home_visits = list((
                await db.execute(
                    select(LocationVisit.lat, LocationVisit.lng, LocationVisit.start_time)
                    .where(LocationVisit.semantic_type.in_(["HOME", "INFERRED_HOME"]))
                    .order_by(LocationVisit.start_time.desc())
                    .limit(5000)
                )
            ).all())

        if not home_visits:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Aucune visite HOME — fournis home_lat/home_lng en params ou utilise /retag",
            )
        # ── Cluster par grille 0.1° (~11km) → cellule la plus dense = home ──
        # Robuste si Marc a habite plusieurs villes (centroid moyen serait dans l'Atlantique).
        bins: dict[tuple[float, float], list[tuple[float, float]]] = {}
        for r in home_visits:
            lat, lng = float(r.lat), float(r.lng)
            key = (round(lat, 1), round(lng, 1))
            bins.setdefault(key, []).append((lat, lng))
        densest_key = max(bins.keys(), key=lambda k: len(bins[k]))
        cluster = bins[densest_key]
        h_lat = sum(p[0] for p in cluster) / len(cluster)
        h_lng = sum(p[1] for p in cluster) / len(cluster)
        logger.info(
            "trips_home_cluster",
            cluster_size=len(cluster),
            total_home_visits=len(home_visits),
            recency_months=home_recency_months,
            h_lat=round(h_lat, 4),
            h_lng=round(h_lng, 4),
        )
    else:
        h_lat, h_lng = home_lat, home_lng

    # 2. Toutes les visites chronologiquement
    visits = list((
        await db.execute(
            select(LocationVisit).order_by(LocationVisit.start_time)
        )
    ).scalars().all())

    if not visits:
        return TripsResponse(home_lat=h_lat, home_lng=h_lng, home_radius_km=home_radius_km,
                             min_duration_hours=min_duration_hours, trips=[])

    # 3. Pour chaque visite, calcule distance au home + flag "away"
    enriched = []
    for v in visits:
        dist = _haversine_km(h_lat, h_lng, float(v.lat), float(v.lng))
        away = dist > home_radius_km
        enriched.append((v, dist, away))

    # 4. Group consecutive "away" visits → trip candidates
    trips_raw: list[list[tuple[LocationVisit, float, bool]]] = []
    current: list[tuple[LocationVisit, float, bool]] = []
    for v, dist, away in enriched:
        if away:
            current.append((v, dist, away))
        else:
            if current:
                trips_raw.append(current)
                current = []
    if current:
        trips_raw.append(current)

    # 5. Filtre par duree min + distance min, calcule stats
    final_trips: list[Trip] = []
    for grp in trips_raw:
        first = grp[0][0]
        last = grp[-1][0]
        duration_h = (last.end_time - first.start_time).total_seconds() / 3600
        max_dist = max(d for _, d, _ in grp)

        if duration_h < min_duration_hours or max_dist < min_distance_km:
            continue

        # Top destinations (count par place_id ou cluster)
        dest_count: dict[str, dict[str, Any]] = {}
        for v, d, _ in grp:
            key = v.place_id or f"{round(float(v.lat), 3)},{round(float(v.lng), 3)}"
            existing = dest_count.setdefault(key, {
                "lat": float(v.lat), "lng": float(v.lng),
                "semantic_type": v.semantic_type, "count": 0,
                "distance_km": round(d, 1),
            })
            existing["count"] += 1
        top_dests = sorted(dest_count.values(), key=lambda x: -x["count"])[:5]

        # Activities pendant la periode → distance totale
        acts = list((
            await db.execute(
                select(LocationActivity.distance_meters)
                .where(LocationActivity.start_time >= first.start_time)
                .where(LocationActivity.end_time <= last.end_time)
            )
        ).scalars().all())
        total_dist_km = round(sum((d or 0) for d in acts) / 1000, 1)

        final_trips.append(Trip(
            start_date=first.start_time.date().isoformat(),
            end_date=last.end_time.date().isoformat(),
            duration_days=max(1, int(duration_h / 24)),
            visit_count=len(grp),
            activity_count=len(acts),
            total_distance_km=total_dist_km,
            max_distance_from_home_km=round(max_dist, 1),
            destinations=top_dests,
        ))

    final_trips.sort(key=lambda t: t.start_date, reverse=True)

    # ── Auto-naming via cache addresses ─────────────────────────────────
    # Charge toutes les addresses geocodees pour pouvoir lookup tolerant
    if final_trips:
        all_addrs = list((
            await db.execute(
                select(LocationAddress).where(LocationAddress.status == "ok")
            )
        ).scalars().all())
        addr_by_cell = {(a.lat_e4, a.lng_e4): a for a in all_addrs}

        def _find_addr(lat: float, lng: float, max_offset: int = 5) -> LocationAddress | None:
            """Lookup tolerant ±N cellules (1 cellule = 11m, 5 = 55m)."""
            base_lat = round(lat * 10000)
            base_lng = round(lng * 10000)
            # Rayon expansif
            for r in range(max_offset + 1):
                if r == 0:
                    addr = addr_by_cell.get((base_lat, base_lng))
                    if addr:
                        return addr
                    continue
                for dlat in range(-r, r + 1):
                    for dlng in range(-r, r + 1):
                        if abs(dlat) != r and abs(dlng) != r:
                            continue  # only edges
                        addr = addr_by_cell.get((base_lat + dlat, base_lng + dlng))
                        if addr:
                            return addr
            return None

        from datetime import date as _date
        month_fr = ["jan", "fév", "mars", "avr", "mai", "juin",
                    "juil", "août", "sept", "oct", "nov", "déc"]
        for t in final_trips:
            if not t.destinations:
                continue
            # Essaye chaque destination top (1 puis 2 puis 3)
            addr = None
            for d in t.destinations[:3]:
                addr = _find_addr(float(d["lat"]), float(d["lng"]))
                if addr:
                    break
            if not addr:
                continue
            t.primary_city = addr.city
            t.primary_country = addr.country
            start_d = _date.fromisoformat(t.start_date)
            mfr = month_fr[start_d.month - 1]
            if addr.city and addr.country:
                t.name = f"{addr.city}, {addr.country} · {mfr} {start_d.year}"
            elif addr.country:
                t.name = f"{addr.country} · {mfr} {start_d.year}"

    return TripsResponse(
        home_lat=h_lat, home_lng=h_lng, home_radius_km=home_radius_km,
        min_duration_hours=min_duration_hours, trips=final_trips,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reverse geocoding (Nominatim avec cache memoire)
# ─────────────────────────────────────────────────────────────────────────────


_GEOCODE_CACHE: dict[tuple[float, float], dict] = {}
_GEOCODE_LAST_REQUEST = [0.0]  # rate limit Nominatim 1 req/s


class ReverseGeocodeResponse(BaseModel):
    lat: float
    lng: float
    address: str | None
    house_number: str | None
    road: str | None
    city: str | None
    state: str | None
    country: str | None
    postcode: str | None
    cached: bool


# ─────────────────────────────────────────────────────────────────────────────
# Routes — top places / streaks / gaps / auto-detect work / year comparison
# ─────────────────────────────────────────────────────────────────────────────


class TopPlace(BaseModel):
    lat: float
    lng: float
    visit_count: int
    total_minutes: int
    semantic_types: list[str]
    first_visit: datetime | None
    last_visit: datetime | None
    label: str  # ex "HOME · 156 visites"


class TopPlacesResponse(BaseModel):
    bin_size_meters: int
    places: list[TopPlace]


@router.get(
    "/top-places",
    response_model=TopPlacesResponse,
    summary="Top N lieux les plus visites (binning par grille fine)",
)
async def get_top_places(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=10, ge=1, le=50),
    bin_degrees: float = Query(default=0.001, ge=0.0001, le=0.1, description="Taille bin (0.001=~111m)"),
    semantic_type: str | None = Query(default=None),
) -> TopPlacesResponse:
    q = select(LocationVisit)
    if semantic_type:
        q = q.where(LocationVisit.semantic_type == semantic_type)
    rows = list((await db.execute(q)).scalars().all())

    # Binning Python (compatible SQLite + PG sans tricks dialect-specific)
    bins: dict[tuple[float, float], dict[str, Any]] = {}
    for v in rows:
        lat_b = round(float(v.lat) / bin_degrees) * bin_degrees
        lng_b = round(float(v.lng) / bin_degrees) * bin_degrees
        key = (round(lat_b, 6), round(lng_b, 6))
        b = bins.setdefault(key, {
            "visits": [], "minutes": 0,
            "types": set(), "first": None, "last": None,
        })
        b["visits"].append(v)
        dur_min = max(0, (v.end_time - v.start_time).total_seconds() / 60)
        b["minutes"] += dur_min
        if v.semantic_type:
            b["types"].add(v.semantic_type)
        if b["first"] is None or v.start_time < b["first"]:
            b["first"] = v.start_time
        if b["last"] is None or v.start_time > b["last"]:
            b["last"] = v.start_time

    sorted_bins = sorted(bins.items(), key=lambda kv: -len(kv[1]["visits"]))[:limit]
    places: list[TopPlace] = []
    for (lat_b, lng_b), b in sorted_bins:
        types = sorted(b["types"]) or ["UNKNOWN"]
        primary = types[0] if "HOME" not in types else "HOME"
        if "WORK" in types and primary != "HOME":
            primary = "WORK"
        places.append(TopPlace(
            lat=lat_b, lng=lng_b,
            visit_count=len(b["visits"]),
            total_minutes=int(b["minutes"]),
            semantic_types=types,
            first_visit=b["first"], last_visit=b["last"],
            label=f"{primary} · {len(b['visits'])} visites",
        ))

    bin_size_m = int(bin_degrees * 111_000)
    return TopPlacesResponse(bin_size_meters=bin_size_m, places=places)


class Streak(BaseModel):
    label: str
    description: str
    value: int  # nombre de jours
    unit: str   # 'jours', 'mois', etc.
    period_start: str | None
    period_end: str | None


class StreaksResponse(BaseModel):
    streaks: list[Streak]


@router.get(
    "/streaks",
    response_model=StreaksResponse,
    summary="Streaks calcules : max sans avion, max consecutifs HOME, longest stay, etc.",
)
async def get_streaks(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreaksResponse:
    streaks: list[Streak] = []

    # ── Max streak sans avion (longest period without FLYING) ─────────────
    flights = list((
        await db.execute(
            select(LocationActivity.start_time, LocationActivity.end_time)
            .where(LocationActivity.activity_type == "FLYING")
            .order_by(LocationActivity.start_time)
        )
    ).all())

    earliest = (await db.execute(select(func.min(LocationVisit.start_time)))).scalar_one_or_none()
    latest = (await db.execute(select(func.max(LocationVisit.start_time)))).scalar_one_or_none()

    if flights and earliest and latest:
        flight_dates = [(f.start_time, f.end_time) for f in flights]
        # Calcule les "trous" entre vols + bord de plage
        boundaries = [(earliest, earliest)] + flight_dates + [(latest, latest)]
        max_gap_days = 0
        max_gap_start = None
        max_gap_end = None
        for i in range(len(boundaries) - 1):
            gap_start = boundaries[i][1]
            gap_end = boundaries[i + 1][0]
            gap_days = (gap_end - gap_start).days
            if gap_days > max_gap_days:
                max_gap_days = gap_days
                max_gap_start = gap_start
                max_gap_end = gap_end
        if max_gap_days > 0:
            streaks.append(Streak(
                label="Sans prendre l'avion",
                description=f"{len(flights)} vols enregistres au total",
                value=max_gap_days,
                unit="jours",
                period_start=max_gap_start.date().isoformat() if max_gap_start else None,
                period_end=max_gap_end.date().isoformat() if max_gap_end else None,
            ))

    # ── Max streak consecutif HOME (jours d'affilee a la maison) ──────────
    home_visits = list((
        await db.execute(
            select(LocationVisit.start_time)
            .where(LocationVisit.semantic_type.in_(["HOME", "INFERRED_HOME"]))
            .order_by(LocationVisit.start_time)
        )
    ).scalars().all())

    if home_visits:
        # Group by date
        home_dates = sorted(set(v.date() for v in home_visits))
        if home_dates:
            max_consec = current = 1
            streak_start = streak_end = home_dates[0]
            cur_start = home_dates[0]
            for i in range(1, len(home_dates)):
                if (home_dates[i] - home_dates[i - 1]).days == 1:
                    current += 1
                    if current > max_consec:
                        max_consec = current
                        streak_start = cur_start
                        streak_end = home_dates[i]
                else:
                    current = 1
                    cur_start = home_dates[i]
            streaks.append(Streak(
                label="Jours consecutifs a la maison",
                description=f"Plus longue periode chez soi sans bouger",
                value=max_consec, unit="jours",
                period_start=streak_start.isoformat(),
                period_end=streak_end.isoformat(),
            ))

    # ── Longest single visit (le sejour le plus long sans bouger) ──────────
    longest_visit_row = (
        await db.execute(
            select(LocationVisit)
            .order_by((LocationVisit.end_time - LocationVisit.start_time).desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if longest_visit_row:
        dur_min = max(0, (longest_visit_row.end_time - longest_visit_row.start_time).total_seconds() / 60)
        streaks.append(Streak(
            label="Plus longue visite",
            description=f"Le sejour ininterrompu le plus long ({longest_visit_row.semantic_type or 'lieu inconnu'})",
            value=int(dur_min / 60), unit="heures",
            period_start=longest_visit_row.start_time.date().isoformat(),
            period_end=longest_visit_row.end_time.date().isoformat(),
        ))

    # ── Most active day (jour avec le plus d'activites) ───────────────────
    is_sq = await _is_sqlite(db)
    if is_sq:
        date_expr = func.date(LocationActivity.start_time)
    else:
        date_expr = func.date(LocationActivity.start_time)
    activity_by_day = list((
        await db.execute(
            select(date_expr.label("day"), func.count().label("n"))
            .group_by(date_expr)
            .order_by(func.count().desc())
            .limit(1)
        )
    ).all())
    if activity_by_day:
        day_str = str(activity_by_day[0].day)
        n = activity_by_day[0].n
        streaks.append(Streak(
            label="Journee la plus active",
            description=f"Le record d'activites enregistrees en une journee",
            value=n, unit="activites",
            period_start=day_str, period_end=day_str,
        ))

    return StreaksResponse(streaks=streaks)


class DataGap(BaseModel):
    start_time: datetime
    end_time: datetime
    duration_hours: float
    duration_days: int


class DataGapsResponse(BaseModel):
    min_hours: int
    total_gaps: int
    total_missing_hours: int
    gaps: list[DataGap]


@router.get(
    "/gaps",
    response_model=DataGapsResponse,
    summary="Detecte les trous de donnees >X heures (telephone eteint, voyage hors couverture, etc.)",
)
async def get_data_gaps(
    db: Annotated[AsyncSession, Depends(get_db)],
    min_hours: int = Query(default=24, ge=2, le=8760),
    limit: int = Query(default=100, ge=1, le=1000),
) -> DataGapsResponse:
    visits = list((
        await db.execute(
            select(LocationVisit.start_time, LocationVisit.end_time)
            .order_by(LocationVisit.start_time)
        )
    ).all())

    gaps: list[DataGap] = []
    total_hours = 0
    for i in range(len(visits) - 1):
        gap_start = visits[i].end_time
        gap_end = visits[i + 1].start_time
        delta_h = (gap_end - gap_start).total_seconds() / 3600
        if delta_h >= min_hours:
            gaps.append(DataGap(
                start_time=gap_start, end_time=gap_end,
                duration_hours=round(delta_h, 1),
                duration_days=int(delta_h / 24),
            ))
            total_hours += delta_h

    # Tri par duree desc + limit
    gaps.sort(key=lambda g: -g.duration_hours)
    return DataGapsResponse(
        min_hours=min_hours,
        total_gaps=len(gaps),
        total_missing_hours=int(total_hours),
        gaps=gaps[:limit],
    )


class WorkDetectResponse(BaseModel):
    detected: bool
    lat: float | None
    lng: float | None
    visit_count: int
    confidence: float  # 0-1
    weekday_visits: int
    daytime_visits: int  # 9h-17h local
    label: str | None  # ex "Levis · 87 visites en semaine 9h-17h"


@router.get(
    "/auto-detect-work",
    response_model=WorkDetectResponse,
    summary="Detecte le lieu de travail probable depuis les patterns weekday-daytime",
)
async def auto_detect_work(
    db: Annotated[AsyncSession, Depends(get_db)],
    months_back: int = Query(default=6, ge=1, le=60),
) -> WorkDetectResponse:
    from datetime import timedelta
    latest = (await db.execute(select(func.max(LocationVisit.start_time)))).scalar_one_or_none()
    if latest is None:
        return WorkDetectResponse(detected=False, lat=None, lng=None,
                                  visit_count=0, confidence=0.0, weekday_visits=0,
                                  daytime_visits=0, label=None)
    cutoff = latest - timedelta(days=months_back * 30)

    visits = list((
        await db.execute(
            select(LocationVisit)
            .where(LocationVisit.start_time >= cutoff)
        )
    ).scalars().all())

    # Filtre : weekday + heures 8-17h (apres conversion tz_offset si dispo)
    bins: dict[tuple[float, float], list[Any]] = {}
    for v in visits:
        # Heure locale approchee
        if v.tz_offset_minutes:
            local = v.start_time + timedelta(minutes=v.tz_offset_minutes)
        else:
            local = v.start_time
        # Lundi=0 ... Dimanche=6
        if local.weekday() >= 5:
            continue  # weekend
        if local.hour < 8 or local.hour >= 17:
            continue  # nuit/soir
        # Skip HOME et apparentes (pas du travail)
        if v.semantic_type in ("HOME", "INFERRED_HOME"):
            continue

        lat_b = round(float(v.lat), 3)  # bins ~111m
        lng_b = round(float(v.lng), 3)
        bins.setdefault((lat_b, lng_b), []).append(v)

    if not bins:
        return WorkDetectResponse(detected=False, lat=None, lng=None,
                                  visit_count=0, confidence=0.0,
                                  weekday_visits=0, daytime_visits=0, label=None)

    # Cluster le plus dense
    densest_key = max(bins.keys(), key=lambda k: len(bins[k]))
    cluster = bins[densest_key]
    cluster_size = len(cluster)
    total_filtered = sum(len(b) for b in bins.values())
    confidence = round(cluster_size / total_filtered, 3) if total_filtered else 0
    avg_lat = sum(float(v.lat) for v in cluster) / cluster_size
    avg_lng = sum(float(v.lng) for v in cluster) / cluster_size

    return WorkDetectResponse(
        detected=True, lat=round(avg_lat, 6), lng=round(avg_lng, 6),
        visit_count=cluster_size, confidence=confidence,
        weekday_visits=cluster_size, daytime_visits=cluster_size,
        label=f"{cluster_size} visites en semaine 8-17h sur {months_back}m, confiance {int(confidence*100)}%",
    )


class YearMonthlyData(BaseModel):
    year: int
    monthly_visits: list[int]  # 12 valeurs jan..dec


class YearComparisonResponse(BaseModel):
    years: list[YearMonthlyData]


@router.get(
    "/year-comparison",
    response_model=YearComparisonResponse,
    summary="Visites par mois pour comparaison annee/annee",
)
async def get_year_comparison(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> YearComparisonResponse:
    is_sq = await _is_sqlite(db)
    if is_sq:
        year_expr = func.cast(func.strftime("%Y", LocationVisit.start_time), sa.Integer)
        month_expr = func.cast(func.strftime("%m", LocationVisit.start_time), sa.Integer)
    else:
        year_expr = func.extract("year", LocationVisit.start_time)
        month_expr = func.extract("month", LocationVisit.start_time)

    rows = list((
        await db.execute(
            select(year_expr.label("y"), month_expr.label("m"), func.count().label("n"))
            .group_by(year_expr, month_expr)
            .order_by(year_expr, month_expr)
        )
    ).all())

    years_map: dict[int, list[int]] = {}
    for r in rows:
        y = int(r.y)
        m = int(r.m)
        if y not in years_map:
            years_map[y] = [0] * 12
        years_map[y][m - 1] = r.n

    years = [YearMonthlyData(year=y, monthly_visits=v) for y, v in sorted(years_map.items())]
    return YearComparisonResponse(years=years)


# ─────────────────────────────────────────────────────────────────────────────
# Reverse geocoding (Nominatim avec cache memoire)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Batch geocoding worker
# ─────────────────────────────────────────────────────────────────────────────


# Etat du worker batch (module-level pour partage entre requests)
_GEOCODE_STATE = {
    "running": False,
    "total": 0,
    "processed": 0,
    "successes": 0,
    "errors": 0,
    "skipped": 0,
    "started_at": None,
    "last_address": None,
    "current_label": None,
    "stop_requested": False,
}


class GeocodeBatchRequest(BaseModel):
    only_unknown: bool = Field(
        default=False,
        description="Si True, ne geocode que les visites avec semantic_type=UNKNOWN. Sinon toutes.",
    )
    max_cells: int = Field(default=10000, ge=1, le=100000, description="Limite cellules a geocoder")


class GeocodeBatchResponse(BaseModel):
    started: bool
    total_to_process: int
    already_cached: int
    message: str


class GeocodeProgressResponse(BaseModel):
    running: bool
    total: int
    processed: int
    successes: int
    errors: int
    skipped: int
    pct: float
    started_at: datetime | None
    last_address: str | None
    current_label: str | None
    eta_seconds: int | None  # estimation


async def _geocode_worker(only_unknown: bool, max_cells: int) -> None:
    """Worker async qui parcourt les visites et geocode les cellules manquantes."""
    import asyncio
    import time
    import httpx

    try:
        async with SessionLocal() as db:
            # 1. Trouver les cellules uniques NON-cachees
            q = select(LocationVisit.lat, LocationVisit.lng, LocationVisit.semantic_type)
            if only_unknown:
                q = q.where(LocationVisit.semantic_type == "UNKNOWN")
            visits = list((await db.execute(q)).all())

            cells: dict[tuple[int, int], tuple[float, float]] = {}
            for v in visits:
                lat_e4 = round(float(v.lat) * 10000)
                lng_e4 = round(float(v.lng) * 10000)
                cells.setdefault((lat_e4, lng_e4), (float(v.lat), float(v.lng)))

            # Filtrer celles deja cachees
            cached_rows = list((
                await db.execute(select(LocationAddress.lat_e4, LocationAddress.lng_e4))
            ).all())
            cached_keys = {(r.lat_e4, r.lng_e4) for r in cached_rows}
            todo = [(k, v) for k, v in cells.items() if k not in cached_keys]
            todo = todo[:max_cells]

            _GEOCODE_STATE["total"] = len(todo)
            _GEOCODE_STATE["processed"] = 0
            _GEOCODE_STATE["successes"] = 0
            _GEOCODE_STATE["errors"] = 0
            _GEOCODE_STATE["skipped"] = 0

            if not todo:
                _GEOCODE_STATE["running"] = False
                logger.info("geocode_batch_nothing_to_do")
                return

            # 2. Boucle Nominatim avec rate-limit 1.1s
            async with httpx.AsyncClient(timeout=15) as client:
                last_t = 0.0
                for (lat_e4, lng_e4), (lat, lng) in todo:
                    if _GEOCODE_STATE["stop_requested"]:
                        logger.info("geocode_batch_stopped")
                        break

                    elapsed = time.time() - last_t
                    if elapsed < 1.1:
                        await asyncio.sleep(1.1 - elapsed)
                    last_t = time.time()

                    _GEOCODE_STATE["current_label"] = f"{lat:.4f}°, {lng:.4f}°"

                    try:
                        r = await client.get(
                            "https://nominatim.openstreetmap.org/reverse",
                            params={
                                "lat": lat, "lon": lng, "format": "jsonv2",
                                "accept-language": "fr,en", "zoom": 18,
                            },
                            headers={
                                "User-Agent": "PersonalDataHub/1.0 "
                                "(private use, marc.richard4@gmail.com)",
                            },
                        )
                        r.raise_for_status()
                        data = r.json()
                        addr = data.get("address", {})

                        # Insere
                        async with SessionLocal() as inner_db:
                            row = LocationAddress(
                                lat_e4=lat_e4, lng_e4=lng_e4, lat=lat, lng=lng,
                                display_name=data.get("display_name"),
                                house_number=addr.get("house_number"),
                                road=addr.get("road"),
                                suburb=addr.get("suburb") or addr.get("neighbourhood"),
                                city=(addr.get("city") or addr.get("town")
                                      or addr.get("village") or addr.get("municipality")),
                                state=addr.get("state") or addr.get("province"),
                                postcode=addr.get("postcode"),
                                country=addr.get("country"),
                                country_code=addr.get("country_code", "")[:2] or None,
                                osm_type=data.get("osm_type"),
                                osm_id=str(data.get("osm_id")) if data.get("osm_id") else None,
                                status="ok" if data.get("display_name") else "no_result",
                            )
                            inner_db.add(row)
                            await inner_db.commit()
                        _GEOCODE_STATE["successes"] += 1
                        _GEOCODE_STATE["last_address"] = (
                            data.get("display_name", "")[:100] if data.get("display_name") else None
                        )
                    except (httpx.HTTPError, ValueError) as exc:
                        _GEOCODE_STATE["errors"] += 1
                        logger.warning(
                            "nominatim_batch_error",
                            cell=(lat_e4, lng_e4), error=str(exc)[:100],
                        )
                        # Insere quand meme un row "failed" pour pas re-tenter
                        try:
                            async with SessionLocal() as inner_db:
                                row = LocationAddress(
                                    lat_e4=lat_e4, lng_e4=lng_e4, lat=lat, lng=lng,
                                    status="failed", error=str(exc)[:200],
                                )
                                inner_db.add(row)
                                await inner_db.commit()
                        except Exception:
                            pass

                    _GEOCODE_STATE["processed"] += 1
    finally:
        _GEOCODE_STATE["running"] = False
        _GEOCODE_STATE["current_label"] = None


@router.post(
    "/geocode-batch",
    response_model=GeocodeBatchResponse,
    summary="Lance le reverse-geocoding en masse de toutes les cellules sans adresse",
)
async def geocode_batch(
    payload: GeocodeBatchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GeocodeBatchResponse:
    import asyncio
    if _GEOCODE_STATE["running"]:
        return GeocodeBatchResponse(
            started=False, total_to_process=0, already_cached=0,
            message="Un job de geocoding tourne deja",
        )

    # Calcul rapide des cellules a faire
    q = select(LocationVisit.lat, LocationVisit.lng)
    if payload.only_unknown:
        q = q.where(LocationVisit.semantic_type == "UNKNOWN")
    visits = list((await db.execute(q)).all())
    cells = {(round(float(v.lat) * 10000), round(float(v.lng) * 10000)) for v in visits}
    cached = list((await db.execute(
        select(LocationAddress.lat_e4, LocationAddress.lng_e4)
    )).all())
    cached_keys = {(r.lat_e4, r.lng_e4) for r in cached}
    to_do = cells - cached_keys
    n_todo = min(len(to_do), payload.max_cells)

    if n_todo == 0:
        return GeocodeBatchResponse(
            started=False, total_to_process=0, already_cached=len(cached_keys),
            message="Toutes les cellules sont deja geocodees",
        )

    _GEOCODE_STATE["running"] = True
    _GEOCODE_STATE["stop_requested"] = False
    _GEOCODE_STATE["started_at"] = datetime.now(UTC)
    _GEOCODE_STATE["last_address"] = None
    _GEOCODE_STATE["current_label"] = None

    asyncio.create_task(_geocode_worker(payload.only_unknown, payload.max_cells))

    eta_min = round(n_todo * 1.1 / 60, 1)
    return GeocodeBatchResponse(
        started=True, total_to_process=n_todo, already_cached=len(cached_keys),
        message=f"Job demarre. {n_todo} cellules a geocoder (~{eta_min} min a 1 req/s)",
    )


@router.get(
    "/geocode-progress",
    response_model=GeocodeProgressResponse,
    summary="Etat du job de geocoding batch en cours",
)
async def geocode_progress() -> GeocodeProgressResponse:
    s = _GEOCODE_STATE
    pct = (s["processed"] / s["total"] * 100) if s["total"] > 0 else 0.0
    eta_s = None
    if s["running"] and s["processed"] > 0 and s["started_at"]:
        elapsed_s = (datetime.now(UTC) - s["started_at"]).total_seconds()
        rate = s["processed"] / elapsed_s if elapsed_s else 1
        remaining = s["total"] - s["processed"]
        eta_s = int(remaining / rate) if rate > 0 else None
    return GeocodeProgressResponse(
        running=s["running"],
        total=s["total"], processed=s["processed"],
        successes=s["successes"], errors=s["errors"], skipped=s["skipped"],
        pct=round(pct, 1),
        started_at=s["started_at"],
        last_address=s["last_address"],
        current_label=s["current_label"],
        eta_seconds=eta_s,
    )


@router.post(
    "/geocode-stop",
    summary="Arrete le job de geocoding en cours",
)
async def geocode_stop() -> dict:
    _GEOCODE_STATE["stop_requested"] = True
    return {"stopped": True, "was_running": _GEOCODE_STATE["running"]}


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint d'enrichissement : visite -> adresse (lookup cache)
# ─────────────────────────────────────────────────────────────────────────────


class VisitWithAddress(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    start_time: datetime
    end_time: datetime
    lat: Decimal
    lng: Decimal
    semantic_type: str | None
    place_id: str | None
    address: str | None  # short label
    full_address: str | None
    city: str | None
    country: str | None


class CountryStat(BaseModel):
    country: str
    country_code: str | None
    cell_count: int
    visit_count: int
    cities: list[str]


class RegionsResponse(BaseModel):
    countries_count: int
    cities_count: int
    cells_geocoded: int
    countries: list[CountryStat]


@router.get(
    "/regions",
    response_model=RegionsResponse,
    summary="Compteur villes/pays uniques visites + breakdown par pays",
)
async def get_regions(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RegionsResponse:
    addrs = list((
        await db.execute(
            select(LocationAddress).where(LocationAddress.status == "ok")
        )
    ).scalars().all())

    if not addrs:
        return RegionsResponse(
            countries_count=0, cities_count=0, cells_geocoded=0, countries=[],
        )

    # Compte les visites par cellule pour ponderer (un meme lieu n'est pas N pays differents)
    visit_rows = list((
        await db.execute(select(LocationVisit.lat, LocationVisit.lng))
    ).all())
    visit_count_by_cell: dict[tuple[int, int], int] = {}
    for v in visit_rows:
        key = (round(float(v.lat) * 10000), round(float(v.lng) * 10000))
        visit_count_by_cell[key] = visit_count_by_cell.get(key, 0) + 1

    # Aggregation par pays
    country_data: dict[str, dict] = {}
    cities_set: set[tuple[str, str]] = set()  # (country_code, city)

    for a in addrs:
        if not a.country:
            continue
        cell = (a.lat_e4, a.lng_e4)
        visits_here = visit_count_by_cell.get(cell, 0)
        if a.city:
            cities_set.add((a.country_code or a.country, a.city))

        country_key = a.country
        c = country_data.setdefault(country_key, {
            "country": a.country,
            "country_code": a.country_code,
            "cell_count": 0,
            "visit_count": 0,
            "cities": set(),
        })
        c["cell_count"] += 1
        c["visit_count"] += visits_here
        if a.city:
            c["cities"].add(a.city)

    countries = sorted(
        [
            CountryStat(
                country=c["country"], country_code=c["country_code"],
                cell_count=c["cell_count"], visit_count=c["visit_count"],
                cities=sorted(c["cities"])[:20],  # top 20 villes
            )
            for c in country_data.values()
        ],
        key=lambda c: -c.visit_count,
    )

    return RegionsResponse(
        countries_count=len(country_data),
        cities_count=len(cities_set),
        cells_geocoded=len(addrs),
        countries=countries,
    )


class AddressLite(BaseModel):
    lat_e4: int
    lng_e4: int
    label: str | None
    city: str | None
    country: str | None
    country_code: str | None


class AddressesIndexResponse(BaseModel):
    total: int
    addresses: list[AddressLite]


@router.get(
    "/addresses",
    response_model=AddressesIndexResponse,
    summary="Index leger de toutes les adresses geocodees (lookup frontend)",
)
async def list_addresses(
    db: Annotated[AsyncSession, Depends(get_db)],
    country: str | None = Query(default=None),
) -> AddressesIndexResponse:
    q = select(LocationAddress).where(LocationAddress.status == "ok")
    if country:
        q = q.where(LocationAddress.country_code == country.lower())
    rows = list((await db.execute(q)).scalars().all())
    return AddressesIndexResponse(
        total=len(rows),
        addresses=[
            AddressLite(
                lat_e4=r.lat_e4, lng_e4=r.lng_e4,
                label=r.short_label(), city=r.city,
                country=r.country, country_code=r.country_code,
            )
            for r in rows
        ],
    )


@router.get(
    "/visits-with-addresses",
    response_model=list[VisitWithAddress],
    summary="Liste des visites enrichies avec leur adresse depuis le cache de geocoding",
)
async def list_visits_with_addresses(
    db: Annotated[AsyncSession, Depends(get_db)],
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    semantic_type: str | None = Query(default=None),
    has_address: bool | None = Query(default=None, description="Filtre : seulement avec/sans adresse"),
    limit: int = Query(default=200, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
) -> list[VisitWithAddress]:
    q = select(LocationVisit).order_by(LocationVisit.start_time.desc())
    if start_date:
        q = q.where(LocationVisit.start_time >= datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC))
    if end_date:
        q = q.where(LocationVisit.start_time <= datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=UTC))
    if semantic_type:
        q = q.where(LocationVisit.semantic_type == semantic_type)
    q = q.limit(limit).offset(offset)
    visits = list((await db.execute(q)).scalars().all())

    # Charge tous les addresses correspondants en 1 query
    cells = {(round(float(v.lat) * 10000), round(float(v.lng) * 10000)) for v in visits}
    if cells:
        addr_rows = list((
            await db.execute(
                select(LocationAddress).where(
                    sa.tuple_(LocationAddress.lat_e4, LocationAddress.lng_e4).in_(list(cells))
                )
            )
        ).scalars().all())
        addr_map = {(a.lat_e4, a.lng_e4): a for a in addr_rows}
    else:
        addr_map = {}

    results: list[VisitWithAddress] = []
    for v in visits:
        key = (round(float(v.lat) * 10000), round(float(v.lng) * 10000))
        addr = addr_map.get(key)
        short = addr.short_label() if addr else None
        if has_address is True and not short:
            continue
        if has_address is False and short:
            continue
        results.append(VisitWithAddress(
            id=v.id, start_time=v.start_time, end_time=v.end_time,
            lat=v.lat, lng=v.lng,
            semantic_type=v.semantic_type, place_id=v.place_id,
            address=short,
            full_address=addr.display_name if addr else None,
            city=addr.city if addr else None,
            country=addr.country if addr else None,
        ))
    return results


@router.get(
    "/reverse-geocode",
    response_model=ReverseGeocodeResponse,
    summary="Reverse geocode lat/lng -> adresse via OpenStreetMap Nominatim (cache memoire)",
)
async def reverse_geocode(
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    db: Annotated[AsyncSession, Depends(get_db)] = ...,
) -> ReverseGeocodeResponse:
    import asyncio
    import time
    import httpx

    # 1. Check cache DB (location_addresses)
    lat_e4 = round(lat * 10000)
    lng_e4 = round(lng * 10000)
    db_row = (await db.execute(
        select(LocationAddress)
        .where(LocationAddress.lat_e4 == lat_e4)
        .where(LocationAddress.lng_e4 == lng_e4)
    )).scalar_one_or_none()
    if db_row and db_row.status == "ok":
        return ReverseGeocodeResponse(
            lat=lat, lng=lng, cached=True,
            address=db_row.display_name,
            house_number=db_row.house_number, road=db_row.road,
            city=db_row.city, state=db_row.state,
            country=db_row.country, postcode=db_row.postcode,
        )

    # 2. Check cache memoire
    key = (round(lat, 4), round(lng, 4))  # ~11m precision
    if key in _GEOCODE_CACHE:
        cached = _GEOCODE_CACHE[key]
        return ReverseGeocodeResponse(lat=lat, lng=lng, cached=True, **cached)

    # Rate limit Nominatim policy : 1 req/s max
    elapsed = time.time() - _GEOCODE_LAST_REQUEST[0]
    if elapsed < 1.1:
        await asyncio.sleep(1.1 - elapsed)
    _GEOCODE_LAST_REQUEST[0] = time.time()

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lng, "format": "jsonv2",
                        "accept-language": "fr,en"},
                headers={"User-Agent": "PersonalDataHub/1.0 (private use, marc.richard4@gmail.com)"},
            )
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("nominatim_error", error=str(exc)[:100])
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Nominatim error: {exc}") from exc

    addr = data.get("address", {})
    result = {
        "address": data.get("display_name"),
        "house_number": addr.get("house_number"),
        "road": addr.get("road"),
        "city": addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality"),
        "state": addr.get("state") or addr.get("province"),
        "country": addr.get("country"),
        "postcode": addr.get("postcode"),
    }
    _GEOCODE_CACHE[key] = result

    return ReverseGeocodeResponse(lat=lat, lng=lng, cached=False, **result)
