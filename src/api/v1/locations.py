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
from src.db.models import LocationActivity, LocationPoint, LocationVisit
from src.db.session import get_db

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
    limit: int = Query(default=500, ge=1, le=10000),
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
    limit: int = Query(default=200, ge=1, le=5000),
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
