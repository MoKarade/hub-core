"""restore_oauth_tokens_indexes

Revision ID: d1f2e3a4b5c6
Revises: c60cb4eb8b9f
Create Date: 2026-05-02 03:30:00.000000

Re-cree les indexes ix_oauth_tokens_provider et ix_oauth_tokens_service qui
ont ete supprimes par la migration c60cb4eb8b9f a cause d'une desync entre
le modele OAuthToken (sans index=True) et la migration originale a1b2c3d4e5f6
(qui les creait).

Le modele a ete corrige pour declarer index=True sur provider et service.
Cette migration restaure l'etat coherent.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d1f2e3a4b5c6"
down_revision: str | None = "c60cb4eb8b9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_oauth_tokens_provider"),
        "oauth_tokens",
        ["provider"],
        unique=False,
    )
    op.create_index(
        op.f("ix_oauth_tokens_service"),
        "oauth_tokens",
        ["service"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_tokens_service"), table_name="oauth_tokens")
    op.drop_index(op.f("ix_oauth_tokens_provider"), table_name="oauth_tokens")
