"""Modele OAuthToken : tokens OAuth 2.0 pour les services externes (Google, etc.).

Tokens chiffres au repos via Fernet (dérivé du secret_key applicatif).
On ne stocke JAMAIS les tokens en clair dans la DB.

Un seul user (Marc), donc pas de user_id. Le champ `user_email` est uniquement
pour traçabilité (qui a authorisé) et permettre plusieurs comptes Google si besoin.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

# ARRAY(Text) sur Postgres, JSON sur SQLite (tests). Même API Python (list[str]).
ScopesType = ARRAY(Text).with_variant(JSON(), "sqlite")


class OAuthToken(Base):
    """Token OAuth 2.0 pour un service externe.

    Une ligne par (provider, service, user_email). Si Marc reconnecte le même
    service, on UPDATE (UPSERT par contrainte unique).
    """

    __tablename__ = "oauth_tokens"
    __table_args__ = (
        UniqueConstraint(
            "provider", "service", "user_email", name="uq_oauth_provider_service_user"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    # Identite du token
    provider: Mapped[str] = mapped_column(String(50), index=True)
    """OAuth provider : 'google', 'strava', 'github', etc."""

    service: Mapped[str] = mapped_column(String(50), index=True)
    """Service spécifique du provider : 'gmail', 'photos', 'drive', 'calendar',
    'fitness', 'people', 'tasks', 'youtube', ou 'all' si scope unifié."""

    user_email: Mapped[str] = mapped_column(String(255))
    """Email du compte qui a authorisé (ex: 'marc.richard4@gmail.com').
    Permet plusieurs comptes du même service si jamais Marc en ajoute un."""

    # Tokens (CHIFFRES via Fernet, jamais en clair)
    access_token_encrypted: Mapped[bytes] = mapped_column()
    """Access token chiffré (Fernet). Court (1h typiquement)."""

    refresh_token_encrypted: Mapped[bytes | None] = mapped_column(nullable=True)
    """Refresh token chiffré. Optionnel : Google ne le donne qu'au 1er consent
    avec offline access. Si None : faut re-consent quand access expire."""

    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """Quand l'access token expire (UTC). Refresh nécessaire avant."""

    # Scopes accordés (ARRAY Postgres / JSON SQLite tests)
    scopes: Mapped[list[str]] = mapped_column(ScopesType, default=list)
    """Liste des scopes effectivement accordés (peut différer de ce qu'on a demandé)."""

    # Metadata
    token_type: Mapped[str] = mapped_column(String(20), default="Bearer")
    """Type du token (Bearer pour Google)."""

    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Dernière fois que le refresh a été utilisé (pour traçabilité/debug)."""

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Si non-null : token révoqué, ne pas l'utiliser (mais garder pour audit)."""

    # Timestamps
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

    @property
    def is_expired(self) -> bool:
        """True si l'access token est expiré (à comparer avec datetime.now(UTC)).

        SQLite stocke les datetime en naive (perd la timezone) — on assume UTC
        si la datetime est naive. Postgres avec DateTime(timezone=True) garde
        l'info correctement.
        """
        expires = self.token_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return datetime.now(UTC) >= expires

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_usable(self) -> bool:
        """True si le token peut être utilisé (non révoqué + access valide ou refresh dispo)."""
        if self.is_revoked:
            return False
        if not self.is_expired:
            return True
        return self.refresh_token_encrypted is not None

    def __repr__(self) -> str:
        return (
            f"<OAuthToken {self.provider}/{self.service}/{self.user_email} "
            f"expires={self.token_expires_at}>"
        )
