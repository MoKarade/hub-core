"""Personal Data Hub — entrée FastAPI."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.v1 import router as v1_router
from src.core.config import get_settings
from src.core.logging import logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hooks au démarrage / arrêt du hub."""
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("hub_startup", env=settings.app_env, app=settings.app_name)
    yield
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
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(v1_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "name": "Personal Data Hub",
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/v1/health",
        }

    return app


app = create_app()
