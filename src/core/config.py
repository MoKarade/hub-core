"""Application settings, lus depuis variables d'env via Pydantic."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings de l'application. Surchargés par les variables d'env / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = "hub-core"
    app_env: str = "dev"
    log_level: str = "INFO"

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://hub:hubpass@localhost:5432/hubdb",
        description="URL de connexion PostgreSQL (avec driver psycopg v3)",
    )

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:14b-instruct"
    ollama_embed_model: str = "nomic-embed-text"

    # Sécurité
    secret_key: str = Field(
        default="changeme",
        min_length=8,
        description=(
            "Secret pour Fernet (chiffrement OAuth tokens) + signature cookies. "
            "32+ chars en prod (sinon RuntimeError au startup, cf. validate_in_production)."
        ),
    )

    # CORS
    cors_allowed_origins: str = "http://localhost:3000"

    # Cloudflare Access
    cf_access_team_domain: str = ""
    cf_access_audience: str = ""

    # Google OAuth 2.0 (Phase 3+ : Gmail/Photos/Drive/Calendar/Fit/People/Tasks)
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/v1/oauth/callback"

    # Frontend URL (pour rediriger après OAuth callback)
    frontend_url: str = "http://localhost:3000"

    # Scheduler auto-sync (Phase 6) : intervals en minutes, 0 = job desactive
    scheduler_enabled: bool = True
    scheduler_emails_minutes: int = 15
    scheduler_calendar_minutes: int = 30
    scheduler_tasks_minutes: int = 30
    scheduler_drive_minutes: int = 360  # 6h
    scheduler_contacts_minutes: int = 720  # 12h
    scheduler_health_minutes: int = 60  # 1h (Google Fit)
    scheduler_news_minutes: int = 30  # Google News RSS
    scheduler_garmin_minutes: int = 360  # 6h (Garmin Connect via garth)
    scheduler_streaming_minutes: int = 720  # 12h (Trakt history scrape)
    scheduler_clip_embed_minutes: int = 30  # 30 min (batch 100 photos / run)
    scheduler_face_detect_minutes: int = 60  # 1h (batch 50 photos / run)

    # Trakt.tv OAuth (Phase 6 — streaming hub)
    # A creer sur https://trakt.tv/oauth/applications. Redirect_uri doit matcher exact.
    trakt_client_id: str = ""
    trakt_client_secret: str = ""
    trakt_redirect_uri: str = "https://hubperso.com/api/v1/streaming/oauth/callback"

    # Steam Web API (Phase 6 — gaming)
    # API key gratuite : https://steamcommunity.com/dev/apikey (besoin d'avoir un compte Steam)
    # SteamID64 : https://steamid.io/ ou via le profil Steam (URL contient /profiles/<id>)
    steam_api_key: str = ""
    steam_user_id: str = ""  # SteamID64 (17 chiffres)
    scheduler_steam_minutes: int = 360  # 6h - snapshots periodiques

    # News : URL RSS Google News (FR Quebec par defaut, modifiable via .env)
    news_rss_url: str = "https://news.google.com/rss?hl=fr-CA&gl=CA&ceid=CA%3Afr"

    # Email du propriétaire du hub — doit être défini dans .env (HUB_OWNER_EMAIL=...).
    # La validation en prod lève RuntimeError si vide ou invalide.
    hub_owner_email: str = ""

    # Web Push (Phase 6) : VAPID keys generees une fois par py_vapid
    vapid_public_key: str = ""
    vapid_private_key_pem: str = ""
    vapid_claim_email: str = ""  # hérité de hub_owner_email si vide (voir notifications.py)

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ALLOWED_ORIGINS (string CSV) en liste."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in ("prod", "production")

    def validate_for_production(self) -> None:
        """Vérifie que la conf est safe pour la prod. À appeler au startup.

        Lève RuntimeError si quelque chose de critique manque/insecure.
        """
        if not self.is_production:
            return
        if self.secret_key == "changeme" or self.secret_key.startswith("changeme"):
            raise RuntimeError(
                "SECRET_KEY est encore au défaut 'changeme'. "
                "Génère-en un avec: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )
        if len(self.secret_key) < 32:
            raise RuntimeError(
                f"SECRET_KEY trop court ({len(self.secret_key)} chars). 32+ recommandé en prod."
            )
        if self.google_oauth_client_secret and not self.google_oauth_client_id:
            raise RuntimeError("GOOGLE_OAUTH_CLIENT_SECRET défini sans CLIENT_ID")
        if not self.hub_owner_email or "@" not in self.hub_owner_email:
            raise RuntimeError(
                "HUB_OWNER_EMAIL manquant ou invalide. "
                "Définis-le dans .env : HUB_OWNER_EMAIL=ton@email.com"
            )


@lru_cache
def get_settings() -> Settings:
    """Renvoie les settings (cachés en mémoire)."""
    return Settings()
