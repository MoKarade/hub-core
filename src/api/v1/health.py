"""Endpoints de santé du hub.

- GET /health  : check basique (le service répond)
- GET /ready   : check approfondi (DB joignable, Ollama joignable, Cloudflare configuré)
"""

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.db.session import get_db

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — répond toujours si le process est up."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Readiness probe — vérifie que les dépendances marchent.

    checks retournés : database, ollama, cloudflare (si configuré).
    Chaque check inclut status (ok|error|unknown) + latency_ms si applicable.
    """
    checks: dict[str, dict[str, Any]] = {}

    # DB check
    t0 = time.monotonic()
    try:
        result = await db.execute(text("SELECT 1"))
        result.scalar_one()
        checks["database"] = {
            "status": "ok",
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        }
    except Exception as e:
        checks["database"] = {"status": "error", "detail": str(e)}

    # Ollama check
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{settings.ollama_base_url}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            checks["ollama"] = {
                "status": "ok",
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "models_available": models,
                "configured_model": settings.ollama_model,
            }
    except Exception as e:
        checks["ollama"] = {"status": "error", "detail": str(e)}

    # Cloudflare : on regarde si la requete passe par CF (header cf-ray) ou
    # si Access est configure. Pas de vrai sondage — c'est un statut declaratif.
    cf_ray = request.headers.get("cf-ray")
    cf_configured = bool(settings.cf_access_team_domain and settings.cf_access_audience)
    if cf_ray:
        checks["cloudflare"] = {
            "status": "ok",
            "via_tunnel": True,
            "cf_ray": cf_ray,
        }
    elif cf_configured:
        checks["cloudflare"] = {
            "status": "ok",
            "via_tunnel": False,
            "configured": True,
            "detail": "Access configuré (requête locale, pas de cf-ray)",
        }
    else:
        checks["cloudflare"] = {
            "status": "unknown",
            "configured": False,
            "detail": "Cloudflare Access non configuré (CF_ACCESS_TEAM_DOMAIN absent)",
        }

    overall = (
        "ok"
        if all(c["status"] == "ok" for c in checks.values() if c["status"] != "unknown")
        else "degraded"
    )
    return {
        "status": overall,
        "checks": checks,
        "checked_at": datetime.now(UTC).isoformat(),
    }
