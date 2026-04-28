"""Modele InvestmentPosition : snapshot mensuel des positions d'un compte Disnat.

Chaque ligne de la section "Details de vos actifs" du releve Disnat devient
une position. La cle unique : (account_id, sub_account_code, statement_date,
description). Re-importer le meme PDF ne cree pas de doublons.

Permet de tracer la valeur du portefeuille mois par mois et l'evolution des
positions individuelles.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


class InvestmentPosition(Base):
    __tablename__ = "investment_positions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        index=True,
    )

    sub_account_code: Mapped[str] = mapped_column(String(20), index=True)
    """Ex: '5NFL7A3' (CAD) ou '5NFL7B1' (USD)."""

    statement_date: Mapped[date] = mapped_column(index=True)
    """Date de fin de mois du releve. Permet l'historique."""

    # Identite du titre
    description: Mapped[str] = mapped_column(Text)
    """Nom complet (ex: 'NVIDIA CORP', 'AMUNDI MSCI EM ASIA UCITS')."""

    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    """Ticker (ex: 'NVDA'). NULL si Disnat ne le fournit pas (cas Amundi sans symbole)."""

    # Position
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    average_unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    """Cout unitaire moyen historique (peut manquer si position recente)."""

    book_cost: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    """Cout comptable total = quantity * average_unit_cost."""

    market_price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    """Prix marche unitaire au snapshot."""

    market_value: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    """Valeur marchande totale = quantity * market_price (dans la devise du marche)."""

    currency: Mapped[str] = mapped_column(String(3))
    """Devise du marche (USD, CAD)."""

    portfolio_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    """% du portefeuille total (donne par Disnat)."""

    # Metadonnees source
    source_format: Mapped[str] = mapped_column(String(50))
    """'desjardins_disnat_pdf'."""

    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)

    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    account: Mapped["Account"] = relationship(back_populates="investment_positions")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<InvPos {self.statement_date} {self.symbol or self.description[:20]!r} "
            f"qty={self.quantity} val={self.market_value} {self.currency}>"
        )
