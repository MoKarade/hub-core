"""Routes API v1."""

from fastapi import APIRouter

from src.api.v1 import ai, finance, health, locations

router = APIRouter(prefix="/v1")
router.include_router(health.router, tags=["health"])
router.include_router(finance.router)
router.include_router(locations.router)
router.include_router(ai.router)
