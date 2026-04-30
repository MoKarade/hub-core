"""SSE (Server-Sent Events) — flux temps reel pour le dashboard frontend.

GET /v1/events/stream : connexion persistante, un event par message.

Architecture :
- Un asyncio.Queue par client connecte (pas de Redis necessaire pour MVP locale).
- broadcast() appele par les endpoints d ingestion (finance, locations) apres insert.
- Heartbeat toutes les 30s pour maintenir la connexion (proxies Caddy / Cloudflare).
- Reconnexion automatique cote client (le hook useEventSource gere le backoff).

Events emis :
  connected          {}                          -- a la connexion
  new_transaction    {account_id, description, amount, currency}
  new_location       {timestamp_utc, activity_type}
  heartbeat          (commentaire SSE, pas d event nomme)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

# ---------------------------------------------------------------------------
# Broadcaster global
# ---------------------------------------------------------------------------

# Ensemble des queues, une par client SSE connecte.
# Les operations add/discard sont thread-safe dans l asyncio event loop.
_clients: set[asyncio.Queue[dict]] = set()


async def broadcast(event_type: str, data: dict) -> None:
    """Envoie un event a tous les clients SSE connectes.

    Silencieux si aucun client. Supprime les clients a la queue pleine
    (lents ou deconnectes de facon non propre).
    """
    if not _clients:
        return

    payload = {"type": event_type, "data": data}
    dead: set[asyncio.Queue[dict]] = set()
    for q in _clients:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)

    if dead:
        _clients.difference_update(dead)
        logger.warning("sse_slow_clients_removed", count=len(dead))

    logger.debug("sse_broadcast", event_type=event_type, active_clients=len(_clients))


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/stream",
    summary="Flux SSE temps reel (transactions, localisations…)",
)
async def sse_stream(request: Request) -> StreamingResponse:
    """Connexion SSE persistante.

    - Envoie un event `connected` a l ouverture.
    - Heartbeat (commentaire) toutes les 30s — evite les timeouts proxy.
    - Se ferme proprement quand le client deconnecte.
    """

    async def event_generator() -> AsyncGenerator[str, None]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=64)
        _clients.add(q)
        logger.info("sse_client_connected", total_clients=len(_clients))
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30.0)
                    event_type = payload["type"]
                    data_str = json.dumps(payload["data"], default=str)
                    yield f"event: {event_type}\ndata: {data_str}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            _clients.discard(q)
            logger.info("sse_client_disconnected", total_clients=len(_clients))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # desactive le buffering nginx / Caddy
        },
    )
