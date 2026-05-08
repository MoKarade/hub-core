"""Personal Data Hub — entrée FastAPI."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.v1 import router as v1_router
from src.core.cf_access import CloudflareAccessMiddleware
from src.core.config import get_settings
from src.core.logging import logger, setup_logging
from src.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hooks au démarrage / arrêt du hub."""
    settings = get_settings()
    setup_logging(settings.log_level)
    # Refuse de démarrer en prod si la conf n'est pas sécurisée (ex: secret_key=changeme)
    settings.validate_for_production()
    # En prod, exige que Cloudflare Access soit configuré (sinon le hub est exposé sans auth).
    if settings.is_production and (
        not settings.cf_access_team_domain or not settings.cf_access_audience
    ):
        raise RuntimeError(
            "Production mode requires Cloudflare Access configured "
            "(cf_access_team_domain + cf_access_audience). "
            "Set them in .env or run with app_env=dev"
        )
    # En prod, refuse CORS '*' avec credentials (CSRF risk).
    if settings.is_production and settings.cors_allowed_origins.strip() == "*":
        raise RuntimeError(
            "Production mode forbids cors_allowed_origins='*' (CSRF risk avec credentials). "
            "Liste les origines explicitement dans .env."
        )
    logger.info("hub_startup", env=settings.app_env, app=settings.app_name)
    # Demarre le scheduler auto-sync (peut etre desactive via SCHEDULER_ENABLED=false)
    await start_scheduler(settings)
    yield
    await stop_scheduler()
    logger.info("hub_shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Personal Data Hub",
        description="Hub centralisé des données personnelles de Marc, avec IA locale.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    )

    # Cloudflare Access JWT validation (transparent en dev local : pas de team_domain).
    # Se déclenche uniquement quand le hub est exposé via Cloudflare Tunnel + Access.
    app.add_middleware(CloudflareAccessMiddleware)

    app.include_router(v1_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        info: dict[str, str] = {
            "name": "Personal Data Hub",
            "version": "0.1.0",
            "health": "/v1/health",
        }
        if not settings.is_production:
            info["docs"] = "/docs"
        return info

    return app


app = create_app()
