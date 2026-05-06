"""Modele RemovalRequest : demandes de suppression / acces Loi 25 / PIPEDA / RGPD.

Tracker pour les demandes que Marc envoie aux entreprises pour exercer ses
droits sous Loi 25 (Quebec), PIPEDA (Canada) ou RGPD (UE).

Workflow type :
1. Marc identifie une entreprise qui detient ses donnees (broker, site web, etc.)
2. Cree une RemovalRequest (status=draft)
3. App genere l'email a partir d'un template (Loi25 / PIPEDA / RGPD)
4. Marc envoie l'email manuellement (ou via Gmail OAuth depuis l'app)
5. App passe la requete en status=sent + calcule deadline (+30 jours legal)
6. Reminder genere si pas de reponse a deadline
7. Marc marque resolu (acknowledged / data_deleted / refused / expired)

Pas de fake data : Marc renseigne lui-meme les entreprises ciblees.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class RemovalRequest(Base):
    """Une demande de suppression / acces envoyee a une entreprise."""

    __tablename__ = "removal_requests"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Entreprise ciblee
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    company_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    company_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """URL contact / privacy policy de l'entreprise (utile pour follow-up)."""

    # Type de demande : access (consultation) | deletion (suppression) | rectification
    request_type: Mapped[str] = mapped_column(String(20), nullable=False, default="deletion")

    # Loi invoquee : loi25 | pipeda | gdpr | other
    legal_basis: Mapped[str] = mapped_column(String(20), nullable=False, default="loi25")

    # Status : draft (brouillon, pas envoye) | sent | acknowledged | data_deleted
    #          | refused | expired (deadline passee sans reponse)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft", index=True)

    # Email genere (sujet + corps), pour qu'on puisse re-envoyer / inspecter
    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Notes libres (Marc peut annoter)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps cycle de vie
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Echeance legale (Loi 25 = 30j, PIPEDA = 30j, RGPD = 30j)."""
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<RemovalRequest {self.company_name} [{self.status}]>"
