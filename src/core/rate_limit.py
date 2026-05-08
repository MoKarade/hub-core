"""Rate limiting minimaliste — sliding window en mémoire (stdlib uniquement).

Single-user single-process : pas besoin de Redis.
Usage :

    @router.post("/ask", dependencies=[Depends(rate_limit(10, 60))])
    async def ask(...): ...
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

_buckets: dict[str, list[float]] = defaultdict(list)


def rate_limit(max_requests: int, window_seconds: float):
    """Dependency FastAPI : lève HTTP 429 si le endpoint est appelé trop souvent.

    Args:
        max_requests: Nombre max d'appels autorisés dans la fenêtre.
        window_seconds: Durée de la fenêtre glissante en secondes.
    """

    async def _check(request: Request) -> None:
        key = request.url.path
        now = time.monotonic()
        times = _buckets[key]
        _buckets[key] = [t for t in times if now - t < window_seconds]
        if len(_buckets[key]) >= max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Trop de requêtes. Réessaie dans quelques instants.",
            )
        _buckets[key].append(now)

    return _check
