"""drop_dup_indexes_add_gin

Revision ID: k7f8g9h0i1j2
Revises: j6e7f8g9h0i1
Create Date: 2026-05-08 11:30:00.000000

Nettoyage perf DB :

1. Drop des doublons d'index sur `start_time` :
   - location_activities : `ix_location_activity_start` (doublon de
     `ix_location_activities_start_time`)
   - location_visits : `ix_location_visit_start` (doublon de
     `ix_location_visits_start_time`)
   Les versions `op.f(...)` sont conservees (convention Alembic).

2. Ajout d'index GIN Postgres sur les colonnes ARRAY[text] frequemment filtrees :
   - emails.labels : `'INBOX' = ANY(labels)` ou `labels @> ARRAY[...]`
   - calendar_events.attendees : recherche par email d'invite

Postgres-only pour les GIN. SQLite no-op (les colonnes y sont JSON, pas array).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "k7f8g9h0i1j2"
down_revision: str | None = "j6e7f8g9h0i1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Drop doublons (idempotent via IF EXISTS sur Postgres / try-skip sur SQLite)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_location_activity_start")
        op.execute("DROP INDEX IF EXISTS ix_location_visit_start")
    else:
        # SQLite : DROP INDEX IF EXISTS supporte
        op.execute("DROP INDEX IF EXISTS ix_location_activity_start")
        op.execute("DROP INDEX IF EXISTS ix_location_visit_start")

    # 2. GIN indexes (Postgres uniquement : array/JSONB)
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_emails_labels_gin "
            "ON emails USING gin (labels)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_calendar_events_attendees_gin "
            "ON calendar_events USING gin (attendees)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_calendar_events_attendees_gin")
        op.execute("DROP INDEX IF EXISTS ix_emails_labels_gin")

    # Recree les doublons supprimes (rollback fidele)
    op.create_index(
        "ix_location_activity_start",
        "location_activities",
        ["start_time"],
        unique=False,
    )
    op.create_index(
        "ix_location_visit_start",
        "location_visits",
        ["start_time"],
        unique=False,
    )
