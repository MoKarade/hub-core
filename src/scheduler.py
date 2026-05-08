"""Scheduler auto-sync des sources de donnees (Phase 6).

Sync periodique sans intervention de Marc :
  - Gmail        : toutes les 15 min  (rapide, mais limites API : 250 quota/sec)
  - Calendar     : toutes les 30 min  (peu de changement)
  - Tasks        : toutes les 30 min
  - Photos       : OFF par defaut    (Picker API requiert interaction Marc)
  - Drive        : toutes les 6h     (gros volume, change peu)
  - Contacts     : toutes les 12h    (change rarement)
  - Health       : toutes les heures (Garmin pousse les data au fil de l'eau)
  - News         : toutes les 30 min (RSS Google News, gratuit)

Architecture :
  - APScheduler AsyncIOScheduler integre dans le lifespan FastAPI
  - Chaque job re-utilise la meme logique que les endpoints /sync existants
    (DRY : on appelle directement les fonctions metier)
  - Errors loggees mais ne crashent pas le scheduler
  - Settings : SCHEDULER_ENABLED=true|false (default true en prod, false en dev
    pour eviter de spammer les API pendant le dev)
  - Intervals personnalisables via env vars : SCHEDULER_GMAIL_MIN, etc.

Marc ne touche a rien : tout se fait en background. La doc des intervals est
dans 06_user_guide.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.db.session import SessionLocal as async_session_maker  # noqa: N813

_OWNER_EMAIL: str = get_settings().hub_owner_email

logger = structlog.get_logger()


# ─── Wrapper helper ──────────────────────────────────────────────────────────


async def _run_with_session(
    job_name: str,
    coro_factory: Callable[[AsyncSession], Awaitable[Any]],
) -> None:
    """Execute une coroutine avec une session DB fraiche, log + catch errors.

    Chaque job a sa propre session pour eviter les locks long-lived. On log
    debut + fin (success ou exception) pour observability via structlog.
    """
    started = datetime.now(UTC)
    log = logger.bind(job=job_name, scheduled_at=started.isoformat())
    try:
        async with async_session_maker() as db:
            result = await coro_factory(db)
            elapsed = (datetime.now(UTC) - started).total_seconds()
            log.info("scheduler_job_ok", elapsed_s=round(elapsed, 1), result=str(result)[:200])
    except Exception as e:
        elapsed = (datetime.now(UTC) - started).total_seconds()
        log.error(
            "scheduler_job_failed",
            elapsed_s=round(elapsed, 1),
            error_type=type(e).__name__,
            error=str(e)[:300],
        )


# ─── Job factories : reutilisent la logique des endpoints /sync ───────────────


async def _job_emails(db: AsyncSession) -> str:
    """Sync Gmail des 1 derniers jours (delta : on rejoue, idempotent par gmail_id)."""
    from src.api.v1.emails import SyncRequest, sync_emails

    res = await sync_emails(
        SyncRequest(user_email=_OWNER_EMAIL, since_days=1, max_results=200),
        db=db,
    )
    return f"ingested={res.ingested} updated={res.updated}"


async def _job_calendar(db: AsyncSession) -> str:
    from src.api.v1.calendar import CalSyncRequest, sync_calendar

    res = await sync_calendar(
        CalSyncRequest(user_email=_OWNER_EMAIL, days_back=7, days_forward=180),
        db=db,
    )
    return f"ingested={res.events_ingested} updated={res.events_updated}"


async def _job_tasks(db: AsyncSession) -> str:
    from src.api.v1.tasks import TasksSyncRequest, sync_tasks

    res = await sync_tasks(
        TasksSyncRequest(user_email=_OWNER_EMAIL),
        db=db,
    )
    return f"ingested={res.tasks_ingested} updated={res.tasks_updated}"


async def _job_drive(db: AsyncSession) -> str:
    from src.api.v1.drive import DriveSyncRequest, sync_drive

    res = await sync_drive(
        DriveSyncRequest(user_email=_OWNER_EMAIL, max_results=500),
        db=db,
    )
    return f"ingested={res.ingested} updated={res.updated}"


async def _job_contacts(db: AsyncSession) -> str:
    from src.api.v1.contacts import ContactsSyncRequest, sync_contacts

    res = await sync_contacts(
        ContactsSyncRequest(user_email=_OWNER_EMAIL),
        db=db,
    )
    return f"ingested={res.ingested} updated={res.updated}"


async def _job_health(db: AsyncSession) -> str:
    """Sync Google Fit des 7 derniers jours (rejoue, idempotent)."""
    from src.api.v1.health_data import HealthSyncRequest, sync_health

    res = await sync_health(
        HealthSyncRequest(user_email=_OWNER_EMAIL, days_back=7),
        db=db,
    )
    return f"ingested={res.metrics_ingested} updated={res.metrics_updated}"


async def _job_news(db: AsyncSession) -> str:
    """Sync RSS Google News (sans auth, gratuit, ~25 articles)."""
    from src.api.v1.news import sync_news_rss

    res = await sync_news_rss(db=db)
    return f"ingested={res['ingested']} updated={res['updated']}"


async def _job_clip_embed(db: AsyncSession) -> str:
    """Embed N photos sans embedding via CLIP. Skip si torch pas installe.

    Cron par defaut chaque 30 min — traite par batch de 100. Pour 5000 photos
    a embed, ca prend ~25h auto au demarrage initial puis idle.
    """
    try:
        import open_clip  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return "skipped (CLIP not installed: pip install -e .[ml])"

    from src.api.v1.photos_ml import EmbedRequest, embed_photos

    res = await embed_photos(EmbedRequest(limit=100), db=db)
    return (
        f"embedded={res.embedded} skipped={res.skipped_no_url} "
        f"errors={res.errors} remaining={res.total_remaining}"
    )


async def _job_face_detect(db: AsyncSession) -> str:
    """Detect+encode visages dans N photos non traitees. Skip si dlib pas la."""
    try:
        import face_recognition  # noqa: F401
    except ImportError:
        return "skipped (face_recognition not installed: pip install -e .[ml])"

    from src.api.v1.photos_ml import FaceDetectRequest, detect_faces

    res = await detect_faces(FaceDetectRequest(limit=50, detection_model="hog"), db=db)
    return f"photos={res.photos_processed} faces={res.faces_found} errors={res.errors}"


async def _job_steam(db: AsyncSession) -> str:
    """Sync Steam library + snapshot playtime. Skip silencieux si pas configure."""
    from src.api.v1.steam import sync_steam
    from src.core.config import get_settings

    settings = get_settings()
    if not settings.steam_api_key or not settings.steam_user_id:
        return "skipped (STEAM_API_KEY / STEAM_USER_ID not configured)"

    res = await sync_steam(db=db, settings=settings)
    return (
        f"games={res.games_in_library} played_2w={res.games_played_2w} "
        f"snapshots={res.snapshots_created}"
    )


async def _job_streaming(db: AsyncSession) -> str:
    """Sync Trakt.tv history des 7 derniers jours.

    Skip silencieux si pas de tokens (Marc pas connecte).
    """
    from src.api.v1.streaming import SyncRequest, _load_trakt_token, sync_streaming
    from src.core.config import get_settings

    try:
        await _load_trakt_token(db, _OWNER_EMAIL)
    except Exception:
        return "skipped (no tokens, run /v1/streaming/connect first)"

    settings = get_settings()
    res = await sync_streaming(
        SyncRequest(user_email=_OWNER_EMAIL, days_back=7),
        db=db,
        settings=settings,
    )
    return f"ingested={res.ingested} updated={res.updated}"


async def _job_garmin(db: AsyncSession) -> str:
    """Sync Garmin Connect des 7 derniers jours (utilise tokens chiffres en DB).

    Skip silencieux si pas de tokens (Marc pas connecte). Pas de crash.
    """
    from src.api.v1.garmin import GarminSyncRequest, _load_token_row, garmin_sync

    # Skip silencieux si pas de tokens en DB (Marc n'a jamais fait /connect)
    row = await _load_token_row(db, _OWNER_EMAIL)
    if row is None or row.revoked_at is not None:
        return "skipped (no tokens, run /v1/garmin/connect first)"

    res = await garmin_sync(
        GarminSyncRequest(user_email=_OWNER_EMAIL, days_back=7),
        db=db,
    )
    return (
        f"ingested={res.metrics_ingested} updated={res.metrics_updated} days={res.days_processed}"
    )


# ─── Scheduler global instance ────────────────────────────────────────────────

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    """Retourne l'instance courante du scheduler (None si non demarre)."""
    return _scheduler


def list_jobs_status() -> list[dict[str, Any]]:
    """Liste les jobs avec leur prochain run (pour /v1/scheduler/status)."""
    if not _scheduler:
        return []
    out = []
    for job in _scheduler.get_jobs():
        out.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
        )
    return out


async def start_scheduler(settings: Settings) -> None:
    """Demarre le scheduler avec les jobs configures.

    Appelle dans le lifespan FastAPI au startup. Utilise async_session_maker
    pour les sessions DB (vs Depends).
    """
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info("scheduler_disabled", reason="settings.scheduler_enabled=false")
        return
    if _scheduler is not None:
        logger.warning("scheduler_already_started")
        return

    _scheduler = AsyncIOScheduler(timezone="UTC")

    # Wrapper pour un job : factory de coroutine + nom + interval
    def _add_job(
        job_id: str,
        factory: Callable[[AsyncSession], Awaitable[Any]],
        minutes: int,
    ) -> None:
        if minutes <= 0:
            logger.info("scheduler_job_disabled", job=job_id, reason="interval=0")
            return
        _scheduler.add_job(
            _run_with_session,
            args=[job_id, factory],
            id=job_id,
            name=job_id,
            trigger=IntervalTrigger(minutes=minutes),
            replace_existing=True,
            # Decale le 1er run de 30s pour laisser le serveur finir son startup
            next_run_time=datetime.now(UTC).replace(microsecond=0)
            + _delay_secs(30 + (hash(job_id) % 60)),
            max_instances=1,  # Pas de chevauchement si un job traine
            coalesce=True,  # Si on rate des runs, on en fait UN seul
            misfire_grace_time=300,
        )

    _add_job("emails", _job_emails, settings.scheduler_emails_minutes)
    _add_job("calendar", _job_calendar, settings.scheduler_calendar_minutes)
    _add_job("tasks", _job_tasks, settings.scheduler_tasks_minutes)
    _add_job("drive", _job_drive, settings.scheduler_drive_minutes)
    _add_job("contacts", _job_contacts, settings.scheduler_contacts_minutes)
    _add_job("health", _job_health, settings.scheduler_health_minutes)
    _add_job("news", _job_news, settings.scheduler_news_minutes)
    _add_job("garmin", _job_garmin, settings.scheduler_garmin_minutes)
    _add_job("streaming", _job_streaming, settings.scheduler_streaming_minutes)
    _add_job("steam", _job_steam, settings.scheduler_steam_minutes)
    _add_job("clip_embed", _job_clip_embed, settings.scheduler_clip_embed_minutes)
    _add_job("face_detect", _job_face_detect, settings.scheduler_face_detect_minutes)

    _scheduler.start()
    jobs = list_jobs_status()
    logger.info("scheduler_started", jobs_count=len(jobs), jobs=[j["id"] for j in jobs])


async def stop_scheduler() -> None:
    """Arret propre dans le shutdown FastAPI."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")
    except Exception as e:
        logger.error("scheduler_stop_failed", error=str(e))
    _scheduler = None


def _delay_secs(seconds: int):
    """Helper : retourne un timedelta. Importe localement pour eviter cycle import."""
    from datetime import timedelta

    return timedelta(seconds=seconds)


# ─── Manual trigger (admin endpoint /v1/scheduler/run/{job}) ──────────────────


async def run_job_now(job_id: str) -> dict[str, Any]:
    """Lance un job manuellement (utile pour tester / forcer une sync)."""
    factories = {
        "emails": _job_emails,
        "calendar": _job_calendar,
        "tasks": _job_tasks,
        "drive": _job_drive,
        "contacts": _job_contacts,
        "health": _job_health,
        "news": _job_news,
        "garmin": _job_garmin,
        "streaming": _job_streaming,
        "steam": _job_steam,
        "clip_embed": _job_clip_embed,
        "face_detect": _job_face_detect,
    }
    if job_id not in factories:
        raise ValueError(f"Unknown job_id: {job_id}. Valid: {list(factories)}")
    started = datetime.now(UTC)
    asyncio.create_task(_run_with_session(job_id, factories[job_id]))
    return {"status": "started", "job_id": job_id, "started_at": started.isoformat()}
