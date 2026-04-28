"""Routes API v1."""

from fastapi import APIRouter

from src.api.v1 import health

router = APIRouter(prefix="/v1")
router.include_router(health.router, tags=["health"])
