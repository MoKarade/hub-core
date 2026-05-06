"""Tests endpoint /v1/export/* — export ZIP complet."""

from __future__ import annotations

import io
import zipfile

import pytest


@pytest.mark.asyncio
async def test_export_preview_returns_counts(client) -> None:
    """GET /export/preview retourne counts par table (0 si vide)."""
    r = await client.get("/v1/export/preview")
    assert r.status_code == 200
    body = r.json()
    assert "tables" in body
    assert "total_rows" in body
    # Au moins quelques tables exportables connues
    assert "transactions.csv" in body["tables"]
    assert "photos.csv" in body["tables"]
    assert "calendar_events.csv" in body["tables"]
    # Sur DB vide, total = 0
    assert body["total_rows"] == 0


@pytest.mark.asyncio
async def test_export_all_returns_zip(client) -> None:
    """GET /export/all retourne un ZIP valide avec CSV + manifest + README."""
    r = await client.get("/v1/export/all")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()

    # Au moins manifest + README
    assert "manifest.json" in names
    assert "README.txt" in names

    # Au moins 1 CSV par table principale
    expected_csvs = [
        "transactions.csv",
        "photos.csv",
        "calendar_events.csv",
        "emails.csv",
        "removal_requests.csv",
    ]
    for csv_name in expected_csvs:
        assert csv_name in names, f"Manque {csv_name} dans l'export"

    # Manifest est un JSON parseable avec generated_at
    import json

    manifest = json.loads(zf.read("manifest.json"))
    assert "generated_at" in manifest
    assert "tables" in manifest


@pytest.mark.asyncio
async def test_export_excludes_email_bodies_by_default(client) -> None:
    """Sans flag, manifest indique include_email_bodies=False."""
    r = await client.get("/v1/export/all")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    import json

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["include_email_bodies"] is False


@pytest.mark.asyncio
async def test_export_with_email_bodies_flag(client) -> None:
    """Avec ?include_email_bodies=true, manifest le reflete."""
    r = await client.get("/v1/export/all?include_email_bodies=true")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    import json

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["include_email_bodies"] is True
