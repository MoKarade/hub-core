"""Routes API v1."""

from fastapi import APIRouter

from src.api.v1 import (
    ai,
    calendar,
    drive,
    emails,
    events,
    finance,
    health,
    health_data,
    locations,
    oauth,
    osint,
    photos,
)

router = APIRouter(prefix="/v1")
router.include_router(health.router, tags=["health"])
router.include_router(finance.router)
router.include_router(locations.router)
router.include_router(ai.router)
router.include_router(events.router)
router.include_router(oauth.router)
router.include_router(osint.router)
router.include_router(emails.router)
router.include_router(calendar.router)
router.include_router(health_data.router)
router.include_router(photos.router)
router.include_router(drive.router)
