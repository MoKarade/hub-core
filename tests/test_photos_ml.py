"""Tests endpoints /v1/photos/* ML (CLIP + face recognition).

Note : on teste uniquement les chemins qui ne necessitent PAS torch/dlib
installes (status, search/embed quand pas de modele = 503, list face_clusters
vide). Les chemins qui requierent ML reels demandent les extras .[ml].
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_ml_status_clean(client) -> None:
    """GET /ml-status retourne le diagnostic d'install + counts."""
    r = await client.get("/v1/photos/ml-status")
    assert r.status_code == 200
    body = r.json()
    assert "clip_installed" in body
    assert "face_recognition_installed" in body
    # Tous a 0 sur DB vide
    assert body["total_photos"] == 0
    assert body["total_embeddings"] == 0
    assert body["total_faces"] == 0
    assert body["total_clusters"] == 0


@pytest.mark.asyncio
async def test_face_clusters_list_empty(client) -> None:
    """GET /face-clusters sans cluster retourne []."""
    r = await client.get("/v1/photos/face-clusters")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty(client) -> None:
    """GET /search?q=  (query vide) retourne [] sans charger CLIP."""
    r = await client.get("/v1/photos/search?q=")
    # Soit 200 [] (skip CLIP early), soit 503 (CLIP non installe + non skipe)
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert r.json() == []


@pytest.mark.asyncio
async def test_face_cluster_404_on_unknown(client) -> None:
    """PATCH /face-clusters/{unknown_id} => 404."""
    fake_id = "00000000-0000-0000-0000-000000000000"
    r = await client.patch(
        f"/v1/photos/face-clusters/{fake_id}",
        json={"name": "Marc"},
    )
    assert r.status_code == 404
