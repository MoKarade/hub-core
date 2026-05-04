"""Endpoint /v1/garmin — Garmin Connect (Phase 4+).

Authentification via garminconnect v0.3.x + garth.
Les tokens sont sérialisés via client.dumps() et stockés chiffrés
dans OAuthToken (provider="garmin", service="connect").

Métriques synchronisées (source="garmin" dans HealthMetric) :
  steps, distance_m, calories, active_minutes
  heart_rate_resting, heart_rate_avg
  sleep_total_min, sleep_deep_min, sleep_rem_min, sleep_light_min
  weight_kg, body_fat_pct
  stress_avg, body_battery_min, body_battery_max
  oxygen_saturation, hrv_avg_ms

garminconnect est synchrone → tout appel API passe par asyncio.to_thread().

Flux d'authentification :
  1. POST /v1/garmin/connect {email, password}
       → si ok              : {status: "connected"}
       → si MFA Garmin      : {status: "mfa_required", session_id: "uuid"}
  2. POST /v1/garmin/connect {session_id, mfa_code}
       → {status: "connected"}
  3. POST /v1/garmin/sync  → importe les N derniers jours
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.crypto import decrypt_str, encrypt_str
from src.db.models.health_metric import HealthMetric
from src.db.models.oauth_token import OAuthToken
from src.db.session import get_db

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/garmin", tags=["garmin"])

GARMIN_PROVIDER = "garmin"
GARMIN_SERVICE = "connect"
DEFAULT_USER = "marc.richard4@gmail.com"

# Cache mémoire des sessions en attente de MFA (single-process, single-user → OK).
# {session_id: {"email": str, "password": str}}
_mfa_sessions: dict[str, dict[str, str]] = {}


class _MfaRequiredError(Exception):
    """Sentinel interne pour interrompre le login quand MFA est requis."""


# ---------------------------------------------------------------------------
# Fonctions sync (garminconnect est sync → exécutées dans asyncio.to_thread)
# ---------------------------------------------------------------------------


def _login_sync(
    email: str,
    password: str,
    mfa_code: str | None = None,
) -> tuple[str | None, bool]:
    """Tente un login Garmin Connect.

    Retourne :
        (tokens_json, False) si succès
        (None, True) si MFA requis et mfa_code=None
    Lève ValueError sur erreur d'authentification.
    """
    import garminconnect

    mfa_needed = False

    def _prompt_mfa() -> str:
        # Signature v0.3.x : Callable[[], str] (aucun argument)
        nonlocal mfa_needed
        if mfa_code is None:
            mfa_needed = True
            raise _MfaRequiredError()
        return mfa_code

    api = garminconnect.Garmin(email, password, prompt_mfa=_prompt_mfa)
    try:
        api.login()
    except _MfaRequiredError:
        return None, True
    except garminconnect.GarminConnectAuthenticationError as e:
        raise ValueError(f"Identifiants Garmin invalides : {e}") from e
    except garminconnect.GarminConnectTooManyRequestsError as e:
        raise ValueError("Trop de tentatives — attends quelques minutes avant de réessayer.") from e
    except Exception as e:
        # Certaines versions lèvent des exceptions génériques sur erreur MFA
        msg = str(e).lower()
        if mfa_needed or any(x in msg for x in ("mfa", "2fa", "factor", "totp", "otp")):
            if mfa_code is None:
                return None, True
        raise ValueError(f"Erreur connexion Garmin : {e}") from e

    tokens_json = api.client.dumps()
    return tokens_json, False


def _load_api_sync(tokens_json: str) -> Any:
    """Recrée une instance Garmin à partir des tokens sérialisés.

    N'appelle PAS login() — charge directement les tokens dans le client.
    """
    import garminconnect

    api = garminconnect.Garmin()
    api.client.loads(tokens_json)
    return api


def _fetch_day_metrics(api: Any, day: date) -> dict[str, float]:
    """Récupère toutes les métriques Garmin pour un jour.

    Chaque source est isolée dans un try/except pour ne pas bloquer les autres
    si une API renvoie une erreur ou des données vides.
    """
    metrics: dict[str, float] = {}
    ds = day.isoformat()

    # ── Stats journalières (steps, distance, calories, HR, stress) ─────────
    try:
        stats = api.get_stats(ds) or {}
        if (v := stats.get("totalSteps")) and v > 0:
            metrics["steps"] = float(v)
        if (v := stats.get("totalDistanceMeters")) and v > 0:
            metrics["distance_m"] = float(v)
        # Garmin retourne des kcal ("kilocalories" = ce qu'on appelle "calories" en sport)
        if (v := stats.get("activeKilocalories")) and v > 0:
            metrics["calories"] = float(v)
        # active_minutes = (highlyActive + active) en secondes → minutes
        ha = stats.get("highlyActiveSeconds") or 0
        act = stats.get("activeSeconds") or 0
        if ha + act > 0:
            metrics["active_minutes"] = round((ha + act) / 60, 1)
        if (v := stats.get("restingHeartRate")) and v > 0:
            metrics["heart_rate_resting"] = float(v)
        if (v := stats.get("averageHeartRate")) and v > 0:
            metrics["heart_rate_avg"] = float(v)
        # Stress : -1 = non mesuré
        if (v := stats.get("averageStressLevel")) is not None and v >= 0:
            metrics["stress_avg"] = float(v)
    except Exception as e:
        logger.debug("garmin_stats_error date=%s err=%s", ds, e)

    # ── Sommeil ─────────────────────────────────────────────────────────────
    try:
        sleep = api.get_sleep_data(ds) or {}
        dto = sleep.get("dailySleepDTO") or {}
        if (total_s := dto.get("sleepTimeSeconds") or 0) > 0:
            metrics["sleep_total_min"] = round(total_s / 60, 1)
        if (deep_s := dto.get("deepSleepSeconds") or 0) > 0:
            metrics["sleep_deep_min"] = round(deep_s / 60, 1)
        if (rem_s := dto.get("remSleepSeconds") or 0) > 0:
            metrics["sleep_rem_min"] = round(rem_s / 60, 1)
        if (light_s := dto.get("lightSleepSeconds") or 0) > 0:
            metrics["sleep_light_min"] = round(light_s / 60, 1)
    except Exception as e:
        logger.debug("garmin_sleep_error date=%s err=%s", ds, e)

    # ── Composition corporelle (poids, body fat) ─────────────────────────
    try:
        body = api.get_body_composition(ds, ds) or {}
        avg = body.get("totalAverage") or {}
        # Garmin retourne le poids en grammes
        if (v := avg.get("weight")) and v > 0:
            metrics["weight_kg"] = round(v / 1000, 2)
        if (v := avg.get("bodyFatPercentage")) and v > 0:
            metrics["body_fat_pct"] = float(v)
    except Exception as e:
        logger.debug("garmin_body_error date=%s err=%s", ds, e)

    # ── Body Battery ─────────────────────────────────────────────────────
    try:
        bb = api.get_body_battery(ds, ds) or []
        if bb and isinstance(bb, list) and bb[0]:
            entry = bb[0]
            if (v := entry.get("charged")) is not None:
                metrics["body_battery_max"] = float(v)
            if (v := entry.get("drained")) is not None:
                metrics["body_battery_min"] = float(v)
    except Exception as e:
        logger.debug("garmin_body_battery_error date=%s err=%s", ds, e)

    # ── SpO2 ──────────────────────────────────────────────────────────────
    try:
        spo2 = api.get_spo2_data(ds) or {}
        if (v := spo2.get("averageSpO2")) and v > 0:
            metrics["oxygen_saturation"] = float(v)
    except Exception as e:
        logger.debug("garmin_spo2_error date=%s err=%s", ds, e)

    # ── HRV ───────────────────────────────────────────────────────────────
    try:
        hrv = api.get_hrv_data(ds) or {}
        hrv_s = hrv.get("hrvSummary") or {}
        # lastNight est la valeur HRV moyenne de la nuit (en ms)
        v = hrv_s.get("lastNight") or hrv_s.get("weeklyAvg")
        if v and v > 0:
            metrics["hrv_avg_ms"] = float(v)
    except Exception as e:
        logger.debug("garmin_hrv_error date=%s err=%s", ds, e)

    return metrics


def _sync_all_days(tokens_json: str, days_back: int) -> dict[str, dict[str, float]]:
    """Sync complet (synchrone, pour asyncio.to_thread).

    Retourne {date_iso: {metric: value}}.
    """
    api = _load_api_sync(tokens_json)
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days_back - 1)

    result: dict[str, dict[str, float]] = {}
    day = start
    while day <= today:
        day_metrics = _fetch_day_metrics(api, day)
        if day_metrics:
            result[day.isoformat()] = day_metrics
        day += timedelta(days=1)

    return result


# ---------------------------------------------------------------------------
# Schémas Pydantic
# ---------------------------------------------------------------------------


class GarminConnectRequest(BaseModel):
    email: str = Field(default=DEFAULT_USER)
    password: str | None = Field(default=None, description="Mot de passe Garmin Connect")
    mfa_code: str | None = Field(default=None, description="Code MFA/TOTP si Garmin le demande")
    session_id: str | None = Field(
        default=None,
        description="ID de session MFA retourné par l'appel précédent (flux 2FA)",
    )


class GarminConnectResponse(BaseModel):
    status: str
    """'connected' | 'mfa_required'"""
    message: str
    session_id: str | None = None
    """Non-null si status='mfa_required' — repasse-le avec mfa_code"""


class GarminSyncRequest(BaseModel):
    user_email: str = Field(default=DEFAULT_USER)
    days_back: int = Field(default=30, ge=1, le=365)


class GarminSyncResponse(BaseModel):
    metrics_ingested: int
    metrics_updated: int
    days_processed: int
    duration_seconds: float


class GarminStatusResponse(BaseModel):
    connected: bool
    last_sync_date: date | None
    total_datapoints: int
    metrics_available: list[str]


# ---------------------------------------------------------------------------
# Helpers DB
# ---------------------------------------------------------------------------


async def _load_token_row(db: AsyncSession, user_email: str) -> OAuthToken | None:
    return (
        await db.execute(
            select(OAuthToken).where(
                OAuthToken.provider == GARMIN_PROVIDER,
                OAuthToken.service == GARMIN_SERVICE,
                OAuthToken.user_email == user_email,
            )
        )
    ).scalar_one_or_none()


async def _save_tokens(db: AsyncSession, user_email: str, tokens_json: str) -> None:
    encrypted = encrypt_str(tokens_json)
    expires_at = datetime.now(UTC) + timedelta(days=90)  # garth gère le refresh

    existing = await _load_token_row(db, user_email)
    if existing:
        existing.access_token_encrypted = encrypted
        existing.token_expires_at = expires_at
        existing.revoked_at = None
        existing.last_refreshed_at = datetime.now(UTC)
    else:
        db.add(
            OAuthToken(
                provider=GARMIN_PROVIDER,
                service=GARMIN_SERVICE,
                user_email=user_email,
                access_token_encrypted=encrypted,
                token_expires_at=expires_at,
                scopes=["garmin_connect"],
            )
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/connect", response_model=GarminConnectResponse)
async def garmin_connect(
    payload: GarminConnectRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GarminConnectResponse:
    """Connecte le hub à Garmin Connect et stocke les tokens.

    Flux sans MFA :
        POST {email, password}  →  {status: "connected"}

    Flux avec MFA Garmin (TOTP) :
        POST {email, password}                     →  {status: "mfa_required", session_id}
        POST {session_id, mfa_code}  →  {status: "connected"}
    """
    # ── Étape 2 MFA : session_id + mfa_code ─────────────────────────────
    if payload.session_id:
        if not payload.mfa_code:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "session_id fourni mais mfa_code manquant",
            )
        session = _mfa_sessions.pop(payload.session_id, None)
        if session is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Session MFA expirée ou invalide — recommence avec {email, password}",
            )
        email = session["email"]
        password = session["password"]
        try:
            tokens_json, still_needs_mfa = await asyncio.to_thread(
                _login_sync, email, password, payload.mfa_code
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e

        if still_needs_mfa or not tokens_json:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code MFA incorrect ou expiré")

        await _save_tokens(db, email, tokens_json)
        logger.info("garmin_connected_mfa", email=email)
        return GarminConnectResponse(
            status="connected",
            message=f"Tokens Garmin sauvegardés pour {email}. Lance /v1/garmin/sync.",
        )

    # ── Étape 1 : login initial ──────────────────────────────────────────
    if not payload.password:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "password requis pour la connexion initiale",
        )

    try:
        tokens_json, needs_mfa = await asyncio.to_thread(
            _login_sync, payload.email, payload.password, payload.mfa_code
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e

    if needs_mfa:
        session_id = str(uuid.uuid4())
        _mfa_sessions[session_id] = {"email": payload.email, "password": payload.password}
        return GarminConnectResponse(
            status="mfa_required",
            message=(
                "Garmin demande un code MFA (TOTP). "
                "Reprends ce session_id avec ton code dans le prochain appel."
            ),
            session_id=session_id,
        )

    await _save_tokens(db, payload.email, tokens_json)  # type: ignore[arg-type]
    logger.info("garmin_connected", email=payload.email)
    return GarminConnectResponse(
        status="connected",
        message=f"Tokens Garmin sauvegardés pour {payload.email}. Lance /v1/garmin/sync.",
    )


@router.post("/sync", response_model=GarminSyncResponse)
async def garmin_sync(
    payload: GarminSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GarminSyncResponse:
    """Synchronise les métriques santé depuis Garmin Connect.

    Upsert dans health_metrics (source='garmin'). Idempotent.
    """
    t0 = time.monotonic()

    row = await _load_token_row(db, payload.user_email)
    if not row or row.is_revoked:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Pas de connexion Garmin — appelle d'abord POST /v1/garmin/connect",
        )

    try:
        tokens_json = decrypt_str(row.access_token_encrypted)
    except ValueError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Tokens corrompus en DB : {e}",
        ) from e

    try:
        all_days: dict[str, dict[str, float]] = await asyncio.to_thread(
            _sync_all_days, tokens_json, payload.days_back
        )
    except Exception as e:
        msg = str(e).lower()
        if any(x in msg for x in ("expired", "invalid", "unauthorized", "401", "403")):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Session Garmin expirée — reconnecte via POST /v1/garmin/connect",
            ) from e
        logger.exception("garmin_sync_error")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Erreur Garmin Connect : {e}",
        ) from e

    ingested = 0
    updated = 0

    for date_str, day_metrics in all_days.items():
        day = date.fromisoformat(date_str)
        for metric_name, value in day_metrics.items():
            existing = (
                await db.execute(
                    select(HealthMetric).where(
                        HealthMetric.user_email == payload.user_email,
                        HealthMetric.date == day,
                        HealthMetric.metric == metric_name,
                        HealthMetric.source == "garmin",
                    )
                )
            ).scalar_one_or_none()

            if existing:
                existing.value = value
                updated += 1
            else:
                db.add(
                    HealthMetric(
                        user_email=payload.user_email,
                        date=day,
                        metric=metric_name,
                        value=value,
                        source="garmin",
                    )
                )
                ingested += 1

    await db.commit()
    logger.info(
        "garmin_sync_done",
        email=payload.user_email,
        days=payload.days_back,
        ingested=ingested,
        updated=updated,
    )

    return GarminSyncResponse(
        metrics_ingested=ingested,
        metrics_updated=updated,
        days_processed=len(all_days),
        duration_seconds=round(time.monotonic() - t0, 2),
    )


@router.get("/status", response_model=GarminStatusResponse)
async def garmin_status(
    db: Annotated[AsyncSession, Depends(get_db)],
    user_email: str = DEFAULT_USER,
) -> GarminStatusResponse:
    """Statut de la connexion Garmin et résumé des données disponibles."""
    row = await _load_token_row(db, user_email)
    connected = row is not None and not row.is_revoked

    total = (
        await db.execute(
            select(func.count(HealthMetric.id)).where(
                HealthMetric.user_email == user_email,
                HealthMetric.source == "garmin",
            )
        )
    ).scalar() or 0

    last_date = (
        await db.execute(
            select(func.max(HealthMetric.date)).where(
                HealthMetric.user_email == user_email,
                HealthMetric.source == "garmin",
            )
        )
    ).scalar()

    metrics_available = list(
        (
            await db.execute(
                select(HealthMetric.metric)
                .where(
                    HealthMetric.user_email == user_email,
                    HealthMetric.source == "garmin",
                )
                .distinct()
                .order_by(HealthMetric.metric)
            )
        )
        .scalars()
        .all()
    )

    return GarminStatusResponse(
        connected=connected,
        last_sync_date=last_date,
        total_datapoints=int(total),
        metrics_available=metrics_available,
    )
