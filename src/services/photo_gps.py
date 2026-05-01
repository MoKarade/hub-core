"""Extraction GPS / EXIF depuis les bytes des photos Picker (Phase 3c+).

Workflow :
1. Telecharge les bytes via Picker baseUrl + Bearer token
2. Parse l'EXIF via exifread
3. Extract GPSInfo + convertit DMS -> decimal
4. Reverse geocode optionnel via Nominatim (free OSM)
"""

from __future__ import annotations

import io
import logging
from typing import Any

import exifread
import httpx

logger = logging.getLogger(__name__)

NOMINATIM_API = "https://nominatim.openstreetmap.org/reverse"


def _dms_to_decimal(dms: list[Any], ref: str) -> float | None:
    """Convertit DMS (degres, minutes, secondes) en decimal signe.
    DMS = exifread Ratio objects.
    """
    if len(dms) != 3:
        return None
    try:
        deg = float(dms[0].num) / float(dms[0].den) if dms[0].den else 0
        mn = float(dms[1].num) / float(dms[1].den) if dms[1].den else 0
        sec = float(dms[2].num) / float(dms[2].den) if dms[2].den else 0
        val = deg + mn / 60 + sec / 3600
        return -val if ref in ("S", "W") else val
    except (AttributeError, ValueError, ZeroDivisionError):
        return None


async def download_photo_bytes(base_url: str, access_token: str, max_dim: int = 1024) -> bytes:
    """Telecharge la photo depuis Picker baseUrl avec auth.
    max_dim : taille max d'un cote (1024 = ~quart-HD pour parser EXIF, suffisant)."""
    url = f"{base_url}=w{max_dim}-h{max_dim}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.content


def extract_gps_from_bytes(
    photo_bytes: bytes,
) -> tuple[float | None, float | None, float | None, dict[str, str]]:
    """Parse l'EXIF avec exifread. Retourne (lat, lng, alt_m, exif_dict)."""
    try:
        tags = exifread.process_file(io.BytesIO(photo_bytes), details=False, debug=False)
    except Exception as e:
        logger.warning("exifread_failed: %r", e)
        return None, None, None, {}

    lat = lng = alt = None
    if "GPS GPSLatitude" in tags and "GPS GPSLatitudeRef" in tags:
        lat = _dms_to_decimal(list(tags["GPS GPSLatitude"].values), str(tags["GPS GPSLatitudeRef"]))
    if "GPS GPSLongitude" in tags and "GPS GPSLongitudeRef" in tags:
        lng = _dms_to_decimal(
            list(tags["GPS GPSLongitude"].values), str(tags["GPS GPSLongitudeRef"])
        )
    if "GPS GPSAltitude" in tags:
        try:
            v = tags["GPS GPSAltitude"].values[0]
            alt = float(v.num) / float(v.den) if v.den else None
        except (AttributeError, ValueError, ZeroDivisionError):
            pass

    # Stocke un sous-ensemble EXIF utile (pas le RAW complet, qui contient
    # MakerNote massif et thumbnail). Filtre par prefixe + skip Thumbnail.
    exif: dict[str, str] = {}
    KEEP_PREFIXES = ("Image ", "EXIF ", "GPS ")
    SKIP_KEYWORDS = ("Thumbnail", "MakerNote", "UserComment")
    for k, v in tags.items():
        ks = str(k)
        if not any(ks.startswith(p) for p in KEEP_PREFIXES):
            continue
        if any(skip in ks for skip in SKIP_KEYWORDS):
            continue
        try:
            sval = str(v)
            if len(sval) < 200:  # skip blobs
                exif[ks] = sval
        except Exception:
            continue

    return lat, lng, alt, exif


async def reverse_geocode(lat: float, lng: float) -> str | None:
    """Reverse geocode via Nominatim (free OSM). Rate limit 1 req/sec.
    Retourne 'Ville, Région, Pays' ou None."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                NOMINATIM_API,
                params={
                    "lat": lat,
                    "lon": lng,
                    "format": "json",
                    "zoom": 14,  # quartier/village
                    "accept-language": "fr",
                },
                headers={"User-Agent": "PersonalDataHub/1.0 (marc.richard4@gmail.com)"},
            )
            r.raise_for_status()
            data = r.json()
            addr = data.get("address", {})
            # Format minimal : ville, region, pays
            parts: list[str] = []
            for key in ("city", "town", "village", "suburb"):
                if key in addr:
                    parts.append(addr[key])
                    break
            for key in ("state", "region"):
                if key in addr:
                    parts.append(addr[key])
                    break
            if "country" in addr:
                parts.append(addr["country"])
            return ", ".join(parts) if parts else data.get("display_name")
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.warning("nominatim_failed: %r", e)
        return None
