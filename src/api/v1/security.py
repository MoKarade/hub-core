"""Endpoint /v1/security - proxy HIBP (Have I Been Pwned).

Pourquoi un proxy ? Le frontend ne peut pas appeler HIBP en direct :
- /range/{prefix} est CORS-friendly mais on veut centraliser le timeout/erreurs.
- /api/v3/breaches refuse les origins browser (CORS) et exige un User-Agent custom.

Tous les endpoints renvoient 502 si HIBP est down, timeout 10s.
"""

from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, HTTPException, Query, status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/security", tags=["security"])

HIBP_PASSWORDS_API = "https://api.pwnedpasswords.com/range"
HIBP_BREACHES_API = "https://haveibeenpwned.com/api/v3/breaches"
HIBP_TIMEOUT = 10.0
HIBP_USER_AGENT = "PersonalDataHub"

_HEX5_RE = re.compile(r"^[0-9a-fA-F]{5}$")


@router.get("/hibp/passwords/{prefix}")
async def hibp_passwords(prefix: str) -> dict[str, str]:
    """Proxy k-anonymity HIBP : prefix = 5 chars hex SHA-1.

    HIBP retourne un text/plain avec les suffixes du SHA-1 + count, qu'on
    relaie tel quel au frontend (qui filtre cote client - jamais le full
    hash n'est envoye a HIBP).
    """
    if not _HEX5_RE.match(prefix):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "prefix doit etre 5 chars hex (SHA-1)",
        )
    try:
        async with httpx.AsyncClient(timeout=HIBP_TIMEOUT) as client:
            r = await client.get(f"{HIBP_PASSWORDS_API}/{prefix}")
            r.raise_for_status()
            return {"ranges": r.text}
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.warning("hibp_passwords_failed: %r", e)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"HIBP passwords indisponible : {type(e).__name__}",
        ) from e


@router.get("/hibp/breaches")
async def hibp_breaches(domain: str | None = Query(default=None)) -> list[dict]:
    """Liste les breaches HIBP, eventuellement filtrees par domaine.

    Doc : https://haveibeenpwned.com/API/v3#AllBreaches
    """
    params = {"domain": domain} if domain else None
    try:
        async with httpx.AsyncClient(timeout=HIBP_TIMEOUT) as client:
            r = await client.get(
                HIBP_BREACHES_API,
                params=params,
                headers={"User-Agent": HIBP_USER_AGENT},
            )
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.warning("hibp_breaches_failed: %r", e)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"HIBP breaches indisponible : {type(e).__name__}",
        ) from e
