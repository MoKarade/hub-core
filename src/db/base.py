"""Base SQLAlchemy partagee par tous les modeles."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base declarative commune a tous les modeles SQLAlchemy."""
