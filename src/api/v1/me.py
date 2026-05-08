"""Endpoint /v1/me/dashboard — vue cross-domain agregee de toutes les sources.

But : 1 seul endpoint qui retourne TOUS les chiffres importants pour Marc
sur une periode donnee (7d / 30d / 90d / 365d / all). Permet une page
frontend `/me` qui affiche le tableau de bord life metrics complet.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import (
    BrowserHistory,
    CalendarEvent,
    Contact,
    DriveFile,
    Email,
    HealthMetric,
    LocationAddress,
    LocationVisit,
    NewsArticle,
    Photo,
    RemovalRequest,
    SteamGame,
    SteamPlaySnapshot,
    StreamingActivity,
    Task,
    Transaction,
    YouTubeActivity,
)
from src.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/me", tags=["me"])


PERIOD_DAYS: dict[str, int | None] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "365d": 365,
    "all": None,
}


class SourceCounts(BaseModel):
    """Counts par source sur la periode."""

    transactions: int = 0
    location_visits: int = 0
    location_unique_places: int = 0
    photos: int = 0
    emails: int = 0
    calendar_events: int = 0
    tasks_completed: int = 0
    tasks_pending: int = 0
    health_datapoints: int = 0
    contacts_total: int = 0
    drive_files_total: int = 0
    youtube_activities: int = 0
    streaming_episodes: int = 0
    streaming_movies: int = 0
    browser_visits: int = 0
    browser_unique_domains: int = 0
    steam_games_played: int = 0
    news_articles: int = 0
    privacy_requests: int = 0


class FinanceSection(BaseModel):
    total_spend_cad: float = 0
    total_credit_cad: float = 0
    net_cad: float = 0
    biggest_debit_amount: float | None = None
    biggest_debit_desc: str | None = None
    transactions_count: int = 0


class HealthSection(BaseModel):
    avg_steps: float | None = None
    avg_sleep_hours: float | None = None
    avg_resting_hr: float | None = None
    avg_stress: float | None = None
    avg_hrv: float | None = None
    total_active_min: int | None = None
    days_with_data: int = 0


class LocationSection(BaseModel):
    visits: int = 0
    unique_places: int = 0
    home_visits: int = 0
    work_visits: int = 0
    last_home_iso: str | None = None
    days_since_home: int | None = None
    most_visited_place: str | None = None
    most_visited_count: int = 0


class ScreenTimeSection(BaseModel):
    """Temps passe sur chaque type d'activite (browser + gaming)."""

    browser_visits: int = 0
    browser_top_domains: list[dict[str, Any]] = []
    gaming_minutes: int = 0
    gaming_top_games: list[dict[str, Any]] = []
    streaming_total_runtime_h: float = 0


class ProductivitySection(BaseModel):
    tasks_completed: int = 0
    tasks_pending: int = 0
    tasks_overdue: int = 0
    completion_rate_pct: float | None = None
    calendar_events: int = 0


class DashboardResponse(BaseModel):
    period: str
    period_days: int | None
    generated_at: datetime
    counts: SourceCounts
    finance: FinanceSection
    health: HealthSection
    locations: LocationSection
    screen_time: ScreenTimeSection
    productivity: ProductivitySection


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


async def _safe_count(db: AsyncSession, stmt) -> int:
    """Wrapper resilient : retourne 0 si la table/colonne n'existe pas."""
    try:
        n = (await db.execute(stmt)).scalar_one_or_none()
        return int(n or 0)
    except Exception as e:
        logger.warning("safe_count_failed err=%r", e)
        return 0


async def _safe_scalar(db: AsyncSession, stmt) -> Any:
    try:
        return (await db.execute(stmt)).scalar_one_or_none()
    except Exception as e:
        logger.warning("safe_scalar_failed err=%r", e)
        return None


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


# ─────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────


@router.get("/dashboard", response_model=DashboardResponse)
async def me_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    period: Annotated[str, Query(description="7d|30d|90d|365d|all")] = "30d",
) -> DashboardResponse:
    """Tableau de bord cross-domain pour Marc."""
    if period not in PERIOD_DAYS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"period invalide ({period}). Valeurs : {list(PERIOD_DAYS)}",
        )

    now = datetime.now(UTC)
    days = PERIOD_DAYS[period]
    cutoff_dt = now - timedelta(days=days) if days else None
    cutoff_date = cutoff_dt.date() if cutoff_dt else None

    # ── Source counts ───────────────────────────────────────────────────
    counts = SourceCounts()

    if cutoff_date:
        counts.transactions = await _safe_count(
            db,
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.transaction_date >= cutoff_date),
        )
        counts.location_visits = await _safe_count(
            db,
            select(func.count())
            .select_from(LocationVisit)
            .where(LocationVisit.start_time >= cutoff_dt),
        )
        counts.location_unique_places = await _safe_count(
            db,
            select(func.count(func.distinct(LocationVisit.place_id))).where(
                LocationVisit.start_time >= cutoff_dt
            ),
        )
        counts.photos = await _safe_count(
            db,
            select(func.count()).select_from(Photo).where(Photo.creation_time >= cutoff_dt),
        )
        counts.emails = await _safe_count(
            db,
            select(func.count()).select_from(Email).where(Email.sent_at >= cutoff_dt),
        )
        counts.calendar_events = await _safe_count(
            db,
            select(func.count())
            .select_from(CalendarEvent)
            .where(CalendarEvent.start_at >= cutoff_dt),
        )
        counts.health_datapoints = await _safe_count(
            db,
            select(func.count()).select_from(HealthMetric).where(HealthMetric.date >= cutoff_date),
        )
        counts.youtube_activities = await _safe_count(
            db,
            select(func.count())
            .select_from(YouTubeActivity)
            .where(YouTubeActivity.published_at >= cutoff_dt),
        )
        counts.streaming_episodes = await _safe_count(
            db,
            select(func.count())
            .select_from(StreamingActivity)
            .where(StreamingActivity.watched_at >= cutoff_dt)
            .where(StreamingActivity.item_type == "episode"),
        )
        counts.streaming_movies = await _safe_count(
            db,
            select(func.count())
            .select_from(StreamingActivity)
            .where(StreamingActivity.watched_at >= cutoff_dt)
            .where(StreamingActivity.item_type == "movie"),
        )
        counts.browser_visits = await _safe_count(
            db,
            select(func.count())
            .select_from(BrowserHistory)
            .where(BrowserHistory.visited_at >= cutoff_dt),
        )
        counts.browser_unique_domains = await _safe_count(
            db,
            select(func.count(func.distinct(BrowserHistory.domain))).where(
                BrowserHistory.visited_at >= cutoff_dt
            ),
        )
        counts.news_articles = await _safe_count(
            db,
            select(func.count())
            .select_from(NewsArticle)
            .where(NewsArticle.published_at >= cutoff_dt),
        )
    else:
        # all-time
        counts.transactions = await _safe_count(db, select(func.count()).select_from(Transaction))
        counts.location_visits = await _safe_count(
            db, select(func.count()).select_from(LocationVisit)
        )
        counts.location_unique_places = await _safe_count(
            db, select(func.count(func.distinct(LocationVisit.place_id)))
        )
        counts.photos = await _safe_count(db, select(func.count()).select_from(Photo))
        counts.emails = await _safe_count(db, select(func.count()).select_from(Email))
        counts.calendar_events = await _safe_count(
            db, select(func.count()).select_from(CalendarEvent)
        )
        counts.health_datapoints = await _safe_count(
            db, select(func.count()).select_from(HealthMetric)
        )
        counts.youtube_activities = await _safe_count(
            db, select(func.count()).select_from(YouTubeActivity)
        )
        counts.streaming_episodes = await _safe_count(
            db,
            select(func.count())
            .select_from(StreamingActivity)
            .where(StreamingActivity.item_type == "episode"),
        )
        counts.streaming_movies = await _safe_count(
            db,
            select(func.count())
            .select_from(StreamingActivity)
            .where(StreamingActivity.item_type == "movie"),
        )
        counts.browser_visits = await _safe_count(
            db, select(func.count()).select_from(BrowserHistory)
        )
        counts.browser_unique_domains = await _safe_count(
            db, select(func.count(func.distinct(BrowserHistory.domain)))
        )
        counts.news_articles = await _safe_count(db, select(func.count()).select_from(NewsArticle))

    # Tasks completed sur periode (champ completed_at) - pending/overdue restent global
    if cutoff_dt:
        counts.tasks_completed = await _safe_count(
            db,
            select(func.count())
            .select_from(Task)
            .where(Task.is_completed.is_(True))
            .where(Task.completed_at >= cutoff_dt),
        )
    else:
        counts.tasks_completed = await _safe_count(
            db,
            select(func.count()).select_from(Task).where(Task.is_completed.is_(True)),
        )
    counts.tasks_pending = await _safe_count(
        db, select(func.count()).select_from(Task).where(Task.is_completed.is_(False))
    )
    counts.contacts_total = await _safe_count(db, select(func.count()).select_from(Contact))
    counts.drive_files_total = await _safe_count(db, select(func.count()).select_from(DriveFile))
    counts.privacy_requests = await _safe_count(
        db, select(func.count()).select_from(RemovalRequest)
    )

    # ── Finance section ─────────────────────────────────────────────────
    finance = FinanceSection()
    if cutoff_date:
        debit_sum = await _safe_scalar(
            db,
            select(func.sum(Transaction.debit)).where(Transaction.transaction_date >= cutoff_date),
        )
        credit_sum = await _safe_scalar(
            db,
            select(func.sum(Transaction.credit)).where(Transaction.transaction_date >= cutoff_date),
        )
        biggest = await _safe_scalar(
            db,
            select(Transaction)
            .where(Transaction.transaction_date >= cutoff_date)
            .where(Transaction.debit.is_not(None))
            .order_by(desc(Transaction.debit))
            .limit(1),
        )
    else:
        debit_sum = await _safe_scalar(db, select(func.sum(Transaction.debit)))
        credit_sum = await _safe_scalar(db, select(func.sum(Transaction.credit)))
        biggest = await _safe_scalar(
            db,
            select(Transaction)
            .where(Transaction.debit.is_not(None))
            .order_by(desc(Transaction.debit))
            .limit(1),
        )

    finance.total_spend_cad = round(float(debit_sum or 0), 2)
    finance.total_credit_cad = round(float(credit_sum or 0), 2)
    finance.net_cad = round(finance.total_credit_cad - finance.total_spend_cad, 2)
    finance.transactions_count = counts.transactions
    if biggest is not None and biggest.debit:
        finance.biggest_debit_amount = round(float(biggest.debit), 2)
        finance.biggest_debit_desc = (biggest.description or "")[:120]

    # ── Health section ───────────────────────────────────────────────────
    health = HealthSection()
    where_h = []
    if cutoff_date:
        where_h.append(HealthMetric.date >= cutoff_date)

    async def _avg_metric(metric_name: str) -> float | None:
        stmt = select(func.avg(HealthMetric.value)).where(
            HealthMetric.metric == metric_name, *where_h
        )
        v = await _safe_scalar(db, stmt)
        return round(float(v), 2) if v is not None else None

    health.avg_steps = await _avg_metric("steps")
    sleep_avg_s = await _avg_metric("sleep_seconds")
    if sleep_avg_s:
        health.avg_sleep_hours = round(sleep_avg_s / 3600, 2)
    health.avg_resting_hr = await _avg_metric("resting_heart_rate")
    health.avg_stress = await _avg_metric("stress")
    health.avg_hrv = await _avg_metric("hrv")

    active_sum = await _safe_scalar(
        db,
        select(func.sum(HealthMetric.value)).where(
            HealthMetric.metric == "active_minutes", *where_h
        ),
    )
    health.total_active_min = int(active_sum or 0) if active_sum else None

    days_with_health = await _safe_scalar(
        db, select(func.count(func.distinct(HealthMetric.date))).where(*where_h)
    )
    health.days_with_data = int(days_with_health or 0)

    # ── Locations section ───────────────────────────────────────────────
    locations = LocationSection()
    locations.visits = counts.location_visits
    locations.unique_places = counts.location_unique_places

    locations.home_visits = await _safe_count(
        db,
        select(func.count())
        .select_from(LocationVisit)
        .where(LocationVisit.semantic_type == "HOME")
        .where(*([LocationVisit.start_time >= cutoff_dt] if cutoff_dt else [])),
    )
    locations.work_visits = await _safe_count(
        db,
        select(func.count())
        .select_from(LocationVisit)
        .where(LocationVisit.semantic_type == "WORK")
        .where(*([LocationVisit.start_time >= cutoff_dt] if cutoff_dt else [])),
    )

    last_home = await _safe_scalar(
        db,
        select(LocationVisit.start_time)
        .where(LocationVisit.semantic_type == "HOME")
        .order_by(desc(LocationVisit.start_time))
        .limit(1),
    )
    if last_home:
        locations.last_home_iso = _aware(last_home).isoformat()
        locations.days_since_home = (now - _aware(last_home)).days

    # Most visited place — top place_id + resolve to human label via location_addresses
    try:
        top_q = (
            select(
                LocationVisit.place_id,
                LocationVisit.lat,
                LocationVisit.lng,
                func.count().label("c"),
            )
            .where(LocationVisit.place_id.is_not(None))
            .where(*([LocationVisit.start_time >= cutoff_dt] if cutoff_dt else []))
            .group_by(LocationVisit.place_id, LocationVisit.lat, LocationVisit.lng)
            .order_by(desc("c"))
            .limit(1)
        )
        top_row = (await db.execute(top_q)).one_or_none()
        if top_row:
            _place_id, _lat, _lng, _count = top_row
            locations.most_visited_count = int(_count)
            # Resolve via reverse-geocode cache (grid cell lat_e4/lng_e4)
            label: str | None = None
            if _lat is not None and _lng is not None:
                lat_e4 = round(float(_lat) * 10_000)
                lng_e4 = round(float(_lng) * 10_000)
                addr_row = (
                    await db.execute(
                        select(LocationAddress)
                        .where(LocationAddress.lat_e4 == lat_e4, LocationAddress.lng_e4 == lng_e4)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if addr_row:
                    label = addr_row.short_label()
            locations.most_visited_place = label or (_place_id[:30] if _place_id else "?")
    except Exception as e:
        logger.debug("me_locations_top_place_failed err=%r", e)

    # ── Screen time section ──────────────────────────────────────────────
    screen = ScreenTimeSection()
    screen.browser_visits = counts.browser_visits

    try:
        top_dom_q = (
            select(BrowserHistory.domain, func.count().label("c"))
            .where(*([BrowserHistory.visited_at >= cutoff_dt] if cutoff_dt else []))
            .group_by(BrowserHistory.domain)
            .order_by(desc("c"))
            .limit(5)
        )
        screen.browser_top_domains = [
            {"domain": r[0], "count": int(r[1])} for r in (await db.execute(top_dom_q)).all()
        ]
    except Exception as e:
        logger.debug("me_screen_browser_failed err=%r", e)

    # Gaming : delta entre snapshots oldest/newest sur la periode
    try:
        # Pour chaque jeu, premier et dernier snapshot dans la periode
        steam_games = (await db.execute(select(SteamGame))).scalars().all()
        gaming_total = 0
        gaming_per_game: list[tuple[str, int]] = []
        for g in steam_games:
            snaps_stmt = (
                select(SteamPlaySnapshot)
                .where(SteamPlaySnapshot.game_id == g.id)
                .order_by(SteamPlaySnapshot.snapshot_at)
            )
            if cutoff_dt:
                snaps_stmt = snaps_stmt.where(SteamPlaySnapshot.snapshot_at >= cutoff_dt)
            snaps = (await db.execute(snaps_stmt)).scalars().all()
            if len(snaps) >= 2:
                delta = snaps[-1].playtime_forever_min - snaps[0].playtime_forever_min
                if delta > 0:
                    gaming_total += delta
                    gaming_per_game.append((g.name, delta))
        screen.gaming_minutes = gaming_total
        gaming_per_game.sort(key=lambda x: -x[1])
        screen.gaming_top_games = [{"name": n, "minutes": m} for n, m in gaming_per_game[:5]]
        if gaming_total > 0:
            counts.steam_games_played = len(gaming_per_game)
    except Exception as e:
        logger.debug("me_screen_gaming_failed err=%r", e)

    # Streaming runtime
    try:
        streaming_runtime = await _safe_scalar(
            db,
            select(func.sum(StreamingActivity.runtime_minutes)).where(
                *([StreamingActivity.watched_at >= cutoff_dt] if cutoff_dt else [])
            ),
        )
        if streaming_runtime:
            screen.streaming_total_runtime_h = round(float(streaming_runtime) / 60, 1)
    except Exception as e:
        logger.debug("me_screen_streaming_failed err=%r", e)

    # ── Productivity section ─────────────────────────────────────────────
    productivity = ProductivitySection()
    productivity.tasks_completed = counts.tasks_completed
    productivity.tasks_pending = counts.tasks_pending
    productivity.calendar_events = counts.calendar_events
    productivity.tasks_overdue = await _safe_count(
        db,
        select(func.count())
        .select_from(Task)
        .where(Task.is_completed.is_(False))
        .where(Task.due_at.is_not(None))
        .where(Task.due_at < now),
    )

    total_tasks_in_period = productivity.tasks_completed + productivity.tasks_pending
    if total_tasks_in_period > 0:
        productivity.completion_rate_pct = round(
            (productivity.tasks_completed / total_tasks_in_period) * 100, 1
        )

    return DashboardResponse(
        period=period,
        period_days=days,
        generated_at=now,
        counts=counts,
        finance=finance,
        health=health,
        locations=locations,
        screen_time=screen,
        productivity=productivity,
    )
