"""Tests endpoints /v1/news/* (Google News RSS)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_news_list_empty(client) -> None:
    """GET /news sur DB vide retourne []."""
    r = await client.get("/v1/news")
    assert r.status_code == 200
    body = r.json()
    # Peut etre soit list directement, soit dict avec articles
    if isinstance(body, dict):
        assert body.get("articles") == [] or body.get("total") == 0
    else:
        assert body == []


@pytest.mark.asyncio
async def test_news_list_pagination(client) -> None:
    """Pagination via limit + offset si endpoint le supporte."""
    r = await client.get("/v1/news?limit=10")
    assert r.status_code == 200
