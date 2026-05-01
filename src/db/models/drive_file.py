"""Modele DriveFile : metadata Google Drive (Phase 3c)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class DriveFile(Base):
    __tablename__ = "drive_files"
    __table_args__ = (Index("ix_drive_user_modified", "user_email", "modified_time"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_email: Mapped[str] = mapped_column(String(255), index=True)
    drive_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    """ID Drive (idempotence)."""

    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str] = mapped_column(String(150), index=True)

    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """NULL pour les Google Docs/Sheets (pas de taille fichier)."""

    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    trashed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)

    owner_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    modified_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    web_view_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    """URL pour ouvrir le fichier dans Drive (permanent)."""

    parents: Mapped[str | None] = mapped_column(Text, nullable=True)
    """IDs des parents (folders), separes par virgules. NULL = root."""

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
