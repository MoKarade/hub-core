"""Modele Account : un compte bancaire/financier de Marc.

Couvre tous les types de comptes : courant, epargne, carte de credit,
investissement. Le champ `account_type` differencie.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Identite du compte
    institution: Mapped[str] = mapped_column(String(100))
    """Nom de l'institution (ex: 'Desjardins')."""

    account_type: Mapped[str] = mapped_column(String(50))
    """Type de compte : 'checking', 'savings', 'credit_card', 'investment'."""

    account_number_masked: Mapped[str] = mapped_column(String(100))
    """Numero de compte masque (ex: '377646-EOP', '5598 22** **** 5004', '5NFL7A3')."""

    nickname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Surnom donne par Marc (ex: 'Mon courant', 'Carte Mastercard')."""

    currency: Mapped[str] = mapped_column(String(3))
    """Devise principale du compte (ex: 'CAD', 'USD')."""

    is_active: Mapped[bool] = mapped_column(default=True)
    """False si le compte est ferme/archive."""

    # Timestamps (timezone-aware UTC)
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

    # Relations
    transactions: Mapped[list["Transaction"]] = relationship(  # noqa: F821
        back_populates="account",
        cascade="all, delete-orphan",
    )
    credit_card_transactions: Mapped[list["CreditCardTransaction"]] = relationship(  # noqa: F821
        back_populates="account",
        cascade="all, delete-orphan",
    )
    investment_transactions: Mapped[list["InvestmentTransaction"]] = relationship(  # noqa: F821
        back_populates="account",
        cascade="all, delete-orphan",
    )
    investment_positions: Mapped[list["InvestmentPosition"]] = relationship(  # noqa: F821
        back_populates="account",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Account {self.institution}/{self.account_type}/{self.account_number_masked}>"
