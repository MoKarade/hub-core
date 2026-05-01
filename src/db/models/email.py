"""Modele Email : metadata d'un email Gmail (Phase 3).

On stocke les emails ingestes via Gmail API (read-only OAuth scope) :
- Headers (sender, recipients, subject, date)
- Body texte (extrait des MIME parts)
- Labels Gmail (Inbox, Sent, custom labels)
- Snippet (pour preview rapide)

Idempotence par gmail_id (l'ID unique alloue par Gmail). Si on resync,
on UPSERT (update si existe deja).

Privacy : tout reste local, jamais expose. Le body peut etre chiffre
ulterieurement si Marc le souhaite (Fernet) - pour MVP on stocke en clair.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

# ARRAY(Text) sur Postgres, JSON sur SQLite. API Python identique (list[str]).
LabelsType = ARRAY(Text).with_variant(JSON(), "sqlite")
RecipientsType = ARRAY(Text).with_variant(JSON(), "sqlite")


class Email(Base):
    """Email Gmail ingere via API (read-only)."""

    __tablename__ = "emails"
    __table_args__ = (
        Index("ix_emails_user_email_sent_at", "user_email", "sent_at"),
        Index("ix_emails_sender_email_sent_at", "sender_email", "sent_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Identite
    user_email: Mapped[str] = mapped_column(String(255), index=True)
    """Email du proprietaire (Marc). Permet multi-comptes plus tard."""

    gmail_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    """ID Gmail unique (pour idempotence resync)."""

    thread_id: Mapped[str] = mapped_column(String(50), index=True)
    """ID du thread Gmail (groupement de la conversation)."""

    # Headers
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender: Mapped[str] = mapped_column(Text)
    """Display name + email, ex: 'Hydro Quebec <noreply@hydroquebec.com>'."""

    sender_email: Mapped[str] = mapped_column(String(255), index=True)
    """Juste l'email extrait du sender (pour filter rapide)."""

    recipients: Mapped[list[str]] = mapped_column(RecipientsType, default=list)
    """Liste des emails destinataires (To + Cc, sans Bcc qu'on voit pas)."""

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    """Quand l'email a ete envoye (header Date)."""

    # Contenu
    snippet: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Preview ~200 chars (fourni par Gmail API)."""

    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Body en texte brut (extrait des MIME parts text/plain)."""

    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Body en HTML (text/html part). Optionnel - peut etre lourd."""

    # Metadata Gmail
    labels: Mapped[list[str]] = mapped_column(LabelsType, default=list)
    """Labels Gmail : INBOX, SENT, IMPORTANT, custom labels..."""

    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    is_unread: Mapped[bool] = mapped_column(Boolean, default=False)

    size_estimate: Mapped[int | None] = mapped_column(nullable=True)
    """Taille estimee en bytes (Gmail API)."""

    # Timestamps internes
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<Email gmail_id={self.gmail_id} from={self.sender_email!r} "
            f"subject={self.subject!r} sent={self.sent_at}>"
        )
