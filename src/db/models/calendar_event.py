"""Modele CalendarEvent : evenements Google Calendar (Phase 3)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

AttendeesType = ARRAY(Text).with_variant(JSON(), "sqlite")


class CalendarEvent(Base):
    """Evenement Google Calendar."""

    __tablename__ = "calendar_events"
    __table_args__ = (Index("ix_calevt_user_start", "user_email", "start_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_email: Mapped[str] = mapped_column(String(255), index=True)
    gcal_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    """ID unique de l'evenement Gcal (idempotence)."""

    calendar_id: Mapped[str] = mapped_column(String(200), index=True)
    """ID du calendrier (ex: marc.richard4@gmail.com pour le primary)."""

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    """True si event sur la journee entiere (pas d'heure)."""

    organizer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attendees: Mapped[list[str]] = mapped_column(AttendeesType, default=list)
    """Liste des emails participants."""

    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    """confirmed, tentative, cancelled."""

    html_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Lien direct Google Calendar pour ouvrir l'event."""

    recurring_event_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    """Si event recurrent, l'ID du parent. NULL si one-off."""

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
