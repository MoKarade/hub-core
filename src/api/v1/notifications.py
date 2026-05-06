"""Endpoints Web Push (PWA notifications natives, remplace ntfy.sh).

Workflow :
1. Frontend appelle GET /vapid-public-key pour avoir la cle publique
2. Frontend demande permission notification + cree subscription via PushManager
3. Frontend POST /subscribe avec le {endpoint, keys: {p256dh, auth}}
4. Le backend stocke en DB (table push_subscriptions)
5. Quand on veut envoyer : POST /send (ou send_to_all_subscriptions helper)
   -> pywebpush iter sur toutes les subs, envoie via VAPID

Pas d'auth user-niveau pour l'instant : single user (Marc), tous les devices
sont consideres comme lui.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.db.models import PushSubscription
from src.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])


# ─────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=10)
    keys: SubscriptionKeys
    label: str | None = None  # optionnel : "Tel perso", "PC bureau"


class SubscribeResponse(BaseModel):
    id: UUID
    endpoint: str
    label: str | None
    status: str  # 'created' | 'updated'


class UnsubscribeRequest(BaseModel):
    endpoint: str


class SendNotificationRequest(BaseModel):
    title: str
    body: str
    url: str | None = None  # URL a ouvrir au clic (ex: /insights)
    icon: str | None = None
    tag: str | None = None  # remplace une notif existante avec meme tag
    require_interaction: bool = False


class SendNotificationResponse(BaseModel):
    sent: int
    failed: int
    revoked: int  # subs 404/410 marquees revoked
    total_subs: int


class SubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    label: str | None
    user_agent: str | None
    last_used_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────


@router.get("/vapid-public-key", response_model=dict)
async def get_vapid_public_key(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """Retourne la cle publique VAPID utilisee par le frontend pour subscribe."""
    if not settings.vapid_public_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "VAPID_PUBLIC_KEY non configure dans .env",
        )
    return {"public_key": settings.vapid_public_key}


@router.post("/subscribe", response_model=SubscribeResponse)
async def subscribe(
    payload: SubscribeRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SubscribeResponse:
    """Enregistre une nouvelle subscription Web Push."""
    user_agent = request.headers.get("user-agent", "")[:500]

    existing = (
        await db.execute(
            select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint)
        )
    ).scalar_one_or_none()

    if existing:
        existing.p256dh = payload.keys.p256dh
        existing.auth = payload.keys.auth
        existing.user_agent = user_agent
        if payload.label:
            existing.label = payload.label
        existing.revoked_at = None
        await db.commit()
        await db.refresh(existing)
        return SubscribeResponse(
            id=existing.id,
            endpoint=existing.endpoint,
            label=existing.label,
            status="updated",
        )

    sub = PushSubscription(
        endpoint=payload.endpoint,
        p256dh=payload.keys.p256dh,
        auth=payload.keys.auth,
        user_agent=user_agent,
        label=payload.label,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return SubscribeResponse(
        id=sub.id,
        endpoint=sub.endpoint,
        label=sub.label,
        status="created",
    )


@router.post("/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(
    payload: UnsubscribeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Marque une subscription comme revoked (soft-delete)."""
    sub = (
        await db.execute(
            select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint)
        )
    ).scalar_one_or_none()
    if sub is not None and sub.revoked_at is None:
        sub.revoked_at = datetime.now(UTC)
        await db.commit()


@router.get("/subscriptions", response_model=list[SubscriptionRead])
async def list_subscriptions(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[PushSubscription]:
    """Liste toutes les subscriptions (actives + revoked) pour debug/UI."""
    rows = (
        (await db.execute(select(PushSubscription).order_by(PushSubscription.created_at.desc())))
        .scalars()
        .all()
    )
    return list(rows)


@router.post("/send", response_model=SendNotificationResponse)
async def send_notification(
    payload: SendNotificationRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SendNotificationResponse:
    """Envoie une notif Web Push a toutes les subscriptions actives.

    Body de la notif (cote service worker frontend) :
        { title, body, url, icon, tag, requireInteraction }
    """
    if not settings.vapid_private_key_pem:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "VAPID_PRIVATE_KEY_PEM non configure dans .env",
        )

    # Charge les subs actives
    subs = (
        (await db.execute(select(PushSubscription).where(PushSubscription.revoked_at.is_(None))))
        .scalars()
        .all()
    )

    if not subs:
        return SendNotificationResponse(sent=0, failed=0, revoked=0, total_subs=0)

    # Payload JSON pour le service worker frontend
    notif_data = {
        "title": payload.title,
        "body": payload.body,
        "url": payload.url or "/",
        "icon": payload.icon or "/favicon.ico",
        "tag": payload.tag,
        "requireInteraction": payload.require_interaction,
    }
    notif_json = json.dumps(notif_data, ensure_ascii=False)

    # pywebpush import lazy (lourd)
    from pywebpush import WebPushException, webpush  # type: ignore[import-untyped]

    sent = failed = revoked = 0
    vapid_claims = {"sub": f"mailto:{settings.vapid_claim_email}"}

    # Normalise le PEM (le .env peut avoir des \n litteraux non interpretes)
    private_key = settings.vapid_private_key_pem.replace("\\n", "\n")

    for sub in subs:
        sub_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=notif_json,
                vapid_private_key=private_key,
                vapid_claims=vapid_claims.copy(),  # webpush mute le dict
            )
            sub.last_used_at = datetime.now(UTC)
            sent += 1
        except WebPushException as exc:
            resp = exc.response
            code = resp.status_code if resp is not None else 0
            if code in (404, 410):
                # endpoint mort/expire -> revoke
                sub.revoked_at = datetime.now(UTC)
                revoked += 1
                logger.warning("push_revoked: status=%d endpoint=%s", code, sub.endpoint[:60])
            else:
                failed += 1
                logger.error("push_failed: status=%d error=%s", code, str(exc)[:200])
        except Exception as exc:
            failed += 1
            logger.error("push_unexpected_error: %s", exc)

    await db.commit()
    return SendNotificationResponse(
        sent=sent,
        failed=failed,
        revoked=revoked,
        total_subs=len(subs),
    )
