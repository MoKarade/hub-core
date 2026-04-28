"""Modeles SQLAlchemy du hub.

IMPORTANT : tous les modeles doivent etre importes ici pour qu'Alembic les
detecte au moment du `revision --autogenerate`.
"""

from src.db.base import Base
from src.db.models.account import Account
from src.db.models.credit_card_transaction import CreditCardTransaction
from src.db.models.investment_position import InvestmentPosition
from src.db.models.investment_transaction import InvestmentTransaction
from src.db.models.location_point import LocationPoint
from src.db.models.transaction import Transaction

__all__ = [
    "Base",
    "Account",
    "Transaction",
    "CreditCardTransaction",
    "InvestmentTransaction",
    "InvestmentPosition",
    "LocationPoint",
]
