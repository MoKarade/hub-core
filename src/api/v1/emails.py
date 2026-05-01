"""Endpoint /v1/emails - ingest et lecture des emails Gmail (Phase 3).

Workflow :
1. Marc a connecte Gmail via /v1/oauth/google/start?service=gmail (deja fait)
2. /v1/emails/sync (POST) : utilise le token OAuth pour pull les emails Gmail
   recents et les stocke en DB (idempotent par gmail_id).
3. /v1/emails (GET) : liste avec filtres (sender, since, q text search).
4. /v1/emails/stats (GET) : top expediteurs + count par mois.

API Gmail utilisee :
- users.messages.list : retourne IDs (pagination via pageToken)
- users.messages.get : retourne 1 message complet (headers + payload MIME)

Rate limit : 1 quota unit par get, 5 par list. Daily quota = 1B units. OK.

Privacy : tout reste local. Body stocke en clair dans SQLite (peut etre chiffre
plus tard avec Fernet si Marc le souhaite).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Email
from src.db.session import get_db
from src.services.oauth_google import get_valid_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/emails", tags=["emails"])

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------


class SyncRequest(BaseModel):
    user_email: str = Field(default="marc.richard4@gmail.com")
    max_results: int = Field(default=200, ge=1, le=2000)
    since_days: int | None = Field(default=30, ge=1, le=3650)
    """Si non-null : ne pull que les emails des N derniers jours (filtre 'q')."""


class SyncResponse(BaseModel):
    ingested: int
    updated: int
    errors: int
    duration_seconds: float


class EmailListItem(BaseModel):
    id: UUID
    gmail_id: str
    thread_id: str
    subject: str | None
    sender: str
    sender_email: str
    sent_at: datetime
    snippet: str | None
    labels: list[str]
    has_attachments: bool
    is_unread: bool
    size_estimate: int | None


class EmailDetail(EmailListItem):
    recipients: list[str]
    body_text: str | None
    body_html: str | None


class EmailStats(BaseModel):
    total: int
    unread: int
    with_attachments: int
    top_senders: list[dict[str, Any]]
    """Top 20 senders : [{sender_email, count, last_seen}]."""

    by_month: list[dict[str, Any]]
    """Counts par mois (12 derniers): [{month: '2026-04', count}]."""


# ---------------------------------------------------------------------
# Gmail API helpers
# ---------------------------------------------------------------------


async def _gmail_list(
    client: httpx.AsyncClient,
    access_token: str,
    *,
    q: str | None = None,
    page_token: str | None = None,
    max_results: int = 100,
) -> dict[str, Any]:
    """List messages avec filtre q optionnel (syntaxe Gmail search : 'after:2026/04/01')."""
    params: dict[str, Any] = {"maxResults": min(max_results, 500)}
    if q:
        params["q"] = q
    if page_token:
        params["pageToken"] = page_token
    r = await client.get(
        f"{GMAIL_API}/users/me/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


async def _gmail_get(
    client: httpx.AsyncClient, access_token: str, message_id: str
) -> dict[str, Any]:
    """Get 1 message complet (avec headers + body MIME)."""
    r = await client.get(
        f"{GMAIL_API}/users/me/messages/{message_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"format": "full"},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _decode_b64url(data: str) -> str:
    """Decode Gmail's base64url body. Handle padding."""
    if not data:
        return ""
    # Gmail uses base64url (- and _ instead of + /), no padding
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extrait body_text et body_html des MIME parts (recursif)."""
    body_text: str | None = None
    body_html: str | None = None

    def walk(part: dict[str, Any]) -> None:
        nonlocal body_text, body_html
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data:
            if mime == "text/plain" and body_text is None:
                body_text = _decode_b64url(data)
            elif mime == "text/html" and body_html is None:
                body_html = _decode_b64url(data)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return body_text, body_html


def _has_attachments(payload: dict[str, Any]) -> bool:
    """True si au moins 1 part avec filename non-vide (= attachment)."""

    def walk(part: dict[str, Any]) -> bool:
        if part.get("filename"):
            return True
        for child in part.get("parts", []) or []:
            if walk(child):
                return True
        return False

    return walk(payload)


def _header(headers: list[dict[str, str]], name: str) -> str | None:
    """Extract header value (case-insensitive)."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return None


def _parse_recipients(raw: str | None) -> list[str]:
    """Extract emails from raw 'To: ...' header (handle multiple, comma-separated)."""
    if not raw:
        return []
    # Split sur les virgules hors guillemets (rough mais OK pour la plupart des cas)
    parts = re.split(r",(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)", raw)
    emails = []
    for p in parts:
        _, addr = parseaddr(p.strip())
        if addr and "@" in addr:
            emails.append(addr.lower())
    return emails


def _parse_email_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Convertit un message Gmail API vers un dict pret pour insertion DB."""
    payload = msg.get("payload", {})
    headers = payload.get("headers", []) or []

    sender_raw = _header(headers, "From") or ""
    sender_name, sender_email = parseaddr(sender_raw)
    sender_display = sender_raw if sender_name or sender_email else sender_raw
    sender_email = (sender_email or "").lower()

    subject = _header(headers, "Subject")
    date_raw = _header(headers, "Date")

    sent_at: datetime
    if date_raw:
        try:
            sent_at = parsedate_to_datetime(date_raw)
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            # Fallback : internalDate (ms epoch)
            sent_at = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, tz=UTC)
    else:
        sent_at = datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, tz=UTC)

    recipients = _parse_recipients(_header(headers, "To"))
    recipients += _parse_recipients(_header(headers, "Cc"))

    body_text, body_html = _extract_body(payload)
    labels = msg.get("labelIds", []) or []

    return {
        "gmail_id": msg["id"],
        "thread_id": msg.get("threadId", msg["id"]),
        "subject": subject,
        "sender": sender_display,
        "sender_email": sender_email,
        "recipients": recipients,
        "sent_at": sent_at,
        "snippet": (msg.get("snippet") or "")[:500],
        "body_text": body_text,
        "body_html": body_html,
        "labels": labels,
        "has_attachments": _has_attachments(payload),
        "is_unread": "UNREAD" in labels,
        "size_estimate": msg.get("sizeEstimate"),
    }


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------


async def _resolve_token(db: AsyncSession, user_email: str) -> str:
    """Tente d'obtenir un access_token valide pour Gmail.

    Cherche d'abord le token avec service='gmail', sinon fallback 'all' (consent
    unifie qui couvre tous les scopes Google).
    """
    for service in ("gmail", "all"):
        try:
            return await get_valid_access_token(db, service=service, user_email=user_email)
        except RuntimeError:
            continue
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        f"Pas de token OAuth Gmail valide pour {user_email}. "
        "Connecte Gmail (ou 'Tous les services Google') depuis /settings.",
    )


@router.post("/sync", response_model=SyncResponse)
async def sync_emails(
    payload: SyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SyncResponse:
    """Pull les emails Gmail recents et les stocke en DB (idempotent)."""
    import time

    start = time.monotonic()

    access_token = await _resolve_token(db, payload.user_email)

    # Filtre q : "after:YYYY/MM/DD" si since_days fourni
    q: str | None = None
    if payload.since_days:
        cutoff = (datetime.now(UTC) - timedelta(days=payload.since_days)).strftime("%Y/%m/%d")
        q = f"after:{cutoff}"

    # 1. List les IDs (peut paginer)
    ids: list[str] = []
    page_token: str | None = None
    async with httpx.AsyncClient() as client:
        while len(ids) < payload.max_results:
            try:
                data = await _gmail_list(
                    client,
                    access_token,
                    q=q,
                    page_token=page_token,
                    max_results=min(500, payload.max_results - len(ids)),
                )
            except httpx.HTTPStatusError as e:
                logger.error(
                    "gmail_list_failed: status=%d body=%s",
                    e.response.status_code,
                    e.response.text[:300],
                )
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Gmail list a echoue: {e.response.status_code}",
                ) from e
            for m in data.get("messages", []):
                ids.append(m["id"])
                if len(ids) >= payload.max_results:
                    break
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        if not ids:
            return SyncResponse(
                ingested=0, updated=0, errors=0, duration_seconds=round(time.monotonic() - start, 2)
            )

        # 2. Get chaque message en parallele (concurrency limitee)
        sem = asyncio.Semaphore(10)

        async def fetch_one(mid: str) -> dict[str, Any] | None:
            async with sem:
                try:
                    return await _gmail_get(client, access_token, mid)
                except httpx.HTTPStatusError as e:
                    logger.warning("gmail_get_failed: id=%s status=%d", mid, e.response.status_code)
                    return None
                except Exception as e:
                    logger.warning("gmail_get_exception: id=%s err=%r", mid, e)
                    return None

        results = await asyncio.gather(*[fetch_one(mid) for mid in ids])

    # 3. Upsert chaque message en DB
    ingested = 0
    updated = 0
    errors = 0
    for msg in results:
        if not msg:
            errors += 1
            continue
        try:
            parsed = _parse_email_message(msg)
        except Exception as e:
            logger.warning("parse_failed: id=%s err=%r", msg.get("id"), e)
            errors += 1
            continue

        existing = (
            await db.execute(select(Email).where(Email.gmail_id == parsed["gmail_id"]))
        ).scalar_one_or_none()

        if existing:
            for k, v in parsed.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(Email(user_email=payload.user_email, **parsed))
            ingested += 1

    await db.commit()

    return SyncResponse(
        ingested=ingested,
        updated=updated,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("", response_model=list[EmailListItem])
async def list_emails(
    db: Annotated[AsyncSession, Depends(get_db)],
    sender_email: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    q: Annotated[str | None, Query(description="Recherche dans subject + snippet")] = None,
    label: Annotated[str | None, Query()] = None,
    is_unread: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EmailListItem]:
    stmt = select(Email).order_by(desc(Email.sent_at))
    if sender_email:
        stmt = stmt.where(Email.sender_email == sender_email.lower())
    if since:
        stmt = stmt.where(Email.sent_at >= since)
    if until:
        stmt = stmt.where(Email.sent_at <= until)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Email.subject.ilike(like), Email.snippet.ilike(like)))
    if is_unread is not None:
        stmt = stmt.where(Email.is_unread == is_unread)
    # label : check si label dans la liste (Postgres ANY ou JSON contains pour SQLite)
    # Pour MVP : filtrage applicatif post-query si label fourni
    stmt = stmt.limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    items = [
        EmailListItem(
            id=r.id,
            gmail_id=r.gmail_id,
            thread_id=r.thread_id,
            subject=r.subject,
            sender=r.sender,
            sender_email=r.sender_email,
            sent_at=r.sent_at,
            snippet=r.snippet,
            labels=r.labels or [],
            has_attachments=r.has_attachments,
            is_unread=r.is_unread,
            size_estimate=r.size_estimate,
        )
        for r in rows
    ]
    if label:
        items = [i for i in items if label in i.labels]
    return items


@router.get("/stats", response_model=EmailStats)
async def email_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EmailStats:
    """Statistiques agregees pour le dashboard."""
    total = (await db.execute(select(func.count(Email.id)))).scalar() or 0
    unread = (
        await db.execute(select(func.count(Email.id)).where(Email.is_unread.is_(True)))
    ).scalar() or 0
    with_att = (
        await db.execute(select(func.count(Email.id)).where(Email.has_attachments.is_(True)))
    ).scalar() or 0

    # Top 20 senders
    top_q = (
        select(
            Email.sender_email,
            func.count(Email.id).label("count"),
            func.max(Email.sent_at).label("last_seen"),
        )
        .group_by(Email.sender_email)
        .order_by(desc("count"))
        .limit(20)
    )
    top_rows = (await db.execute(top_q)).all()
    top_senders = [
        {"sender_email": r[0], "count": int(r[1]), "last_seen": r[2].isoformat() if r[2] else None}
        for r in top_rows
    ]

    # Counts par mois (12 derniers mois)
    cutoff = datetime.now(UTC) - timedelta(days=365)
    by_month_q = (
        select(
            func.strftime("%Y-%m", Email.sent_at).label("month")
            if "sqlite" in str(db.bind.dialect.name)
            else func.to_char(Email.sent_at, "YYYY-MM").label("month"),
            func.count(Email.id).label("count"),
        )
        .where(Email.sent_at >= cutoff)
        .group_by("month")
        .order_by("month")
    )
    by_month_rows = (await db.execute(by_month_q)).all()
    by_month = [{"month": r[0], "count": int(r[1])} for r in by_month_rows if r[0]]

    return EmailStats(
        total=total,
        unread=unread,
        with_attachments=with_att,
        top_senders=top_senders,
        by_month=by_month,
    )


@router.get("/{email_id}", response_model=EmailDetail)
async def get_email(
    email_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EmailDetail:
    e = (await db.execute(select(Email).where(Email.id == email_id))).scalar_one_or_none()
    if not e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Email introuvable")
    return EmailDetail(
        id=e.id,
        gmail_id=e.gmail_id,
        thread_id=e.thread_id,
        subject=e.subject,
        sender=e.sender,
        sender_email=e.sender_email,
        recipients=e.recipients or [],
        sent_at=e.sent_at,
        snippet=e.snippet,
        body_text=e.body_text,
        body_html=e.body_html,
        labels=e.labels or [],
        has_attachments=e.has_attachments,
        is_unread=e.is_unread,
        size_estimate=e.size_estimate,
    )
