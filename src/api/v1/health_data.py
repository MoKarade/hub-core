"""Endpoint /v1/health-data - ingest Google Fit (Phase 4).

Pull les datapoints sante via Fitness REST API et stocke comme HealthMetric.
Couvre : steps, distance, calories, active minutes, sleep, weight, heart rate.

Endpoint Google Fit :
POST https://www.googleapis.com/fitness/v1/users/me/dataset:aggregate
Body : {aggregateBy: [{dataTypeName}], bucketByTime: {durationMillis: 86400000},
        startTimeMillis, endTimeMillis}
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.db.models import HealthMetric
from src.db.session import get_db
from src.services.oauth_google import get_valid_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health-data", tags=["health-data"])
_OWNER_EMAIL: str = get_settings().hub_owner_email

FIT_API = "https://www.googleapis.com/fitness/v1"

# Mapping Google Fit dataTypeName -> (nom local, methode d'agregation)
# avg : moyenne (heart_rate) | sum : total (steps) | last : derniere val (weight)
FIT_DATA_TYPES: dict[str, tuple[str, str]] = {
    "com.google.step_count.delta": ("steps", "sum"),
    "com.google.distance.delta": ("distance_m", "sum"),
    "com.google.calories.expended": ("calories", "sum"),
    "com.google.active_minutes": ("active_minutes", "sum"),
    "com.google.weight": ("weight_kg", "last"),
    "com.google.heart_rate.bpm": ("heart_rate_avg", "avg"),
    "com.google.heart_minutes": ("heart_minutes", "sum"),
    "com.google.body.fat.percentage": ("body_fat_pct", "last"),
    "com.google.oxygen_saturation": ("oxygen_saturation", "avg"),
    "com.google.blood_pressure": ("blood_pressure_systolic", "avg"),
    "com.google.body.temperature": ("body_temp_c", "avg"),
    "com.google.hydration": ("hydration_l", "sum"),
    "com.google.height": ("height_m", "last"),
    "com.google.power.sample": ("power_w", "avg"),
    "com.google.speed": ("speed_avg_ms", "avg"),
    "com.google.cycling.pedaling.cadence": ("cycling_cadence_rpm", "avg"),
    "com.google.cycling.wheel_revolution.cumulative": ("cycling_wheel_revs", "last"),
    "com.google.activity.segment": ("activity_segments", "sum"),
}

# Sleep activityType code = 72 (Google Fit)
SLEEP_ACTIVITY_TYPE = 72
FIT_SESSIONS_API = "https://www.googleapis.com/fitness/v1/users/me/sessions"


class HealthSyncRequest(BaseModel):
    user_email: str = Field(default=_OWNER_EMAIL)
    days_back: int = Field(default=90, ge=1, le=3650)


class HealthSyncResponse(BaseModel):
    metrics_ingested: int
    metrics_updated: int
    errors: int
    duration_seconds: float


class HealthMetricItem(BaseModel):
    id: UUID
    date: date
    metric: str
    value: float
    source: str


async def _resolve_token(db: AsyncSession, user_email: str) -> str:
    for service in ("fitness", "all"):
        try:
            return await get_valid_access_token(db, service=service, user_email=user_email)
        except RuntimeError:
            continue
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Pas de token Google Fit pour {user_email}")


async def _aggregate(
    client: httpx.AsyncClient,
    access_token: str,
    data_type: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    """Bucketize par jour et aggregate (sum). Retourne liste de buckets."""
    body = {
        "aggregateBy": [{"dataTypeName": data_type}],
        "bucketByTime": {"durationMillis": 86400000},
        "startTimeMillis": start_ms,
        "endTimeMillis": end_ms,
    }
    r = await client.post(
        f"{FIT_API}/users/me/dataset:aggregate",
        headers={"Authorization": f"Bearer {access_token}"},
        json=body,
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json().get("bucket", [])


def _extract_value(point: dict[str, Any], data_type: str) -> float | None:
    """Extract la valeur principale d'un dataPoint Google Fit."""
    values = point.get("value", [])
    if not values:
        return None
    v = values[0]
    if "intVal" in v:
        return float(v["intVal"])
    if "fpVal" in v:
        return float(v["fpVal"])
    return None


@router.post("/sync", response_model=HealthSyncResponse)
async def sync_health(
    payload: HealthSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HealthSyncResponse:
    start = time.monotonic()
    access_token = await _resolve_token(db, payload.user_email)

    end_ts = datetime.now(UTC)
    start_ts = end_ts - timedelta(days=payload.days_back)
    start_ms = int(start_ts.timestamp() * 1000)
    end_ms = int(end_ts.timestamp() * 1000)

    ingested = 0
    updated = 0
    errors = 0

    async with httpx.AsyncClient() as client:
        for data_type, (metric_name, agg) in FIT_DATA_TYPES.items():
            try:
                buckets = await _aggregate(client, access_token, data_type, start_ms, end_ms)
            except httpx.HTTPStatusError as e:
                logger.warning(
                    "fit_aggregate_failed: type=%s status=%d body=%s",
                    data_type,
                    e.response.status_code,
                    e.response.text[:200],
                )
                errors += 1
                continue

            for bucket in buckets:
                bucket_start_ms = int(bucket.get("startTimeMillis", 0))
                if not bucket_start_ms:
                    continue
                bucket_date = datetime.fromtimestamp(bucket_start_ms / 1000, tz=UTC).date()

                vals: list[float] = []
                for ds in bucket.get("dataset", []):
                    for point in ds.get("point", []):
                        val = _extract_value(point, data_type)
                        if val is not None:
                            vals.append(val)

                if not vals:
                    continue

                # Aggregation selon la strategie (sum/avg/last)
                if agg == "avg":
                    final = sum(vals) / len(vals)
                elif agg == "last":
                    final = vals[-1]
                else:  # sum
                    final = sum(vals)

                # UPSERT par (user_email, date, metric, source)
                existing = (
                    await db.execute(
                        select(HealthMetric).where(
                            HealthMetric.user_email == payload.user_email,
                            HealthMetric.date == bucket_date,
                            HealthMetric.metric == metric_name,
                            HealthMetric.source == "google_fit",
                        )
                    )
                ).scalar_one_or_none()

                if existing:
                    existing.value = final
                    updated += 1
                else:
                    db.add(
                        HealthMetric(
                            user_email=payload.user_email,
                            date=bucket_date,
                            metric=metric_name,
                            value=final,
                            source="google_fit",
                        )
                    )
                    ingested += 1

    await db.commit()

    return HealthSyncResponse(
        metrics_ingested=ingested,
        metrics_updated=updated,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("/metrics", response_model=list[HealthMetricItem])
async def list_metrics(
    db: Annotated[AsyncSession, Depends(get_db)],
    metric: Annotated[str | None, Query()] = None,
    since: Annotated[date | None, Query()] = None,
    until: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> list[HealthMetricItem]:
    stmt = select(HealthMetric).order_by(desc(HealthMetric.date))
    if metric:
        stmt = stmt.where(HealthMetric.metric == metric)
    if since:
        stmt = stmt.where(HealthMetric.date >= since)
    if until:
        stmt = stmt.where(HealthMetric.date <= until)
    stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        HealthMetricItem(id=r.id, date=r.date, metric=r.metric, value=r.value, source=r.source)
        for r in rows
    ]


class HealthSummary(BaseModel):
    total_datapoints: int
    by_metric: list[dict[str, Any]]
    """Par metric: count + last_date + last_value + 7d_avg."""


@router.get("/summary", response_model=HealthSummary)
async def health_summary(db: Annotated[AsyncSession, Depends(get_db)]) -> HealthSummary:
    total = (await db.execute(select(func.count(HealthMetric.id)))).scalar() or 0
    cutoff_7d = datetime.now(UTC).date() - timedelta(days=7)
    cutoff_14d = datetime.now(UTC).date() - timedelta(days=14)
    cutoff_30d = datetime.now(UTC).date() - timedelta(days=30)
    by_q = (
        select(
            HealthMetric.metric,
            func.count(HealthMetric.id).label("count"),
            func.max(HealthMetric.date).label("last_date"),
            func.avg(HealthMetric.value).label("avg"),
            func.max(HealthMetric.value).label("max"),
            func.min(HealthMetric.value).label("min"),
        )
        .group_by(HealthMetric.metric)
        .order_by(desc("count"))
    )
    rows = (await db.execute(by_q)).all()

    by_metric = []
    for r in rows:
        metric_name = r[0]
        # Recent value
        last_val_q = (
            select(HealthMetric.value)
            .where(HealthMetric.metric == metric_name)
            .order_by(desc(HealthMetric.date))
            .limit(1)
        )
        last_val = (await db.execute(last_val_q)).scalar()

        # Avg this week vs last week (compare trends)
        avg_7d = (
            await db.execute(
                select(func.avg(HealthMetric.value)).where(
                    HealthMetric.metric == metric_name, HealthMetric.date >= cutoff_7d
                )
            )
        ).scalar()
        avg_prev_7d = (
            await db.execute(
                select(func.avg(HealthMetric.value)).where(
                    HealthMetric.metric == metric_name,
                    HealthMetric.date >= cutoff_14d,
                    HealthMetric.date < cutoff_7d,
                )
            )
        ).scalar()
        avg_30d = (
            await db.execute(
                select(func.avg(HealthMetric.value)).where(
                    HealthMetric.metric == metric_name,
                    HealthMetric.date >= cutoff_30d,
                )
            )
        ).scalar()

        by_metric.append(
            {
                "metric": metric_name,
                "count": int(r[1]),
                "last_date": r[2].isoformat() if r[2] else None,
                "last_value": round(float(last_val), 2) if last_val is not None else None,
                "avg_90d": round(float(r[3]), 2) if r[3] is not None else None,
                "max_90d": round(float(r[4]), 2) if r[4] is not None else None,
                "min_90d": round(float(r[5]), 2) if r[5] is not None else None,
                "avg_7d": round(float(avg_7d), 2) if avg_7d is not None else None,
                "avg_prev_7d": round(float(avg_prev_7d), 2) if avg_prev_7d is not None else None,
                "avg_30d": round(float(avg_30d), 2) if avg_30d is not None else None,
            }
        )

    return HealthSummary(total_datapoints=total, by_metric=by_metric)


class TimeseriesPoint(BaseModel):
    date: date
    value: float


@router.get("/timeseries", response_model=list[TimeseriesPoint])
async def metric_timeseries(
    db: Annotated[AsyncSession, Depends(get_db)],
    metric: Annotated[str, Query()],
    days: Annotated[int, Query(ge=1, le=3650)] = 90,
) -> list[TimeseriesPoint]:
    """Time series d'une metric pour les N derniers jours (pour charts)."""
    cutoff = datetime.now(UTC).date() - timedelta(days=days)
    rows = (
        await db.execute(
            select(HealthMetric.date, HealthMetric.value)
            .where(HealthMetric.metric == metric, HealthMetric.date >= cutoff)
            .order_by(HealthMetric.date)
        )
    ).all()
    return [TimeseriesPoint(date=r[0], value=float(r[1])) for r in rows]
