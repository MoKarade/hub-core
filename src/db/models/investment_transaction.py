"""Modele InvestmentTransaction : transactions des comptes d'investissement Disnat.

Couvre :
- Transferts (TRANSFERT RECU, TRANSFERT EMIS)
- Achats / ventes
- Depots / retraits
- Frais
- Dividendes / interets

Distincts des transactions bancaires car la semantique est differente :
- 2 dates (date de transaction + date de reglement)
- Quantite + symbole quand applicable
- Devise du titre potentiellement != devise du compte
- Prix unitaire eventuel

Sprint 4 : on stocke ces transactions telles que listees dans la section
"Activite mensuelle" du PDF Disnat.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


class InvestmentTransaction(Base):
    __tablename__ = "investment_transactions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        index=True,
    )

    # Sous-compte (chez Disnat un client peut avoir A3 = CAD, B1 = USD, etc.)
    sub_account_code: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    """Ex: '5NFL7A3' (CAD) ou '5NFL7B1' (USD)."""

    # Dates
    transaction_date: Mapped[date] = mapped_column(index=True)
    """Date d'execution de l'ordre."""

    settlement_date: Mapped[date | None] = mapped_column(nullable=True)
    """Date de reglement (peut etre absente)."""

    # Type d'operation tel qu'indique par Disnat
    operation: Mapped[str] = mapped_column(String(60))
    """'TRANSFERT RECU', 'ACHAT', 'VENTE', 'FRAIS', 'DIVIDENDE', 'DEPOT RECU D''UNE CAISSE', etc."""

    description: Mapped[str] = mapped_column(Text)
    """Libelle complet (nom du titre + 'TRSF IN' / 'EUROCLEAR/INTL' / etc.)."""

    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    """Symbole boursier si applicable (ex: 'NVDA', 'AVGO', 'SAF'). NULL pour transferts/frais."""

    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    """Nombre de titres (peut etre fractionnaire pour fonds)."""

    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    """Prix unitaire si fourni."""

    amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    """Montant total signe (positif = entree dans le compte, negatif = sortie).

    NULL pour les transferts de titres ou amount n'est pas pertinent.
    """

    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    """Devise du montant si specifiee. NULL = celle du sous-compte par defaut."""

    # Metadonnees source
    statement_date: Mapped[date] = mapped_column(index=True)
    """Date du releve dont vient la transaction."""

    source_format: Mapped[str] = mapped_column(String(50))
    """'desjardins_disnat_pdf'."""

    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)

    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    account: Mapped["Account"] = relationship(  # noqa: F821
        back_populates="investment_transactions",
    )

    def __repr__(self) -> str:
        return (
            f"<InvTxn {self.transaction_date} {self.operation} "
            f"{self.symbol or self.description[:20]!r} qty={self.quantity}>"
        )
