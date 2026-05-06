"""Tests endpoints /v1/steam/*."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_steam_status_not_configured(client) -> None:
    """Sans STEAM_API_KEY, status configured=False."""
    r = await client.get("/v1/steam/status")
    assert r.status_code == 200
    body = r.json()
    assert "configured" in body
    assert body["games_in_db"] == 0
    assert body["snapshots_in_db"] == 0


@pytest.mark.asyncio
async def test_steam_stats_empty(client) -> None:
    """Stats sur DB vide retourne tous 0."""
    r = await client.get("/v1/steam/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_games"] == 0
    assert body["total_playtime_hours"] == 0
    assert body["games_played_2w"] == 0
    assert body["top_games"] == []
    assert body["last_played"] is None


@pytest.mark.asyncio
async def test_steam_games_empty(client) -> None:
    """GET /games sans data retourne []."""
    r = await client.get("/v1/steam/games")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_steam_sessions_empty(client) -> None:
    """GET /sessions sans snapshots retourne []."""
    r = await client.get("/v1/steam/sessions")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_steam_sync_without_creds_503(client, monkeypatch) -> None:
    """POST /sync sans STEAM_API_KEY => 503."""
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "steam_api_key", "")
    monkeypatch.setattr(settings, "steam_user_id", "")

    r = await client.post("/v1/steam/sync")
    assert r.status_code == 503
