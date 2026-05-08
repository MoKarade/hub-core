"""Rate limiting minimaliste — sliding window en mémoire (stdlib uniquement).

Single-user single-process : pas besoin de Redis.

Bucket par (path, identifiant_client). L'identifiant est, dans l'ordre :
  1. l'email Cloudflare Access (si CF Access est en place : 1 user authentifie)
  2. le X-Forwarded-For premier hop (derriere CF Tunnel)
  3. le client.host direct (dev local)

Sans cet IP/email, un attaquant non authentifie partagerait le bucket de Marc
et pourrait epuiser son quota a distance.

Usage :

    @router.post("/ask", dependencies=[Depends(rate_limit(10, 60))])
    async def ask(...): ...
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

_buckets: dict[str, list[float]] = defaultdict(list)


def _client_id(request: Request) -> str:
    """Identifiant client pour le bucket : email CF si dispo, sinon IP."""
    user = getattr(request.state, "cf_user_email", None)
    if user:
        return f"u:{user}"
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return f"ip:{fwd.split(',')[0].strip()}"
    if request.client and request.client.host:
        return f"ip:{request.client.host}"
    return "ip:unknown"


def rate_limit(max_requests: int, window_seconds: float):
    """Dependency FastAPI : lève HTTP 429 si le endpoint est appelé trop souvent.

    Args:
        max_requests: Nombre max d'appels autorisés dans la fenêtre.
        window_seconds: Durée de la fenêtre glissante en secondes.
    """

    async def _check(request: Request) -> None:
        key = f"{request.url.path}:{_client_id(request)}"
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
