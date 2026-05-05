"""Endpoint /v1/news - actualites via RSS Google News (Phase 6).

Pourquoi RSS et pas API officielle ?
  - Google News API officielle n'existe pas (deprecated en 2011).
  - Alternatives payantes : NewsAPI.org (100 req/jour gratuits puis $$),
    GNews (100 req/jour), Bing News (Azure $$). Aucune ne couvre bien le
    QC/FR.
  - Solution gratuite et illimitee : flux RSS public Google News
    (https://news.google.com/rss?hl=fr-CA&gl=CA&ceid=CA%3Afr).
  - Pas d'auth, pas de quota, pas de limite, multilingue.

Endpoints :
  POST /v1/news/sync  -> pull le RSS, upsert en DB
  GET  /v1/news       -> liste paginee, filtres source/category/since
  GET  /v1/news/stats -> stats globales (total, by_source, by_day)

Le scheduler appelle sync_news_rss toutes les 30 min en background
(cf. src/scheduler.py :: _job_news).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any

import feedparser
import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.db.models.news_article import NewsArticle
from src.db.session import get_db

logger = structlog.get_logger()

router = APIRouter(prefix="/news", tags=["news"])


# ─── Schemas ──────────────────────────────────────────────────────────────────


class NewsItem(BaseModel):
    id: str
    guid: str
    title: str
    link: str
    summary: str | None
    source: str | None
    category: str | None
    image_url: str | None
    published_at: datetime
    feed_url: str


class NewsStats(BaseModel):
    total: int
    by_source: list[dict[str, Any]]
    by_day: list[dict[str, Any]]
    last_sync: datetime | None


# ─── Sync via RSS ─────────────────────────────────────────────────────────────


def _strip_html(s: str | None) -> str | None:
    """Strip rough HTML tags pour avoir un summary text-only readable."""
    if not s:
        return None
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:500] if s else None


def _detect_source(entry: dict[str, Any]) -> str | None:
    """Google News RSS met la source dans <source>NomMedia</source> ou en
    suffixe du title : 'Mon titre - Le Devoir'. On essaie les deux."""
    src = entry.get("source", {})
    if isinstance(src, dict) and src.get("title"):
        return src["title"][:100]
    title = entry.get("title", "") or ""
    m = re.search(r" - ([^-]+)$", title)
    if m:
        return m.group(1).strip()[:100]
    return None


def _parse_published(entry: dict[str, Any]) -> datetime | None:
    """Convertit feedparser's published_parsed (struct_time) -> datetime UTC."""
    pp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not pp:
        return None
    try:
        return datetime(*pp[:6], tzinfo=UTC)
    except (ValueError, TypeError):
        return None


async def sync_news_rss(db: AsyncSession) -> dict[str, Any]:
    """Pull le RSS configure (settings.news_rss_url), upsert articles.

    Idempotent : guid unique => update si existe deja.
    """
    settings = get_settings()
    feed_url = settings.news_rss_url
    log = logger.bind(feed_url=feed_url)

    # feedparser est sync, mais leger et rapide (~200ms pour 100 articles).
    # On accepte le block courte duree dans l'event loop.
    feed = feedparser.parse(feed_url)
    if feed.bozo and feed.bozo_exception:
        log.warning("news_rss_parse_warning", error=str(feed.bozo_exception)[:200])

    ingested = 0
    updated = 0
    errors = 0
    for entry in feed.entries:
        try:
            guid = entry.get("id") or entry.get("guid") or entry.get("link")
            if not guid:
                continue
            published = _parse_published(entry) or datetime.now(UTC)
            title = (entry.get("title") or "(sans titre)")[:1000]
            link = entry.get("link") or ""
            summary = _strip_html(entry.get("summary") or entry.get("description"))
            source_name = _detect_source(entry)
            # category : feedparser parse <category> mais Google News n'en donne pas
            cat_list = entry.get("tags", [])
            category = cat_list[0]["term"] if cat_list else None
            if category:
                category = category[:50]

            existing = (
                await db.execute(select(NewsArticle).where(NewsArticle.guid == guid))
            ).scalar_one_or_none()
            if existing:
                # Update champs susceptibles de changer (title corrige, summary)
                changed = False
                if existing.title != title:
                    existing.title = title
                    changed = True
                if existing.summary != summary:
                    existing.summary = summary
                    changed = True
                if changed:
                    updated += 1
            else:
                db.add(
                    NewsArticle(
                        guid=guid,
                        title=title,
                        link=link,
                        summary=summary,
                        source=source_name,
                        category=category,
                        image_url=None,
                        published_at=published,
                        feed_url=feed_url,
                    )
                )
                ingested += 1
        except Exception as e:
            errors += 1
            log.warning("news_entry_failed", error=str(e)[:200])

    await db.commit()
    log.info("news_sync_done", ingested=ingested, updated=updated, errors=errors)
    return {
        "ingested": ingested,
        "updated": updated,
        "errors": errors,
        "feed_url": feed_url,
        "total_entries": len(feed.entries),
    }


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/sync")
async def post_sync(db: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, Any]:
    """Force un sync immediat du RSS Google News."""
    return await sync_news_rss(db)


@router.get("", response_model=list[NewsItem])
async def list_news(
    db: Annotated[AsyncSession, Depends(get_db)],
    source: str | None = Query(None, description="Filtre par source (ex: 'Le Devoir')"),
    category: str | None = Query(None, description="Filtre par categorie"),
    since: datetime | None = Query(None, description="Articles publies apres cette date"),
    q: str | None = Query(None, description="Recherche texte dans le titre"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[NewsItem]:
    """Liste des articles, paginee, tries par date publication desc."""
    stmt = select(NewsArticle)
    if source:
        stmt = stmt.where(NewsArticle.source.ilike(f"%{source}%"))
    if category:
        stmt = stmt.where(NewsArticle.category == category)
    if since:
        stmt = stmt.where(NewsArticle.published_at >= since)
    if q:
        stmt = stmt.where(NewsArticle.title.ilike(f"%{q}%"))
    stmt = stmt.order_by(desc(NewsArticle.published_at)).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        NewsItem(
            id=str(r.id),
            guid=r.guid,
            title=r.title,
            link=r.link,
            summary=r.summary,
            source=r.source,
            category=r.category,
            image_url=r.image_url,
            published_at=r.published_at,
            feed_url=r.feed_url,
        )
        for r in rows
    ]


@router.get("/stats", response_model=NewsStats)
async def news_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> NewsStats:
    """Stats globales : total, top sources, derniers jours, derniere sync."""
    total = (await db.execute(select(func.count()).select_from(NewsArticle))).scalar_one() or 0

    by_source_rows = (
        await db.execute(
            select(NewsArticle.source, func.count().label("count"))
            .where(NewsArticle.source.isnot(None))
            .group_by(NewsArticle.source)
            .order_by(desc("count"))
            .limit(15)
        )
    ).all()
    by_source = [{"source": r[0], "count": r[1]} for r in by_source_rows]

    by_day_rows = (
        await db.execute(
            select(
                func.date(NewsArticle.published_at).label("day"),
                func.count().label("count"),
            )
            .group_by("day")
            .order_by(desc("day"))
            .limit(14)
        )
    ).all()
    by_day = [{"day": str(r[0]), "count": r[1]} for r in by_day_rows]

    last_sync = (
        await db.execute(select(func.max(NewsArticle.created_at)))
    ).scalar_one_or_none()

    return NewsStats(
        total=total,
        by_source=by_source,
        by_day=by_day,
        last_sync=last_sync,
    )
