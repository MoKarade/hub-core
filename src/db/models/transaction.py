"""Modele Transaction : une transaction bancaire (compte courant ou epargne).

Sprint 1 : couvre uniquement les transactions debit/credit/solde des comptes
courants (EOP) et epargne (ET1) de Desjardins. Les transactions de carte de
credit et d'investissement auront leurs propres modeles (Sprint 3 et 4).

Format inspire des CSV Desjardins : on conserve les metadonnees brutes (transit,
seq_num) pour pouvoir tracer chaque transaction a sa source d'origine.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        index=True,
    )

    # Donnees principales de la transaction
    transaction_date: Mapped[date] = mapped_column(index=True)
    """Date de la transaction (telle qu'inscrite par la banque)."""

    description: Mapped[str] = mapped_column(Text)
    """Libelle brut de la transaction (ex: 'Paie /ROBOVIC INC.')."""

    debit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    """Montant debité (sortant). NULL si la transaction est un credit."""

    credit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    """Montant credité (entrant). NULL si la transaction est un debit."""

    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    """Solde du compte apres cette transaction (si fourni par la source)."""

    # Metadonnees source (pour audit + idempotence)
    source_format: Mapped[str] = mapped_column(String(50))
    """Identifiant du parser source (ex: 'desjardins_csv_eop', 'desjardins_csv_et1')."""

    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Nom du fichier d'origine (ex: 'janv2026.csv'). Pour audit."""

    source_seq_num: Mapped[int | None] = mapped_column(nullable=True)
    """Numero de sequence Desjardins (00001-00021 dans le mois). NULL si non applicable."""

    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    """SHA-256 de la transaction normalisee. Empeche les doublons sur ré-import."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    # Relations
    account: Mapped["Account"] = relationship(  # noqa: F821
        back_populates="transactions",
    )

    def __repr__(self) -> str:
        amount = self.debit or self.credit or 0
        sign = "-" if self.debit else "+"
        return f"<Transaction {self.transaction_date} {sign}{amount} {self.description[:40]!r}>"
