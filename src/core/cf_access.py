"""Middleware FastAPI pour valider les JWT Cloudflare Access.

Phase 0 fin — defense-in-depth pour quand le hub est exposé via Cloudflare Tunnel.

Comment ça marche :
  1. User va sur https://hub.tondomaine.com (passé par CF Edge)
  2. Cloudflare Access intercepte → demande login Google + MFA
  3. Si autorisé : CF ajoute un header Cf-Access-Jwt-Assertion
  4. Ce middleware valide le JWT (signature + audience + expiration)
  5. Si invalide → 401 (defense contre bypass de Cloudflare Edge)

Activation : si cf_access_team_domain et cf_access_audience sont vides dans
la config (dev local), le middleware passe transparent. En prod, ils sont
remplis et la validation est stricte.

Doc Cloudflare : https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

import httpx
import jwt as pyjwt
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.core.config import get_settings
from src.core.logging import logger


@lru_cache(maxsize=1)
def _get_jwks_url(team_domain: str) -> str:
    """URL des clés publiques JWT pour ton tenant Cloudflare Access."""
    return f"https://{team_domain}/cdn-cgi/access/certs"


# Cache des JWKS (clés publiques) pour ne pas refetch à chaque requête.
# Le cache expire après 1h (Cloudflare rotate les keys régulièrement).
_jwks_cache: dict[str, Any] = {"keys": [], "fetched_at": 0.0}
_JWKS_CACHE_TTL = 3600.0


async def _fetch_jwks(team_domain: str) -> list[dict[str, Any]]:
    """Récupère les JWKS Cloudflare (cached 1h)."""
    now = time.time()
    if _jwks_cache["keys"] and (now - _jwks_cache["fetched_at"]) < _JWKS_CACHE_TTL:
        return _jwks_cache["keys"]

    url = _get_jwks_url(team_domain)
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    _jwks_cache["keys"] = data.get("keys", [])
    _jwks_cache["fetched_at"] = now
    return _jwks_cache["keys"]


async def validate_cf_access_jwt(token: str, team_domain: str, audience: str) -> dict[str, Any]:
    """Valide un JWT Cloudflare Access. Retourne les claims si OK, lève sinon.

    Vérifie :
      - signature (RS256 contre les JWKS Cloudflare)
      - audience (tag du tunnel)
      - expiration (exp claim)
      - issuer (https://<team>.cloudflareaccess.com)
    """
    keys = await _fetch_jwks(team_domain)

    # Trouver la clé qui matche le kid du token
    header = pyjwt.get_unverified_header(token)
    kid = header.get("kid")
    matching_key = next((k for k in keys if k.get("kid") == kid), None)
    if not matching_key:
        # Refetch JWKS au cas où Cloudflare aurait rotaté
        _jwks_cache["fetched_at"] = 0.0
        keys = await _fetch_jwks(team_domain)
        matching_key = next((k for k in keys if k.get("kid") == kid), None)
        if not matching_key:
            raise pyjwt.InvalidTokenError(f"Aucune clé JWKS pour kid={kid}")

    # PyJWT supporte directement les JWK
    public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(matching_key)
    issuer = f"https://{team_domain}"

    claims = pyjwt.decode(
        token,
        key=public_key,  # type: ignore[arg-type]
        algorithms=["RS256"],
        audience=audience,
        issuer=issuer,
    )
    return claims


class CloudflareAccessMiddleware(BaseHTTPMiddleware):
    """Valide le JWT Cf-Access-Jwt-Assertion sur toutes les requêtes.

    Inactif si team_domain ou audience vides (dev local sans Cloudflare).

    Skip pour :
      - /v1/health et /v1/ready (health checks Docker)
      - GET / (root)
      - /docs, /redoc, /openapi.json (Swagger en dev seulement, à exclure en prod)
      - /v1/oauth/callback (OAuth callback Google, pas via Cloudflare)
    """

    SKIP_PATHS = {
        "/v1/health",
        "/v1/ready",
        "/",
        "/v1/oauth/callback",  # callback Google, pas via Cloudflare
        # /docs, /redoc, /openapi.json intentionnellement absents :
        # en dev (CF Access non configuré) → middleware transparent, Swagger accessible.
        # en prod (CF Access configuré) → Swagger protégé comme tout le reste.
    }

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        team_domain = settings.cf_access_team_domain
        audience = settings.cf_access_audience

        # Pas configuré + mode prod : on bloque tout sauf health (defense en
        # profondeur si quelqu'un a foire le startup-check via env edge case).
        if not team_domain or not audience:
            if settings.is_production and request.url.path not in self.SKIP_PATHS:
                logger.error("cf_access_misconfigured_in_production", path=request.url.path)
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Cloudflare Access non configuré en production"},
                )
            return await call_next(request)

        # Path skipped (health, openapi, etc.)
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)

        token = request.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            logger.warning("cf_access_missing_token", path=request.url.path)
            return JSONResponse(
                status_code=401,
                content={"detail": "Cloudflare Access JWT manquant"},
            )

        try:
            claims = await validate_cf_access_jwt(token, team_domain, audience)
            # Stocker l'email du user dans request.state (pour audit/log)
            request.state.cf_user_email = claims.get("email", "unknown")
        except pyjwt.ExpiredSignatureError:
            return JSONResponse(status_code=401, content={"detail": "JWT expiré"})
        except pyjwt.InvalidTokenError as e:
            logger.warning("cf_access_invalid_token", error=str(e), path=request.url.path)
            return JSONResponse(status_code=401, content={"detail": "JWT invalide"})

        return await call_next(request)
