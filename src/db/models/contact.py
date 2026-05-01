"""Modele Contact : Google People API (Phase 5)."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Date, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

ListType = ARRAY(Text).with_variant(JSON(), "sqlite")


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (Index("ix_contacts_user_name", "user_email", "display_name"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_email: Mapped[str] = mapped_column(String(255), index=True)
    person_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    """ID People API (resourceName, ex: 'people/c1234567890123')."""

    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    given_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    family_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    emails: Mapped[list[str]] = mapped_column(ListType, default=list)
    phones: Mapped[list[str]] = mapped_column(ListType, default=list)
    addresses: Mapped[list[str]] = mapped_column(ListType, default=list)
    organizations: Mapped[list[str]] = mapped_column(ListType, default=list)
    """Liste 'NomEntreprise — Titre' (humain-friendly)."""

    birthday: Mapped[date | None] = mapped_column(Date, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
