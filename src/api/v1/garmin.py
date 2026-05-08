"""Endpoint /v1/garmin — Garmin Connect (Phase 4+) — Forerunner 955.

Authentification via garminconnect v0.3.x + garth.
Les tokens sont sérialisés via client.dumps() et stockés chiffrés
dans OAuthToken (provider="garmin", service="connect").

Métriques PAR JOUR synchronisées (source="garmin" dans HealthMetric) :
  — Activité
  steps, distance_m, calories, calories_total, bmr_calories
  active_minutes, sedentary_minutes
  floors, floors_ascended_m, floors_descended_m
  intensity_moderate_min, intensity_vigorous_min

  — Fréquence cardiaque
  heart_rate_resting, heart_rate_min, heart_rate_max, rhr_7day_avg

  — Stress & récupération
  stress_avg, stress_max
  body_battery_max, body_battery_min, body_battery_end
  body_battery_charged, body_battery_drained

  — Respiration
  respiration_waking_avg, respiration_min, respiration_max

  — Sommeil
  sleep_total_min, sleep_deep_min, sleep_rem_min, sleep_light_min, sleep_awake_min

  — Oxymétrie (port la nuit)
  oxygen_saturation, oxygen_saturation_min, sleep_spo2_avg

  — HRV
  hrv_avg_ms

  — Préparation à l'entraînement
  training_readiness, recovery_time_h

  — Composition corporelle (si balance Garmin)
  weight_kg, body_fat_pct

Métriques GLOBALES (stockées sur la date du jour du sync) :
  race_time_5k_s, race_time_10k_s, race_time_half_s, race_time_marathon_s
  cycling_ftp_w
  fitness_age, fitness_age_best
  endurance_score

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
import json
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.crypto import decrypt_str, encrypt_str
from src.core.logging import mask_email
from src.core.rate_limit import rate_limit
from src.db.models.health_metric import HealthMetric
from src.db.models.oauth_token import OAuthToken
from src.db.session import get_db

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/garmin", tags=["garmin"])

GARMIN_PROVIDER = "garmin"
GARMIN_SERVICE = "connect"
DEFAULT_USER: str = get_settings().hub_owner_email

# Cache mémoire des sessions en attente de MFA (single-process, single-user → OK).
# {session_id: {"email": str, "password": str, "created_at": float}}
# TTL 5 minutes : mot de passe ne doit pas rester en RAM indéfiniment.
_MFA_SESSION_TTL = 300.0
_mfa_sessions: dict[str, dict[str, object]] = {}


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
        msg = str(e).lower()
        if mfa_needed or any(x in msg for x in ("mfa", "2fa", "factor", "totp", "otp")):
            if mfa_code is None:
                return None, True
        raise ValueError(f"Erreur connexion Garmin : {e}") from e

    # Sérialise tokens + display_name dans un wrapper JSON.
    # display_name (UUID Garmin) est requis par get_stats/get_user_summary.
    # Sans lui, ces endpoints échouent même avec un token valide.
    garth_tokens = api.client.dumps()
    tokens_json = json.dumps({"g": garth_tokens, "dn": api.display_name or ""})
    return tokens_json, False


def _load_api_sync(tokens_json: str) -> Any:
    """Recrée une instance Garmin à partir des tokens sérialisés sans re-login.

    Deux formats supportés :
    - Nouveau : {"g": <garth_tokens>, "dn": <display_name>}
    - Ancien (backward compat) : <garth_tokens> directement

    display_name est requis par get_stats/get_user_summary.
    Si absent du token stocké, il est récupéré via get_userprofile_settings().
    """
    import garminconnect

    api = garminconnect.Garmin()

    try:
        wrapper = json.loads(tokens_json)
        if isinstance(wrapper, dict) and "g" in wrapper:
            # Nouveau format avec display_name intégré
            api.client.loads(wrapper["g"])
            api.display_name = wrapper.get("dn") or None
        else:
            # Vieux format (plain garth JSON)
            api.client.loads(tokens_json)
    except (json.JSONDecodeError, TypeError):
        api.client.loads(tokens_json)

    # Si display_name manque (anciens tokens), le récupérer via API
    if not api.display_name:
        try:
            settings = api.get_userprofile_settings() or {}
            api.display_name = settings.get("displayName")
        except Exception as e:
            # Sans display_name, get_stats échouera mais les autres endpoints tiendront.
            # On log en warning : si ça arrive en prod, ~25 métriques par jour sont
            # silencieusement perdues — Marc doit le voir.
            logger.warning("garmin_display_name_fetch_failed", err=str(e)[:200])

    return api


def _safe(fn: Any, label: str) -> Any:
    """Appelle fn(), logue l'erreur et retourne None si ça échoue.

    Log en warning (pas debug) : un endpoint Garmin down ou un token expiré
    fait silencieusement perdre une métrique santé. Marc doit le voir dans
    les logs sans avoir à activer DEBUG.
    """
    try:
        return fn()
    except Exception as e:
        logger.warning("garmin_api_error", label=label, err=str(e)[:200])
        return None


def _fetch_day_metrics(api: Any, day: date) -> dict[str, float]:
    """Récupère toutes les métriques Garmin disponibles pour un jour.

    Chaque source est isolée dans un try/except pour ne jamais bloquer les autres.
    Source principale : get_stats() qui contient ~25 champs en un seul appel.
    """
    metrics: dict[str, float] = {}
    ds = day.isoformat()

    # ── get_stats : source centrale (~25 métriques en 1 appel) ──────────────
    stats = _safe(lambda: api.get_stats(ds), "get_stats") or {}

    def _s(key: str) -> float | None:
        v = stats.get(key)
        return float(v) if v is not None and v != 0 else None

    def _snz(key: str) -> float | None:
        """get_stats field, skip if null OR zero."""
        v = stats.get(key)
        return float(v) if v is not None and isinstance(v, (int, float)) and v > 0 else None

    # Activité
    if v := _snz("totalSteps"):
        metrics["steps"] = v
    if v := _snz("totalDistanceMeters"):
        metrics["distance_m"] = v
    if v := _snz("activeKilocalories"):
        metrics["calories"] = v
    if v := _snz("totalKilocalories"):
        metrics["calories_total"] = v
    if v := _snz("bmrKilocalories"):
        metrics["bmr_calories"] = v

    ha = stats.get("highlyActiveSeconds") or 0
    act = stats.get("activeSeconds") or 0
    if ha + act > 0:
        metrics["active_minutes"] = round((ha + act) / 60, 1)

    if v := _snz("sedentarySeconds"):
        metrics["sedentary_minutes"] = round(v / 60, 1)

    # Étages
    if (v := stats.get("floorsAscended")) is not None and v > 0:
        metrics["floors"] = round(float(v), 2)
    if (v := stats.get("floorsAscendedInMeters")) is not None and v > 0:
        metrics["floors_ascended_m"] = round(float(v), 1)
    if (v := stats.get("floorsDescendedInMeters")) is not None and v > 0:
        metrics["floors_descended_m"] = round(float(v), 1)

    # Intensity minutes (hebdo cumulatif, valeur du jour)
    if (v := stats.get("moderateIntensityMinutes")) is not None and v > 0:
        metrics["intensity_moderate_min"] = float(v)
    if (v := stats.get("vigorousIntensityMinutes")) is not None and v > 0:
        metrics["intensity_vigorous_min"] = float(v)

    # Fréquence cardiaque (depuis stats)
    if v := _snz("restingHeartRate"):
        metrics["heart_rate_resting"] = v
    if v := _snz("maxHeartRate"):
        metrics["heart_rate_max"] = v
    if v := _snz("minHeartRate"):
        metrics["heart_rate_min"] = v
    if v := _snz("lastSevenDaysAvgRestingHeartRate"):
        metrics["rhr_7day_avg"] = v

    # Stress (averageStressLevel ≥ 0 = mesuré, -1 = non mesuré)
    if (v := stats.get("averageStressLevel")) is not None and v >= 0:
        metrics["stress_avg"] = float(v)
    if (v := stats.get("maxStressLevel")) is not None and v > 0:
        metrics["stress_max"] = float(v)

    # Body Battery (depuis stats — plus complet que get_body_battery)
    if (v := stats.get("bodyBatteryHighestValue")) is not None and v > 0:
        metrics["body_battery_max"] = float(v)
    if (v := stats.get("bodyBatteryLowestValue")) is not None and v > 0:
        metrics["body_battery_min"] = float(v)
    if (v := stats.get("bodyBatteryMostRecentValue")) is not None and v > 0:
        metrics["body_battery_end"] = float(v)
    if (v := stats.get("bodyBatteryChargedValue")) is not None and v > 0:
        metrics["body_battery_charged"] = float(v)
    if (v := stats.get("bodyBatteryDrainedValue")) is not None and v > 0:
        metrics["body_battery_drained"] = float(v)

    # Respiration (depuis stats)
    if (v := stats.get("avgWakingRespirationValue")) is not None and v > 0:
        metrics["respiration_waking_avg"] = round(float(v), 1)
    if (v := stats.get("highestRespirationValue")) is not None and v > 0:
        metrics["respiration_max"] = round(float(v), 1)
    if (v := stats.get("lowestRespirationValue")) is not None and v > 0:
        metrics["respiration_min"] = round(float(v), 1)

    # ── Sommeil ─────────────────────────────────────────────────────────────
    sleep = _safe(lambda: api.get_sleep_data(ds), "get_sleep_data") or {}
    dto = sleep.get("dailySleepDTO") or {}

    if (total_s := dto.get("sleepTimeSeconds") or 0) > 0:
        metrics["sleep_total_min"] = round(total_s / 60, 1)
    if (v := dto.get("deepSleepSeconds") or 0) > 0:
        metrics["sleep_deep_min"] = round(v / 60, 1)
    if (v := dto.get("remSleepSeconds") or 0) > 0:
        metrics["sleep_rem_min"] = round(v / 60, 1)
    if (v := dto.get("lightSleepSeconds") or 0) > 0:
        metrics["sleep_light_min"] = round(v / 60, 1)
    if (v := dto.get("awakeSleepSeconds") or 0) > 0:
        metrics["sleep_awake_min"] = round(v / 60, 1)

    # Respiration nocturne (dans sleep_data, séparée du waking)
    for k_resp, k_metric in [
        ("avgSleepRespirationValue", "sleep_respiration_avg"),
        ("highestRespirationValue", "sleep_respiration_max"),
        ("lowestRespirationValue", "sleep_respiration_min"),
    ]:
        v = dto.get(k_resp)
        if v and float(v) > 0:
            metrics[k_metric] = round(float(v), 1)

    # ── SpO2 ─────────────────────────────────────────────────────────────────
    spo2 = _safe(lambda: api.get_spo2_data(ds), "get_spo2_data") or {}
    if (v := spo2.get("averageSpO2")) and v > 0:
        metrics["oxygen_saturation"] = float(v)
    if (v := spo2.get("lowestSpO2")) and v > 0:
        metrics["oxygen_saturation_min"] = float(v)
    if (v := spo2.get("avgSleepSpO2")) and v > 0:
        metrics["sleep_spo2_avg"] = float(v)

    # ── HRV ──────────────────────────────────────────────────────────────────
    hrv = _safe(lambda: api.get_hrv_data(ds), "get_hrv_data") or {}
    hrv_s = hrv.get("hrvSummary") or {}
    v = hrv_s.get("lastNight") or hrv_s.get("weeklyAvg")
    if v and v > 0:
        metrics["hrv_avg_ms"] = float(v)

    # ── Training readiness ───────────────────────────────────────────────────
    tr_list = _safe(lambda: api.get_training_readiness(ds), "get_training_readiness") or []
    tr = tr_list[0] if isinstance(tr_list, list) and tr_list else (tr_list or {})
    if isinstance(tr, dict):
        if (v := tr.get("score")) is not None and v > 0:
            metrics["training_readiness"] = float(v)
        if (v := tr.get("recoveryTime")) is not None and v >= 0:
            metrics["recovery_time_h"] = float(v)

    # ── Composition corporelle (si balance Garmin connectée) ─────────────────
    body = _safe(lambda: api.get_body_composition(ds, ds), "get_body_composition") or {}
    avg = body.get("totalAverage") or {}
    if (v := avg.get("weight")) and v > 0:
        metrics["weight_kg"] = round(v / 1000, 2)  # grammes → kg
    if (v := avg.get("bodyFatPercentage")) and v > 0:
        metrics["body_fat_pct"] = float(v)

    return metrics


def _fetch_global_metrics(api: Any, today: date) -> dict[str, float]:
    """Métriques ponctuelles stockées sur la date d'aujourd'hui.

    Race predictions, FTP, fitness age, endurance score — valeurs actuelles
    de Garmin Connect, mises à jour à chaque sync.
    """
    metrics: dict[str, float] = {}
    ds = today.isoformat()

    # Race predictions (temps prédits 5K/10K/half/marathon en secondes)
    rp = _safe(lambda: api.get_race_predictions(), "get_race_predictions") or {}
    for k_api, k_metric in [
        ("time5K", "race_time_5k_s"),
        ("time10K", "race_time_10k_s"),
        ("timeHalfMarathon", "race_time_half_s"),
        ("timeMarathon", "race_time_marathon_s"),
    ]:
        if (v := rp.get(k_api)) and v > 0:
            metrics[k_metric] = float(v)

    # Cycling FTP (Functional Threshold Power en watts)
    ftp = _safe(lambda: api.get_cycling_ftp(), "get_cycling_ftp") or {}
    if (v := ftp.get("functionalThresholdPower")) and v > 0:
        metrics["cycling_ftp_w"] = float(v)

    # Fitness age (âge biologique calculé par Garmin)
    fa = _safe(lambda: api.get_fitnessage_data(ds), "get_fitnessage_data") or {}
    if (v := fa.get("fitnessAge")) and v > 0:
        metrics["fitness_age"] = round(float(v), 1)
    if (v := fa.get("achievableFitnessAge")) and v > 0:
        metrics["fitness_age_best"] = round(float(v), 1)

    # Endurance score (score global de condition aérobie)
    es = _safe(lambda: api.get_endurance_score(ds, ds), "get_endurance_score") or {}
    es_dto = es.get("enduranceScoreDTO") or {}
    if (v := es_dto.get("overallScore")) and v > 0:
        metrics["endurance_score"] = float(v)

    return metrics


def _sync_all_days(tokens_json: str, days_back: int) -> dict[str, dict[str, float]]:
    """Sync complet (synchrone, pour asyncio.to_thread).

    Retourne {date_iso: {metric: value}}.
    Les métriques globales sont stockées sur la date d'aujourd'hui.
    """
    api = _load_api_sync(tokens_json)
    today = datetime.now(UTC).date()
    start = today - timedelta(days=days_back - 1)

    result: dict[str, dict[str, float]] = {}

    # Métriques par jour
    day = start
    while day <= today:
        day_metrics = _fetch_day_metrics(api, day)
        if day_metrics:
            result[day.isoformat()] = day_metrics
        day += timedelta(days=1)

    # Métriques globales → ajoutées sur aujourd'hui
    global_metrics = _fetch_global_metrics(api, today)
    if global_metrics:
        existing = result.get(today.isoformat(), {})
        existing.update(global_metrics)
        result[today.isoformat()] = existing

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


@router.post(
    "/connect", response_model=GarminConnectResponse, dependencies=[Depends(rate_limit(2, 300))]
)
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
    # Purge proactive des sessions MFA expirées (mot de passe en RAM ≤ TTL).
    _now = time.monotonic()
    _expired_keys = [
        k for k, v in _mfa_sessions.items() if _now - float(v["created_at"]) > _MFA_SESSION_TTL
    ]
    for _k in _expired_keys:
        _mfa_sessions.pop(_k, None)

    # ── Étape 2 MFA : session_id + mfa_code ─────────────────────────────
    if payload.session_id:
        if not payload.mfa_code:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "session_id fourni mais mfa_code manquant",
            )
        session = _mfa_sessions.pop(payload.session_id, None)
        if session is None or (time.monotonic() - float(session["created_at"])) > _MFA_SESSION_TTL:
            _mfa_sessions.pop(payload.session_id, None)  # purge si expiré
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
            logger.warning("garmin_mfa_auth_failed", error=str(e)[:200])
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Code MFA invalide ou session expirée"
            ) from e

        if still_needs_mfa or not tokens_json:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Code MFA incorrect ou expiré")

        await _save_tokens(db, email, tokens_json)
        logger.info("garmin_connected_mfa", email=mask_email(email))
        return GarminConnectResponse(
            status="connected",
            message="Tokens Garmin sauvegardés (MFA). Lance /v1/garmin/sync.",
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
        logger.warning("garmin_connect_auth_failed", error=str(e)[:200])
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Identifiants Garmin invalides") from e

    if needs_mfa:
        session_id = str(uuid.uuid4())
        _mfa_sessions[session_id] = {
            "email": payload.email,
            "password": payload.password,
            "created_at": time.monotonic(),
        }
        return GarminConnectResponse(
            status="mfa_required",
            message=(
                "Garmin demande un code MFA (TOTP). "
                "Reprends ce session_id avec ton code dans le prochain appel."
            ),
            session_id=session_id,
        )

    await _save_tokens(db, payload.email, tokens_json)  # type: ignore[arg-type]
    logger.info("garmin_connected", email=mask_email(payload.email))
    return GarminConnectResponse(
        status="connected",
        message="Tokens Garmin sauvegardés. Lance /v1/garmin/sync.",
    )


@router.post("/sync", response_model=GarminSyncResponse, dependencies=[Depends(rate_limit(3, 60))])
async def garmin_sync(
    payload: GarminSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GarminSyncResponse:
    """Synchronise les métriques santé depuis Garmin Connect.

    Upsert dans health_metrics (source='garmin'). Idempotent.
    Forerunner 955 : ~35 métriques par jour + métriques globales (FTP, race times, fitness age).
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

    # Charge en 1 requete tous les HealthMetric existants pour la plage couverte
    # (au lieu d'1 SELECT par (date, metric) — ~1050 round-trips pour 30j x 35 metriques)
    days = [date.fromisoformat(d) for d in all_days]
    if days:
        existing_rows = (
            await db.execute(
                select(HealthMetric).where(
                    HealthMetric.user_email == payload.user_email,
                    HealthMetric.source == "garmin",
                    HealthMetric.date.in_(days),
                )
            )
        ).scalars().all()
        existing_map: dict[tuple[date, str], HealthMetric] = {
            (row.date, row.metric): row for row in existing_rows
        }
    else:
        existing_map = {}

    for date_str, day_metrics in all_days.items():
        day = date.fromisoformat(date_str)
        for metric_name, value in day_metrics.items():
            existing = existing_map.get((day, metric_name))
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
        email=mask_email(payload.user_email),
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
