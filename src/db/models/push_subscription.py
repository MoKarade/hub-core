"""Modele PushSubscription : abonnements Web Push (PWA notifications natives).

Quand Marc autorise les notifications dans l'app Next.js (PWA installee sur son
tel ou desktop), le browser genere une endpoint URL + 2 cles cryptographiques.
On stocke ces 3 valeurs ici pour pouvoir lui envoyer des notifs quand on veut
via pywebpush.

Une seule subscription par browser/device, mais Marc peut en avoir plusieurs
(tel + desktop + tablette) -> on stocke par endpoint unique.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class PushSubscription(Base):
    """Une subscription Web Push d'un device de Marc."""

    __tablename__ = "push_subscriptions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # endpoint URL fourni par le browser (FCM/APNS/Mozilla autopush selon)
    # Sert aussi de cle naturelle (UNIQUE) pour upsert si Marc re-subscribe
    endpoint: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)

    # Cles cryptographiques fournies par PushManager
    p256dh: Mapped[str] = mapped_column(String(255), nullable=False)
    auth: Mapped[str] = mapped_column(String(50), nullable=False)

    # Metadata user-agent pour identifier les devices
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    """Label optionnel saisi par Marc : 'Tel perso', 'PC bureau', etc."""

    # Soft-delete : si pywebpush retourne 404/410 -> on revoke au lieu de supprimer
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<PushSubscription {self.label or self.endpoint[:30]}...>"
