"""Modele YouTubeActivity : historique de visionnage / liked YouTube (Phase 6)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class YouTubeActivity(Base):
    __tablename__ = "youtube_activities"
    __table_args__ = (Index("ix_yt_user_published", "user_email", "published_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_email: Mapped[str] = mapped_column(String(255), index=True)
    activity_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)

    activity_type: Mapped[str] = mapped_column(String(50), index=True)
    """upload, like, favorite, subscription, etc."""

    video_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    video_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    channel_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
