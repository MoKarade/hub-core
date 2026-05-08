"""Routes OAuth 2.0 — flow Authorization Code + PKCE pour les services Google.

Endpoints :
  GET  /v1/oauth/google/start?service=gmail        → redirige vers Google
  GET  /v1/oauth/callback?code=...&state=...       → reçoit, échange, sauve, redirige
  GET  /v1/oauth/status                            → liste des services connectés
  POST /v1/oauth/google/{service}/revoke           → révoque le token

Le `state` PKCE est stocké en mémoire (dict) — OK pour single-instance.
En multi-instance, faudrait Redis (out of scope Phase 3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.crypto import decrypt_str
from src.core.logging import logger, mask_email
from src.core.rate_limit import rate_limit
from src.db.models.oauth_token import OAuthToken
from src.db.session import get_db
from src.services.oauth_google import (
    SERVICE_SCOPES,
    GoogleService,
    build_authorize_url,
    exchange_code_for_tokens,
    generate_pkce_pair,
    generate_state,
    get_userinfo,
    revoke_token,
    save_token,
)

router = APIRouter(prefix="/oauth", tags=["oauth"])

# ── State storage (in-memory) ────────────────────────────────────────────────
# state → (service, code_verifier, created_at UTC). Expire après 10 min.
# ⚠️ Multi-worker (uvicorn --workers >1) : chaque worker a son dict, callback
# peut tomber sur un worker qui ne connaît pas le state. Pour multi-worker,
# remplacer par Redis ou table DB. Single-worker = OK pour usage perso.
_STATE_STORE: dict[str, tuple[str, str, datetime]] = {}
_STATE_TTL_SECONDS = 600


def _cleanup_old_states() -> None:
    """Retire les states expirés du store (appelé à chaque start).

    Construit la liste des cles expirees + les pop en une passe atomique
    (snapshot via list() puis dict.pop). Asyncio peut interrompre une coroutine
    entre operations, donc on snapshot d'abord pour eviter les "dictionary
    changed size during iteration".
    """
    now = datetime.now(UTC)
    snapshot = list(_STATE_STORE.items())
    for k, (_, _, ts) in snapshot:
        if (now - ts).total_seconds() > _STATE_TTL_SECONDS:
            _STATE_STORE.pop(k, None)


# ── Schémas réponse ──────────────────────────────────────────────────────────


class OAuthStatusItem(BaseModel):
    """État d'un token OAuth pour un service."""

    provider: str
    service: str
    user_email: str
    connected: bool
    expired: bool
    revoked: bool
    scopes: list[str]
    expires_at: datetime
    last_refreshed_at: datetime | None


class OAuthStatusResponse(BaseModel):
    """Liste des services OAuth connectés."""

    tokens: list[OAuthStatusItem]
    available_services: list[str]


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/google/start")
async def oauth_google_start(
    service: Annotated[
        str, Query(description="gmail/photos/drive/calendar/fitness/people/tasks/youtube/all")
    ] = "all",
):
    """Démarre le flow OAuth pour un service Google donné.

    Génère state + PKCE, stocke côté serveur, redirige vers Google.
    """
    settings = get_settings()
    if not settings.google_oauth_client_id:
        raise HTTPException(
            status_code=503,
            detail="OAuth Google non configuré. Manque GOOGLE_OAUTH_CLIENT_ID dans .env.",
        )
    if service not in (*SERVICE_SCOPES.keys(), "all"):
        raise HTTPException(status_code=400, detail=f"Service inconnu : {service}")

    _cleanup_old_states()

    state = generate_state()
    code_verifier, code_challenge = generate_pkce_pair()
    _STATE_STORE[state] = (service, code_verifier, datetime.now(UTC))

    url = build_authorize_url(service, state, code_challenge)
    logger.info("oauth_start", service=service, state=state[:8])
    return RedirectResponse(url=url, status_code=302)


@router.get("/callback")
async def oauth_callback(
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
):
    """Callback OAuth — reçoit le code de Google, échange contre tokens, sauve en DB.

    Redirige le browser vers le frontend après succès/échec.
    """
    settings = get_settings()
    frontend_url = settings.frontend_url.rstrip("/")

    if error:
        logger.warning("oauth_callback_error", error=error)
        return RedirectResponse(
            url=f"{frontend_url}/oauth/error?error={error}",
            status_code=302,
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Manque code ou state")

    stored = _STATE_STORE.pop(state, None)
    if not stored:
        raise HTTPException(status_code=400, detail="State invalide ou expiré (replay attack ?)")

    service, code_verifier, _created = stored

    try:
        token_data = await exchange_code_for_tokens(code, code_verifier)
    except Exception as e:
        logger.error("oauth_token_exchange_failed", error=str(e))
        return RedirectResponse(
            url=f"{frontend_url}/oauth/error?error=token_exchange_failed", status_code=302
        )

    # Récupère l'email du user via userinfo (sinon fail proprement, sans
    # fallback "unknown" qui crée des tokens orphelins + collisions sur la
    # contrainte unique au prochain consent)
    try:
        userinfo = await get_userinfo(token_data["access_token"])
        user_email = userinfo.get("email")
        if not user_email:
            raise ValueError("Pas d'email dans userinfo response")
    except Exception as e:
        logger.error("oauth_userinfo_failed", error=str(e))
        return RedirectResponse(
            url=f"{frontend_url}/oauth/error?error=userinfo_failed", status_code=302
        )

    expires_in = token_data.get("expires_in")
    if not expires_in or not isinstance(expires_in, int):
        logger.error("oauth_no_expires_in", token_data_keys=list(token_data.keys()))
        return RedirectResponse(
            url=f"{frontend_url}/oauth/error?error=invalid_token_response", status_code=302
        )

    await save_token(
        db,
        service=service,
        user_email=user_email,
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_in=expires_in,
        scope=token_data.get("scope", ""),
    )

    logger.info("oauth_token_saved", service=service, user_email=mask_email(user_email))
    return RedirectResponse(url=f"{frontend_url}/settings?oauth_success={service}", status_code=302)


@router.get("/status", response_model=OAuthStatusResponse)
async def oauth_status(db: AsyncSession = Depends(get_db)) -> OAuthStatusResponse:
    """Liste les tokens OAuth en DB (sans exposer les valeurs en clair)."""
    stmt = select(OAuthToken).order_by(OAuthToken.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    items = [
        OAuthStatusItem(
            provider=t.provider,
            service=t.service,
            user_email=t.user_email,
            connected=t.is_usable,
            expired=t.is_expired,
            revoked=t.is_revoked,
            scopes=t.scopes,
            expires_at=t.token_expires_at,
            last_refreshed_at=t.last_refreshed_at,
        )
        for t in rows
    ]
    return OAuthStatusResponse(
        tokens=items,
        available_services=sorted(SERVICE_SCOPES.keys()),
    )


@router.post("/google/{service}/revoke")
async def oauth_google_revoke(
    service: GoogleService,
    db: AsyncSession = Depends(get_db),
):
    """Révoque le token Google pour un service donné. Marque revoked_at en DB
    + appelle l'endpoint Google /revoke (best-effort)."""
    stmt = select(OAuthToken).where(
        OAuthToken.provider == "google",
        OAuthToken.service == service,
    )
    token = (await db.execute(stmt)).scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail=f"Aucun token pour service={service}")

    # Best-effort revoke côté Google
    try:
        access = decrypt_str(token.access_token_encrypted)
        await revoke_token(access)
    except Exception as e:
        logger.warning("oauth_revoke_remote_failed", service=service, error=str(e))

    # Marque révoqué localement
    token.revoked_at = datetime.now(UTC)
    await db.commit()

    return {"status": "revoked", "service": service}


@router.post("/cleanup", dependencies=[Depends(rate_limit(5, 60))])
async def oauth_cleanup(db: AsyncSession = Depends(get_db)):
    """Supprime de la DB les tokens révoqués qui ont un fallback "all" valide.

    Utile après un consent "scope=all" : les anciens tokens par service
    deviennent automatiquement obsolètes (Google invalide leurs refresh_tokens
    quand on consent un scope plus large). Cet endpoint fait le ménage pour
    désencombrer /v1/oauth/status.

    Garde : on supprime UNIQUEMENT si un token "all" non-révoqué existe pour
    le même user_email (sinon on perd l'info que le service a été connecté).
    """
    from sqlalchemy import delete

    # 1. Récupère les tokens "all" actifs par user
    stmt_all = select(OAuthToken).where(
        OAuthToken.provider == "google",
        OAuthToken.service == "all",
        OAuthToken.revoked_at.is_(None),
    )
    all_tokens = (await db.execute(stmt_all)).scalars().all()
    users_with_all = {t.user_email for t in all_tokens}

    if not users_with_all:
        return {"deleted": 0, "reason": "Aucun token 'all' actif pour fallback."}

    # 2. Supprime les tokens service-spécifiques révoqués pour ces users
    stmt_del = delete(OAuthToken).where(
        OAuthToken.provider == "google",
        OAuthToken.service != "all",
        OAuthToken.revoked_at.is_not(None),
        OAuthToken.user_email.in_(users_with_all),
    )
    result = await db.execute(stmt_del)
    await db.commit()

    deleted = result.rowcount or 0
    logger.info("oauth_cleanup", deleted=deleted, users=list(users_with_all))
    return {"deleted": deleted, "fallback_users": sorted(users_with_all)}
