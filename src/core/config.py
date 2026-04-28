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
    secret_key: str = Field(default="changeme", min_length=8)

    # CORS
    cors_allowed_origins: str = "http://localhost:3000"

    # Cloudflare Access
    cf_access_team_domain: str = ""
    cf_access_audience: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ALLOWED_ORIGINS (string CSV) en liste."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in ("prod", "production")


@lru_cache
def get_settings() -> Settings:
    """Renvoie les settings (cachés en mémoire)."""
    return Settings()
