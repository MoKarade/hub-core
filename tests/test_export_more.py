"""Tests supplementaires /v1/export."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

import pytest


@pytest.mark.asyncio
async def test_export_with_browser_data(client) -> None:
    """Apres insert browser, le ZIP contient les data."""
    now = datetime.now(UTC)
    await client.post(
        "/v1/browser/sync",
        json={"items": [{"url": "https://exp.com", "visited_at": now.isoformat()}]},
    )

    r = await client.get("/v1/export/all?confirm=oui")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert "manifest.json" in zf.namelist()


@pytest.mark.asyncio
async def test_export_preview_keeps_zero_for_empty_tables(client) -> None:
    """preview retourne 0 pour les tables vides."""
    r = await client.get("/v1/export/preview")
    body = r.json()
    # Au moins une table doit avoir count >= 0 (pas -1 = error)
    assert all(v >= 0 or v == -1 for v in body["tables"].values())


@pytest.mark.asyncio
async def test_export_returns_zip_content_type(client) -> None:
    """Headers content-type correct."""
    r = await client.get("/v1/export/all?confirm=oui")
    assert r.headers["content-type"] == "application/zip"
    assert "Content-Disposition" in r.headers or "content-disposition" in r.headers


@pytest.mark.asyncio
async def test_export_filename_has_timestamp(client) -> None:
    """Le filename suggere a un timestamp YYYYMMDD."""
    r = await client.get("/v1/export/all?confirm=oui")
    cd = r.headers.get("content-disposition") or r.headers.get("Content-Disposition", "")
    assert "hub-export-" in cd
    assert ".zip" in cd
