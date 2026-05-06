"""Tests endpoints /v1/streaming/* — Trakt.tv hub."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_streaming_stats_empty(client) -> None:
    """Stats sans aucune activite : tous a 0."""
    r = await client.get("/v1/streaming/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_activities"] == 0
    assert body["total_movies"] == 0
    assert body["total_episodes"] == 0
    assert body["total_runtime_hours"] == 0
    assert body["top_shows"] == []


@pytest.mark.asyncio
async def test_streaming_status_no_token(client) -> None:
    """GET /status retourne connected=False sans token Trakt."""
    r = await client.get("/v1/streaming/status")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body.get("reason") == "no token"


@pytest.mark.asyncio
async def test_streaming_history_empty(client) -> None:
    """GET /history sans data retourne liste vide."""
    r = await client.get("/v1/streaming/history")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_streaming_sync_without_token_401(client) -> None:
    """POST /sync sans token Trakt => 401."""
    r = await client.post(
        "/v1/streaming/sync",
        json={"days_back": 7, "max_results": 100},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_streaming_connect_without_client_id_503(client, monkeypatch) -> None:
    """GET /connect sans TRAKT_CLIENT_ID configure => 503."""
    # Override settings pour vider le client_id
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "trakt_client_id", "")

    r = await client.get("/v1/streaming/connect", follow_redirects=False)
    assert r.status_code == 503
