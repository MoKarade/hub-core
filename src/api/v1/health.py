"""Endpoints de santé du hub.

- GET /health  : check basique (le service répond)
- GET /ready   : check approfondi (DB joignable, Ollama joignable)
"""

from typing import Any

import httpx
from fastapi import APIRouter, Depends
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
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Readiness probe — vérifie que les dépendances marchent."""
    checks: dict[str, dict[str, Any]] = {}

    # DB check
    try:
        result = await db.execute(text("SELECT 1"))
        result.scalar_one()
        checks["database"] = {"status": "ok"}
    except Exception as e:
        checks["database"] = {"status": "error", "detail": str(e)}

    # Ollama check
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{settings.ollama_base_url}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            checks["ollama"] = {
                "status": "ok",
                "models_available": models,
                "configured_model": settings.ollama_model,
            }
    except Exception as e:
        checks["ollama"] = {"status": "error", "detail": str(e)}

    overall = "ok" if all(c["status"] == "ok" for c in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}
