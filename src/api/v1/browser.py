"""Endpoints /v1/browser/* — historique navigateur (Chrome/Firefox/etc.).

Ingestion :
- Le connecteur hub-ingest copie le SQLite Chrome (locked si Chrome tourne -> shutil.copy2),
  parse, et POST /v1/browser/sync avec un batch d'items.
- Idempotent par dedup_hash = sha256(url + visited_at iso).

Recherche :
- GET /v1/browser/history?domain=&since_days=&q=&limit=&offset=
- GET /v1/browser/stats : top domains + by_hour + by_day_of_week
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import BrowserHistory
from src.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/browser", tags=["browser"])


# ─────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────


class BrowserItem(BaseModel):
    source: str = Field(default="chrome", max_length=20)
    external_id: str | None = Field(None, max_length=64)
    url: str = Field(..., min_length=1)
    title: str | None = Field(None, max_length=500)
    visited_at: datetime
    visit_duration_s: int | None = None
    transition: str | None = Field(None, max_length=30)


class SyncRequest(BaseModel):
    items: list[BrowserItem]


class SyncResponse(BaseModel):
    ingested: int
    skipped_dedup: int
    errors: int


class HistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    url: str
    domain: str
    title: str | None
    visited_at: datetime
    visit_duration_s: int | None
    transition: str | None


class StatsResponse(BaseModel):
    total_visits: int
    unique_domains: int
    top_domains: list[dict[str, Any]]
    by_hour: list[dict[str, Any]]  # 0..23 -> count
    by_day_of_week: list[dict[str, Any]]  # 0=lundi..6=dimanche


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _extract_domain(url: str) -> str:
    """Extrait le domaine depuis une URL. Fallback sur la string raw si parse fail."""
    try:
        return urlparse(url).netloc.lower() or url[:100]
    except Exception:
        return url[:100]


def _dedup_hash(url: str, visited_at: datetime) -> str:
    key = f"{url}|{visited_at.isoformat()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────


@router.post("/sync", response_model=SyncResponse)
async def sync_browser(
    payload: SyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SyncResponse:
    """Upsert un batch de visites (idempotent par dedup_hash)."""
    if not payload.items:
        return SyncResponse(ingested=0, skipped_dedup=0, errors=0)

    # Pre-calcule les hashes
    candidates: list[tuple[BrowserItem, str]] = []
    for item in payload.items:
        try:
            h = _dedup_hash(item.url, item.visited_at)
            candidates.append((item, h))
        except Exception as e:
            logger.warning("browser_hash_failed url=%s err=%r", item.url[:100], e)

    if not candidates:
        return SyncResponse(ingested=0, skipped_dedup=0, errors=len(payload.items))

    # Batch check : quels hashes existent deja ?
    hashes = [h for _, h in candidates]
    existing_rows = (
        (
            await db.execute(
                select(BrowserHistory.dedup_hash).where(BrowserHistory.dedup_hash.in_(hashes))
            )
        )
        .scalars()
        .all()
    )
    existing_set = set(existing_rows)

    ingested = 0
    skipped = 0
    errors = 0
    # Dedup intra-batch : Chrome history peut avoir N visites strictement identiques
    # (meme url + meme visited_at) si l'utilisateur recharge rapidement. Le check
    # `existing_set` ne couvre que ce qui est deja committe en DB ; sans dedup
    # intra-batch on declenche une UniqueViolation au commit.
    seen_in_batch: set[str] = set()
    for item, h in candidates:
        if h in existing_set or h in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(h)
        try:
            db.add(
                BrowserHistory(
                    source=item.source,
                    external_id=item.external_id,
                    url=item.url[:8000],  # cap pour eviter URLs absurdes
                    domain=_extract_domain(item.url),
                    title=item.title[:500] if item.title else None,
                    visited_at=item.visited_at,
                    visit_duration_s=item.visit_duration_s,
                    transition=item.transition,
                    dedup_hash=h,
                )
            )
            ingested += 1
        except Exception as e:
            logger.warning("browser_insert_failed url=%s err=%r", item.url[:100], e)
            errors += 1

    await db.commit()
    return SyncResponse(ingested=ingested, skipped_dedup=skipped, errors=errors)


@router.get("/history", response_model=list[HistoryItem])
async def list_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    domain: str | None = None,
    q: str | None = None,
    since_days: int | None = None,
    source: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[HistoryItem]:
    """Liste les visites, plus recente d'abord. Filtres optionnels."""
    stmt = select(BrowserHistory).order_by(desc(BrowserHistory.visited_at))
    if domain:
        stmt = stmt.where(BrowserHistory.domain == domain.lower())
    if source:
        stmt = stmt.where(BrowserHistory.source == source.lower())
    if since_days:
        cutoff = datetime.now(UTC) - timedelta(days=since_days)
        stmt = stmt.where(BrowserHistory.visited_at >= cutoff)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                BrowserHistory.url.ilike(like),
                BrowserHistory.title.ilike(like),
            )
        )
    stmt = stmt.limit(min(limit, 1000)).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [HistoryItem.model_validate(r) for r in rows]


@router.get("/stats", response_model=StatsResponse)
async def browser_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    since_days: int | None = 90,
) -> StatsResponse:
    """Stats globales sur les N derniers jours (default 90j)."""
    where = []
    if since_days:
        cutoff = datetime.now(UTC) - timedelta(days=since_days)
        where.append(BrowserHistory.visited_at >= cutoff)

    total = (
        await db.execute(select(func.count()).select_from(BrowserHistory).where(*where))
    ).scalar_one() or 0

    unique_domains = (
        await db.execute(select(func.count(func.distinct(BrowserHistory.domain))).where(*where))
    ).scalar_one() or 0

    # Top domains
    top_q = (
        select(BrowserHistory.domain, func.count().label("c"))
        .where(*where)
        .group_by(BrowserHistory.domain)
        .order_by(desc("c"))
        .limit(20)
    )
    top_domains = [{"domain": r[0], "count": r[1]} for r in (await db.execute(top_q)).all()]

    # By hour (0..23) + dow : strftime sur SQLite, extract sur Postgres
    by_hour: list[dict[str, Any]] = []
    by_dow: list[dict[str, Any]] = []
    is_postgres = db.get_bind().dialect.name == "postgresql"
    try:
        if is_postgres:
            hour_expr = func.extract("hour", BrowserHistory.visited_at)
            dow_expr = func.extract("dow", BrowserHistory.visited_at)
        else:
            hour_expr = func.strftime("%H", BrowserHistory.visited_at)
            dow_expr = func.strftime("%w", BrowserHistory.visited_at)

        hour_q = (
            select(hour_expr.label("h"), func.count().label("c"))
            .where(*where)
            .group_by("h")
            .order_by("h")
        )
        by_hour = [{"hour": int(r[0]), "count": r[1]} for r in (await db.execute(hour_q)).all()]

        dow_q = (
            select(dow_expr.label("d"), func.count().label("c"))
            .where(*where)
            .group_by("d")
            .order_by("d")
        )
        # SQLite et Postgres : 0=dimanche..6=samedi -> remap pour FR (0=lundi..6=dimanche)
        raw = [(int(r[0]), r[1]) for r in (await db.execute(dow_q)).all()]
        remap = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
        by_dow = [{"day": remap.get(d, d), "count": c} for d, c in raw]
        by_dow.sort(key=lambda x: x["day"])
    except Exception as e:
        logger.warning("browser_stats_time_failed err=%r dialect=%s", e, db.get_bind().dialect.name)

    return StatsResponse(
        total_visits=int(total),
        unique_domains=int(unique_domains),
        top_domains=top_domains,
        by_hour=by_hour,
        by_day_of_week=by_dow,
    )


@router.delete("/wipe")
async def wipe_browser(
    db: Annotated[AsyncSession, Depends(get_db)],
    confirm: bool = False,
) -> dict[str, int]:
    """Supprime TOUTE l'historique. Necessite ?confirm=true."""
    if not confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Ajoute ?confirm=true pour confirmer la suppression complete.",
        )
    res = await db.execute(BrowserHistory.__table__.delete())
    await db.commit()
    return {"deleted": res.rowcount or 0}
