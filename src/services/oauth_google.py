"""Service OAuth 2.0 pour Google APIs (Gmail/Photos/Drive/Calendar/Fit/People/Tasks).

Implémente le flow Authorization Code + PKCE :
  1. start_auth_flow() → URL Google avec state + code_challenge
  2. callback récupère code → exchange_code() → tokens
  3. refresh_access_token() avant expiration
  4. revoke_token() pour révocation propre

Les tokens sont chiffrés via Fernet (cf. src/core/crypto.py) avant stockage DB.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.crypto import decrypt_str, encrypt_str
from src.core.logging import logger
from src.db.models.oauth_token import OAuthToken

# ── Constantes Google OAuth ──────────────────────────────────────────────────

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

GoogleService = Literal[
    "gmail",
    "photos",
    "drive",
    "calendar",
    "fitness",
    "people",
    "tasks",
    "youtube",
    "all",  # tous les scopes en une fois
]

# Scopes read-only par service
SERVICE_SCOPES: dict[str, list[str]] = {
    "gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
    # Photos : Google a deprecier photoslibrary.readonly en 2025 pour les nouvelles apps.
    # On utilise la NOUVELLE Picker API (designed for new apps, marche sans verification).
    # Le user devra picker explicitement les photos a importer (1 fois par session).
    "photos": [
        "https://www.googleapis.com/auth/photospicker.mediaitems.readonly",
        "https://www.googleapis.com/auth/photoslibrary.readonly",
    ],
    "drive": ["https://www.googleapis.com/auth/drive.readonly"],
    "calendar": ["https://www.googleapis.com/auth/calendar.readonly"],
    "fitness": [
        "https://www.googleapis.com/auth/fitness.activity.read",
        "https://www.googleapis.com/auth/fitness.body.read",
        "https://www.googleapis.com/auth/fitness.sleep.read",
        "https://www.googleapis.com/auth/fitness.location.read",
    ],
    "people": ["https://www.googleapis.com/auth/contacts.readonly"],
    "tasks": ["https://www.googleapis.com/auth/tasks.readonly"],
    "youtube": ["https://www.googleapis.com/auth/youtube.readonly"],
}

# Scopes minimaux pour identifier l'user (toujours inclus)
BASE_SCOPES = ["openid", "email", "profile"]


def get_scopes_for(service: str) -> list[str]:
    """Retourne les scopes pour un service. 'all' = tous les services."""
    if service == "all":
        all_scopes = set(BASE_SCOPES)
        for s in SERVICE_SCOPES.values():
            all_scopes.update(s)
        return sorted(all_scopes)
    if service not in SERVICE_SCOPES:
        raise ValueError(f"Service OAuth inconnu : {service}")
    return BASE_SCOPES + SERVICE_SCOPES[service]


# ── PKCE (proof of key code exchange) ────────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """Génère (code_verifier, code_challenge) PKCE.

    code_verifier : 43-128 chars random URL-safe (à garder côté serveur)
    code_challenge : SHA256(verifier) en base64url (envoyé à Google)
    """
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def generate_state() -> str:
    """Génère un state token cryptographiquement sûr (CSRF protection)."""
    return secrets.token_urlsafe(32)


# ── Authorization URL ────────────────────────────────────────────────────────


def build_authorize_url(
    service: GoogleService,
    state: str,
    code_challenge: str,
) -> str:
    """Construit l'URL d'autorisation Google pour un service donné.

    Le user est redirigé là, consent au scope, puis Google redirige vers
    redirect_uri avec un `code` qu'on échangera côté serveur.
    """
    settings = get_settings()
    if not settings.google_oauth_client_id:
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID manquant dans .env")

    scopes = get_scopes_for(service)
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        # offline = on veut le refresh_token pour ne pas redemander consent
        "access_type": "offline",
        # prompt=consent force l'écran de consent (sinon Google peut skip
        # et ne pas redonner le refresh_token)
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# ── Token exchange ───────────────────────────────────────────────────────────


def _scrub_oauth_error(body: str) -> str:
    """Extrait l'`error` Google sans leak du body complet (qui peut contenir
    code_verifier ou client_secret reflected dans certaines erreurs).
    """
    try:
        if body.strip().startswith("{"):
            data = json.loads(body)
            if isinstance(data, dict):
                err = data.get("error", "unknown_error")
                desc = data.get("error_description", "")
                return f"{err}: {desc[:80]}" if desc else err
    except (json.JSONDecodeError, ValueError):
        pass
    return "unknown_error"


class InvalidGrantError(RuntimeError):
    """Refresh token révoqué/invalide côté Google. L'utilisateur doit re-consent."""


async def exchange_code_for_tokens(code: str, code_verifier: str) -> dict:
    """Échange le code reçu en callback contre access_token + refresh_token.

    Retourne le dict raw de Google :
      {
        access_token, refresh_token, expires_in, scope, token_type, id_token
      }
    """
    settings = get_settings()
    data = {
        "code": code,
        "client_id": settings.google_oauth_client_id,
        "client_secret": settings.google_oauth_client_secret,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(TOKEN_URL, data=data)
        if resp.status_code != 200:
            err = _scrub_oauth_error(resp.text)
            logger.error("oauth_exchange_failed", status=resp.status_code, error=err)
            raise RuntimeError(f"Token exchange échec : {resp.status_code} ({err})")
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    """Utilise le refresh_token pour obtenir un nouvel access_token.

    Retourne le dict de Google : {access_token, expires_in, scope, token_type}.
    Note: refresh_token peut être renvoyé en cas de rotation (rare).

    Lève InvalidGrantError si le refresh_token est révoqué/invalide
    (l'utilisateur doit re-consent).
    """
    settings = get_settings()
    data = {
        "refresh_token": refresh_token,
        "client_id": settings.google_oauth_client_id,
        "client_secret": settings.google_oauth_client_secret,
        "grant_type": "refresh_token",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(TOKEN_URL, data=data)
        if resp.status_code != 200:
            err = _scrub_oauth_error(resp.text)
            logger.error("oauth_refresh_failed", status=resp.status_code, error=err)
            if "invalid_grant" in err.lower():
                raise InvalidGrantError(f"Refresh token révoqué : {err}")
            raise RuntimeError(f"Refresh échec : {resp.status_code} ({err})")
        return resp.json()


async def revoke_token(token: str) -> None:
    """Révoque un token côté Google. Idempotent : pas d'erreur si déjà révoqué."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(REVOKE_URL, data={"token": token})
        # Google répond 200 si succès, 400 si token déjà invalide → on ignore
        logger.info("oauth_revoke", status=resp.status_code)


async def get_userinfo(access_token: str) -> dict:
    """Récupère l'email et nom du user via l'endpoint OIDC userinfo."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"userinfo échec : {resp.status_code}")
        return resp.json()


# ── DB persistence ───────────────────────────────────────────────────────────


async def save_token(
    db: AsyncSession,
    *,
    service: str,
    user_email: str,
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
    scope: str,
) -> OAuthToken:
    """UPSERT le token en DB (chiffré). Retourne l'objet OAuthToken sauvegardé."""
    # Cherche un existing pour ce trio (provider, service, user_email)
    stmt = select(OAuthToken).where(
        OAuthToken.provider == "google",
        OAuthToken.service == service,
        OAuthToken.user_email == user_email,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    scopes_list = scope.split(" ") if scope else []
    encrypted_access = encrypt_str(access_token)
    encrypted_refresh = encrypt_str(refresh_token) if refresh_token else None

    if existing:
        existing.access_token_encrypted = encrypted_access
        if encrypted_refresh:
            # Garde l'ancien refresh si Google n'en redonne pas (peut arriver)
            existing.refresh_token_encrypted = encrypted_refresh
        existing.token_expires_at = expires_at
        existing.scopes = scopes_list
        existing.revoked_at = None  # reset si re-consent après revoke
        existing.last_refreshed_at = datetime.now(UTC)
        token_obj = existing
    else:
        token_obj = OAuthToken(
            provider="google",
            service=service,
            user_email=user_email,
            access_token_encrypted=encrypted_access,
            refresh_token_encrypted=encrypted_refresh,
            token_expires_at=expires_at,
            scopes=scopes_list,
        )
        db.add(token_obj)

    await db.commit()
    await db.refresh(token_obj)
    return token_obj


async def get_valid_access_token(
    db: AsyncSession,
    *,
    service: str,
    user_email: str,
) -> str:
    """Retourne un access_token valide (refresh automatique si expiré).

    Lève RuntimeError si :
      - pas de token pour ce service
      - token révoqué
      - refresh échoue (re-consent nécessaire)
    """
    stmt = select(OAuthToken).where(
        OAuthToken.provider == "google",
        OAuthToken.service == service,
        OAuthToken.user_email == user_email,
    )
    token = (await db.execute(stmt)).scalar_one_or_none()
    if not token:
        raise RuntimeError(f"Aucun token Google pour service={service}")
    if token.is_revoked:
        raise RuntimeError(f"Token Google révoqué pour service={service}")

    # Si pas expiré (avec marge 60s), on retourne directement.
    # SQLite stocke datetime naive : on assume UTC si tzinfo manque.
    expires_at = token.token_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if datetime.now(UTC) < expires_at - timedelta(seconds=60):
        return decrypt_str(token.access_token_encrypted)

    # Sinon refresh
    if not token.refresh_token_encrypted:
        raise RuntimeError(
            f"Token expiré et pas de refresh_token pour {service}. Re-consent requis."
        )

    refresh = decrypt_str(token.refresh_token_encrypted)
    try:
        new_data = await refresh_access_token(refresh)
    except InvalidGrantError:
        # Refresh révoqué côté Google → mark révoqué localement, force re-consent
        token.revoked_at = datetime.now(UTC)
        await db.commit()
        raise

    # Update le token
    token.access_token_encrypted = encrypt_str(new_data["access_token"])
    token.token_expires_at = datetime.now(UTC) + timedelta(seconds=new_data["expires_in"])
    token.last_refreshed_at = datetime.now(UTC)
    # Google peut rotater le refresh_token (rare mais arrive)
    if "refresh_token" in new_data and new_data["refresh_token"]:
        token.refresh_token_encrypted = encrypt_str(new_data["refresh_token"])
    await db.commit()

    return new_data["access_token"]
