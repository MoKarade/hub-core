"""Endpoint /v1/contacts - ingest Google People API (Phase 5)."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Contact
from src.db.session import get_db
from src.services.oauth_google import get_valid_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/contacts", tags=["contacts"])

PEOPLE_API = "https://people.googleapis.com/v1"
PERSON_FIELDS = (
    "names,emailAddresses,phoneNumbers,addresses,organizations,"
    "birthdays,photos,biographies,metadata"
)


class ContactsSyncRequest(BaseModel):
    user_email: str = Field(default="marc.richard4@gmail.com")
    page_size: int = Field(default=1000, ge=1, le=2000)


class ContactsSyncResponse(BaseModel):
    ingested: int
    updated: int
    errors: int
    duration_seconds: float


class ContactItem(BaseModel):
    id: UUID
    person_id: str
    display_name: str | None
    given_name: str | None
    family_name: str | None
    emails: list[str]
    phones: list[str]
    organizations: list[str]
    birthday: date | None
    photo_url: str | None


async def _resolve_token(db: AsyncSession, user_email: str) -> str:
    for service in ("people", "all"):
        try:
            return await get_valid_access_token(db, service=service, user_email=user_email)
        except RuntimeError:
            continue
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Pas de token People pour {user_email}")


def _first_value(field: list[dict[str, Any]] | None, key: str = "value") -> str | None:
    if not field:
        return None
    primary = next((f for f in field if f.get("metadata", {}).get("primary")), None)
    item = primary or field[0]
    val = item.get(key)
    return str(val) if val else None


def _parse_birthday(birthdays: list[dict[str, Any]] | None) -> date | None:
    if not birthdays:
        return None
    for b in birthdays:
        d = b.get("date")
        if d and d.get("year") and d.get("month") and d.get("day"):
            try:
                return date(d["year"], d["month"], d["day"])
            except ValueError:
                continue
    return None


def _parse_person(p: dict[str, Any]) -> dict[str, Any] | None:
    rid = p.get("resourceName")
    if not rid:
        return None

    names = p.get("names") or []
    name = names[0] if names else {}
    display = name.get("displayName")
    given = name.get("givenName")
    family = name.get("familyName")

    emails = [e["value"] for e in (p.get("emailAddresses") or []) if e.get("value")]
    phones = [ph["value"] for ph in (p.get("phoneNumbers") or []) if ph.get("value")]
    addresses = [
        a.get("formattedValue", "") for a in (p.get("addresses") or []) if a.get("formattedValue")
    ]
    organizations = []
    for org in p.get("organizations") or []:
        org_name = org.get("name", "")
        org_title = org.get("title", "")
        if org_name and org_title:
            organizations.append(f"{org_name} — {org_title}")
        elif org_name:
            organizations.append(org_name)
        elif org_title:
            organizations.append(org_title)

    biographies = p.get("biographies") or []
    notes = biographies[0].get("value") if biographies else None

    photos = p.get("photos") or []
    photo_url = photos[0].get("url") if photos else None

    metadata = p.get("metadata") or {}
    sources = metadata.get("sources") or []
    last_modified = None
    for s in sources:
        upd = s.get("updateTime")
        if upd:
            try:
                last_modified = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                break
            except ValueError:
                continue

    return {
        "person_id": rid,
        "display_name": display,
        "given_name": given,
        "family_name": family,
        "emails": emails,
        "phones": phones,
        "addresses": addresses,
        "organizations": organizations,
        "birthday": _parse_birthday(p.get("birthdays")),
        "photo_url": photo_url,
        "notes": notes,
        "last_modified": last_modified,
    }


@router.post("/sync", response_model=ContactsSyncResponse)
async def sync_contacts(
    payload: ContactsSyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ContactsSyncResponse:
    start = time.monotonic()
    access_token = await _resolve_token(db, payload.user_email)

    ingested = 0
    updated = 0
    errors = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "personFields": PERSON_FIELDS,
                "pageSize": payload.page_size,
                "sortOrder": "LAST_MODIFIED_DESCENDING",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                r = await client.get(
                    f"{PEOPLE_API}/people/me/connections",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params=params,
                )
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Contacts list failed: {e.response.status_code} {e.response.text[:200]}",
                ) from e

            for p in data.get("connections", []):
                parsed = _parse_person(p)
                if not parsed:
                    errors += 1
                    continue
                existing = (
                    await db.execute(
                        select(Contact).where(Contact.person_id == parsed["person_id"])
                    )
                ).scalar_one_or_none()
                if existing:
                    for k, v in parsed.items():
                        setattr(existing, k, v)
                    updated += 1
                else:
                    db.add(Contact(user_email=payload.user_email, **parsed))
                    ingested += 1

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        await db.commit()

    return ContactsSyncResponse(
        ingested=ingested,
        updated=updated,
        errors=errors,
        duration_seconds=round(time.monotonic() - start, 2),
    )


@router.get("", response_model=list[ContactItem])
async def list_contacts(
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ContactItem]:
    stmt = select(Contact).order_by(Contact.display_name)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Contact.display_name.ilike(like)))
    stmt = stmt.limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        ContactItem(
            id=c.id,
            person_id=c.person_id,
            display_name=c.display_name,
            given_name=c.given_name,
            family_name=c.family_name,
            emails=c.emails or [],
            phones=c.phones or [],
            organizations=c.organizations or [],
            birthday=c.birthday,
            photo_url=c.photo_url,
        )
        for c in rows
    ]


class ContactsStats(BaseModel):
    total: int
    with_email: int
    with_phone: int
    with_organization: int


@router.get("/stats", response_model=ContactsStats)
async def contacts_stats(db: Annotated[AsyncSession, Depends(get_db)]) -> ContactsStats:
    total = (await db.execute(select(func.count(Contact.id)))).scalar() or 0
    # SQLite : json_array_length('[]') = 0, on filtre par notNull + non-vide via JSON
    rows = (await db.execute(select(Contact.emails, Contact.phones, Contact.organizations))).all()
    with_email = sum(1 for r in rows if r[0] and len(r[0]) > 0)
    with_phone = sum(1 for r in rows if r[1] and len(r[1]) > 0)
    with_organization = sum(1 for r in rows if r[2] and len(r[2]) > 0)
    return ContactsStats(
        total=total,
        with_email=with_email,
        with_phone=with_phone,
        with_organization=with_organization,
    )
