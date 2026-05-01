"""Modele HealthMetric : datapoints sante agreges par jour (Phase 4).

Modele unifie qui sert pour :
- Sleep (duree, deep/rem/light)
- Activity (steps, calories, distance, active minutes)
- Body (weight, heart rate)

Chaque ligne = 1 metric pour 1 jour. Sources multiples possibles
(google_fit, garmin, apple_health, manual).
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Date, DateTime, Float, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class HealthMetric(Base):
    """Une metric sante pour un jour donne (sommeil, pas, calories, etc.)."""

    __tablename__ = "health_metrics"
    __table_args__ = (
        UniqueConstraint(
            "user_email", "date", "metric", "source", name="uq_health_user_date_metric_source"
        ),
        Index("ix_health_user_date", "user_email", "date"),
        Index("ix_health_metric_date", "metric", "date"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_email: Mapped[str] = mapped_column(String(255), index=True)

    date: Mapped[date] = mapped_column(Date, index=True)
    """Jour de la mesure (UTC date, facilite les agregations)."""

    metric: Mapped[str] = mapped_column(String(50), index=True)
    """sleep_total_min, sleep_deep_min, sleep_rem_min, sleep_light_min,
    steps, calories, distance_m, active_minutes, weight_kg, heart_rate_avg, etc."""

    value: Mapped[float] = mapped_column(Float)
    """Valeur numerique (unite implicite par metric, cf. nom)."""

    source: Mapped[str] = mapped_column(String(50), default="google_fit")
    """google_fit, garmin, apple_health, manual."""

    raw_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """Metadata additionelle (raw response, segments details, etc.)."""

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
