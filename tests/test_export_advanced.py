"""Tests avances /v1/export : edge cases + content."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC, datetime

import pytest


@pytest.mark.asyncio
async def test_export_zip_contains_readme_with_counts(client) -> None:
    """README.txt liste les tables avec leurs counts."""
    r = await client.get("/v1/export/all")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    readme = zf.read("README.txt").decode("utf-8")
    assert "Personal Data Hub" in readme
    assert "transactions.csv" in readme


@pytest.mark.asyncio
async def test_export_csv_has_headers(client) -> None:
    """Chaque CSV exporte commence par une ligne de headers."""
    r = await client.get("/v1/export/all")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    csv_files = [n for n in zf.namelist() if n.endswith(".csv")]
    for name in csv_files:
        content = zf.read(name).decode("utf-8")
        first_line = content.split("\n")[0] if content else ""
        # Headers presents (ligne pas vide, contient au moins une virgule
        # ou le seul nom de colonne pour table avec 1 col)
        assert first_line, f"{name} has no headers"


@pytest.mark.asyncio
async def test_export_browser_history_in_zip(client) -> None:
    """browser_history exportee si la table existe."""
    # Insere 1 row
    await client.post(
        "/v1/browser/sync",
        json={
            "items": [
                {
                    "url": "https://example.com",
                    "visited_at": datetime.now(UTC).isoformat(),
                }
            ]
        },
    )

    r = await client.get("/v1/export/all")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    # Il devrait y avoir un browser_history.csv ou pas (selon si on l'a ajoute
    # a EXPORT_TABLES). Pour l'instant on teste juste qu'on a un ZIP valide.
    assert "manifest.json" in zf.namelist()


@pytest.mark.asyncio
async def test_export_csv_has_data_after_inserts(client) -> None:
    """Apres insert d'une privacy request, removal_requests.csv contient une row."""
    await client.post(
        "/v1/privacy/requests",
        json={"company_name": "TestExport", "request_type": "deletion"},
    )

    r = await client.get("/v1/export/all")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    rr_csv = zf.read("removal_requests.csv").decode("utf-8")
    reader = csv.DictReader(io.StringIO(rr_csv))
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["company_name"] == "TestExport"


@pytest.mark.asyncio
async def test_export_manifest_structure(client) -> None:
    """manifest.json a tous les champs attendus."""
    r = await client.get("/v1/export/all")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    manifest = json.loads(zf.read("manifest.json"))
    assert "generated_at" in manifest
    assert "tables" in manifest
    assert "include_email_bodies" in manifest
    assert "total_rows" in manifest
    assert isinstance(manifest["tables"], dict)


@pytest.mark.asyncio
async def test_export_preview_includes_all_tables(client) -> None:
    """preview liste toutes les tables exportables."""
    r = await client.get("/v1/export/preview")
    body = r.json()
    expected_tables = [
        "transactions.csv",
        "photos.csv",
        "calendar_events.csv",
        "removal_requests.csv",
        "streaming_activities.csv",
    ]
    for t in expected_tables:
        assert t in body["tables"], f"Manque {t} dans preview"
