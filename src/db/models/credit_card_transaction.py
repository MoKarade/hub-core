"""Modele CreditCardTransaction : transactions des cartes de credit.

Separe de `Transaction` car la semantique est differente :
- 2 dates (transaction_date = au moment de l'achat ; posting_date = quand
  la transaction apparait au releve)
- Montant signe unique : positif = achat/debit, negatif = paiement/remboursement
- cashback_rate fourni par Desjardins permet une categorisation gratuite
- card_number_masked != account_number_masked (un compte peut avoir
  plusieurs cartes, principale + secondaire)

Couvre uniquement les transactions DECRITES dans le PDF (sections
"transactions courantes" + "operations au compte"). Les remises en argent
agregees ne sont pas stockees.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


class CreditCardTransaction(Base):
    __tablename__ = "credit_card_transactions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        index=True,
    )

    # Carte physique utilisee (un compte peut en avoir plusieurs).
    card_number_masked: Mapped[str] = mapped_column(String(50), index=True)
    """Ex: '5598 22** **** 5020' (carte principale de Marc)."""

    # Dates
    transaction_date: Mapped[date] = mapped_column(index=True)
    """Date a laquelle la transaction a ete effectuee chez le marchand."""

    posting_date: Mapped[date]
    """Date a laquelle Desjardins a inscrit la transaction au compte."""

    # Description (libelle complet, incluant ville/province colles)
    description: Mapped[str] = mapped_column(Text)

    # Montant signe : positif = achat/frais (debit pour le porteur),
    # negatif = paiement/remboursement/credit-remises (credit pour le porteur).
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    # Cashback rate eventuel (0.50%, 2.00%, etc.). NULL si non applicable
    # (paiements, remises directes).
    cashback_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)

    # Section du PDF d'ou vient la transaction.
    section: Mapped[str] = mapped_column(String(30))
    """'transactions_courantes', 'operations_au_compte', etc."""

    # Metadonnees source (audit + idempotence)
    source_format: Mapped[str] = mapped_column(String(50))
    """Ex: 'desjardins_mastercard_pdf'."""

    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Nom du PDF d'origine."""

    statement_date: Mapped[date | None] = mapped_column(nullable=True)
    """Date du releve (haut du PDF). Utile pour grouper les transactions par cycle."""

    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    # Relations
    account: Mapped["Account"] = relationship(back_populates="credit_card_transactions")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<CCTxn {self.transaction_date} {self.amount:+} "
            f"{self.description[:30]!r} card={self.card_number_masked[-4:]}>"
        )
