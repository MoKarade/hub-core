"""Tests avances /v1/streaming : workflow OAuth + history filters."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_streaming_history_with_item_type_filter(client) -> None:
    """GET /history?item_type=movie filtre correctement (sans data = [])."""
    r = await client.get("/v1/streaming/history?item_type=movie")
    assert r.status_code == 200
    assert r.json() == []

    r = await client.get("/v1/streaming/history?item_type=episode")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_streaming_history_since_days(client) -> None:
    """since_days fonctionne sans crasher meme sans data."""
    r = await client.get("/v1/streaming/history?since_days=7")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_streaming_status_returns_required_fields(client) -> None:
    """status retourne les champs minimums."""
    r = await client.get("/v1/streaming/status")
    body = r.json()
    assert "connected" in body
    assert isinstance(body["connected"], bool)


@pytest.mark.asyncio
async def test_streaming_oauth_callback_without_code_422(client) -> None:
    """GET /oauth/callback sans ?code= retourne 422 (validation)."""
    r = await client.get("/v1/streaming/oauth/callback")
    # Pas de code -> 422 Pydantic
    assert r.status_code == 422
