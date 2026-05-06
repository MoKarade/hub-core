"""Endpoints privacy : tracker des demandes Loi 25 / PIPEDA / RGPD.

CRUD pour les RemovalRequest + helpers :
- Generation auto de l'email a partir d'un template selon legal_basis
- Marquer une requete comme envoyee + calcul auto de la deadline (+30 jours)
- Reminders : retourner les requetes en retard

Pas d'envoi d'email automatique : Marc copie/colle le texte ou l'envoie via
Gmail OAuth lui-meme. La reglementation exige une signature personnelle, donc
on facilite mais on automatise pas l'envoi.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.removal_request import RemovalRequest
from src.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/privacy", tags=["privacy"])


# ─────────────────────────────────────────────────────────────────────────
# Templates
# ─────────────────────────────────────────────────────────────────────────


REQUEST_TYPES = {"access", "deletion", "rectification"}
LEGAL_BASES = {"loi25", "pipeda", "gdpr", "other"}
STATUSES = {
    "draft",
    "sent",
    "acknowledged",
    "data_deleted",
    "refused",
    "expired",
}

LEGAL_BASIS_LABEL = {
    "loi25": "Loi 25 (QC)",
    "pipeda": "PIPEDA (CA)",
    "gdpr": "RGPD (UE)",
    "other": "Privacy law",
}

REQUEST_TYPE_FR = {
    "access": "consultation de mes donnees personnelles",
    "deletion": "suppression de toutes mes donnees personnelles",
    "rectification": "rectification de mes donnees personnelles",
}

# Template francais, signe a la main par Marc apres copie
EMAIL_TEMPLATE = """Objet : Demande {legal_label} - {request_type_fr}

Madame, Monsieur,

En vertu de la {legal_label_long}, je vous demande formellement \
la {request_type_fr_full} que votre organisation detient a mon sujet.

Mes informations d'identification :
- Nom complet : Marc Richard
- Courriel(s) potentiellement enregistres : marc.richard4@gmail.com{extra_emails}

Conformement a la loi, vous disposez de 30 jours pour repondre a cette demande, \
faute de quoi je porterai plainte aupres de la Commission d'acces a l'information \
du Quebec (ou du Commissariat a la protection de la vie privee du Canada).

Merci de me confirmer la reception de cette demande, et de m'envoyer :
{request_specifics}

Cordialement,
Marc Richard
"""

REQUEST_SPECIFICS = {
    "access": (
        "- La liste exhaustive des donnees personnelles que vous detenez sur moi\n"
        "- L'origine de ces donnees (collecte directe ou source tierce)\n"
        "- Les categories de tiers a qui elles ont ete communiquees\n"
        "- La duree de conservation prevue\n"
        "- Une copie de toutes ces donnees dans un format lisible"
    ),
    "deletion": (
        "- La confirmation ecrite que toutes mes donnees ont ete supprimees\n"
        "- La liste des sous-traitants / tiers a qui vous communiquerez egalement la demande\n"
        "- La date effective de la suppression"
    ),
    "rectification": (
        "- La confirmation ecrite des corrections effectuees\n"
        "- La liste des donnees rectifiees avec leurs nouvelles valeurs\n"
        "- La date effective de la rectification"
    ),
}

LEGAL_FULL = {
    "loi25": "Loi modernisant des dispositions legislatives en matiere de protection des "
    "renseignements personnels (Loi 25 du Quebec)",
    "pipeda": "Loi sur la protection des renseignements personnels et les documents "
    "electroniques (PIPEDA, Canada)",
    "gdpr": "Reglement general sur la protection des donnees (RGPD, UE)",
    "other": "legislation applicable en matiere de protection des donnees personnelles",
}

REQUEST_TYPE_FULL = {
    "access": "consultation complete de mes donnees personnelles",
    "deletion": "suppression definitive de toutes mes donnees personnelles",
    "rectification": "rectification de mes donnees personnelles inexactes",
}

DEADLINE_DAYS = 30


# ─────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────


class RemovalRequestCreate(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=200)
    company_email: str | None = Field(None, max_length=200)
    company_url: str | None = Field(None, max_length=500)
    request_type: str = Field("deletion", pattern="^(access|deletion|rectification)$")
    legal_basis: str = Field("loi25", pattern="^(loi25|pipeda|gdpr|other)$")
    notes: str | None = None
    extra_emails: list[str] | None = None  # autres emails a inclure dans le template


class RemovalRequestUpdate(BaseModel):
    status: str | None = Field(
        None, pattern="^(draft|sent|acknowledged|data_deleted|refused|expired)$"
    )
    company_email: str | None = None
    company_url: str | None = None
    notes: str | None = None
    subject: str | None = None
    body: str | None = None


class RemovalRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_name: str
    company_email: str | None
    company_url: str | None
    request_type: str
    legal_basis: str
    status: str
    subject: str | None
    body: str | None
    notes: str | None
    created_at: datetime
    sent_at: datetime | None
    deadline_at: datetime | None
    resolved_at: datetime | None
    days_until_deadline: int | None = None  # calcule a la volee


class RemovalSummary(BaseModel):
    total: int
    draft: int
    sent: int
    overdue: int  # sent + deadline passee + pas resolved
    resolved: int
    refused: int


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _generate_email(req: RemovalRequest, extra_emails: list[str] | None = None) -> tuple[str, str]:
    """Genere (subject, body) a partir des metadonnees de la requete."""
    extras = ""
    if extra_emails:
        extras = "\n  + " + "\n  + ".join(extra_emails)

    body = EMAIL_TEMPLATE.format(
        legal_label=LEGAL_BASIS_LABEL.get(req.legal_basis, "Privacy"),
        legal_label_long=LEGAL_FULL.get(req.legal_basis, "law"),
        request_type_fr=REQUEST_TYPE_FR.get(req.request_type, req.request_type),
        request_type_fr_full=REQUEST_TYPE_FULL.get(req.request_type, req.request_type),
        extra_emails=extras,
        request_specifics=REQUEST_SPECIFICS.get(req.request_type, ""),
    )
    subject = (
        f"Demande {LEGAL_BASIS_LABEL.get(req.legal_basis, 'Privacy')} "
        f"- {REQUEST_TYPE_FR.get(req.request_type, req.request_type)}"
    )
    return subject, body


def _to_out(req: RemovalRequest) -> RemovalRequestOut:
    out = RemovalRequestOut.model_validate(req)
    if req.deadline_at and req.status == "sent":
        delta = req.deadline_at - datetime.now(UTC)
        out.days_until_deadline = int(delta.total_seconds() // 86400)
    return out


# ─────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────


@router.post("/requests", response_model=RemovalRequestOut, status_code=status.HTTP_201_CREATED)
async def create_request(
    payload: RemovalRequestCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RemovalRequestOut:
    """Cree une nouvelle demande (status=draft) + genere le template d'email."""
    req = RemovalRequest(
        company_name=payload.company_name.strip(),
        company_email=payload.company_email,
        company_url=payload.company_url,
        request_type=payload.request_type,
        legal_basis=payload.legal_basis,
        notes=payload.notes,
        status="draft",
    )
    subject, body = _generate_email(req, payload.extra_emails)
    req.subject = subject
    req.body = body
    db.add(req)
    await db.commit()
    await db.refresh(req)
    logger.info("removal_request_created id=%s company=%s", req.id, req.company_name)
    return _to_out(req)


@router.get("/requests", response_model=list[RemovalRequestOut])
async def list_requests(
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: str | None = None,
    limit: int = 200,
) -> list[RemovalRequestOut]:
    """Liste des demandes, plus recentes en premier. Filtre optionnel par status."""
    stmt = select(RemovalRequest).order_by(desc(RemovalRequest.created_at)).limit(limit)
    if status_filter:
        if status_filter not in STATUSES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid status_filter")
        stmt = stmt.where(RemovalRequest.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/requests/{req_id}", response_model=RemovalRequestOut)
async def get_request(
    req_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RemovalRequestOut:
    req = await db.get(RemovalRequest, req_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Removal request not found")
    return _to_out(req)


@router.patch("/requests/{req_id}", response_model=RemovalRequestOut)
async def update_request(
    req_id: UUID,
    payload: RemovalRequestUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RemovalRequestOut:
    req = await db.get(RemovalRequest, req_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Removal request not found")

    # Si transition draft -> sent, marque sent_at + deadline
    if payload.status and payload.status != req.status:
        if payload.status == "sent" and req.status == "draft":
            req.sent_at = datetime.now(UTC)
            req.deadline_at = req.sent_at + timedelta(days=DEADLINE_DAYS)
        if payload.status in {"acknowledged", "data_deleted", "refused", "expired"}:
            req.resolved_at = datetime.now(UTC)
        req.status = payload.status

    if payload.company_email is not None:
        req.company_email = payload.company_email
    if payload.company_url is not None:
        req.company_url = payload.company_url
    if payload.notes is not None:
        req.notes = payload.notes
    if payload.subject is not None:
        req.subject = payload.subject
    if payload.body is not None:
        req.body = payload.body

    await db.commit()
    await db.refresh(req)
    return _to_out(req)


@router.delete("/requests/{req_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_request(
    req_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    req = await db.get(RemovalRequest, req_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Removal request not found")
    await db.delete(req)
    await db.commit()


@router.get("/summary", response_model=RemovalSummary)
async def removal_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RemovalSummary:
    """Compteurs par status + overdue (sent avec deadline passee non resolved)."""
    rows = (await db.execute(select(RemovalRequest))).scalars().all()
    now = datetime.now(UTC)
    summary = RemovalSummary(total=len(rows), draft=0, sent=0, overdue=0, resolved=0, refused=0)
    for r in rows:
        if r.status == "draft":
            summary.draft += 1
        elif r.status == "sent":
            summary.sent += 1
            if r.deadline_at:
                # Normalise pour comparer avec now (UTC-aware)
                deadline = (
                    r.deadline_at if r.deadline_at.tzinfo else r.deadline_at.replace(tzinfo=UTC)
                )
                if deadline < now:
                    summary.overdue += 1
        elif r.status in {"acknowledged", "data_deleted"}:
            summary.resolved += 1
        elif r.status == "refused":
            summary.refused += 1
    return summary


@router.get("/templates", response_model=dict)
async def get_templates() -> dict:
    """Retourne les templates et constantes pour le frontend (preview)."""
    return {
        "request_types": sorted(REQUEST_TYPES),
        "legal_bases": [{"value": k, "label": LEGAL_BASIS_LABEL[k]} for k in LEGAL_BASES],
        "statuses": sorted(STATUSES),
        "deadline_days": DEADLINE_DAYS,
    }
