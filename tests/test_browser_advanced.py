"""Tests avances /v1/browser : edge cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_browser_sync_empty_items_ok(client) -> None:
    """POST /sync avec items vide => 0/0/0."""
    r = await client.post("/v1/browser/sync", json={"items": []})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ingested": 0, "skipped_dedup": 0, "errors": 0}


@pytest.mark.asyncio
async def test_browser_url_truncated_at_8000(client) -> None:
    """URL > 8000 chars est tronquee."""
    long_url = "https://example.com/" + "a" * 10000
    r = await client.post(
        "/v1/browser/sync",
        json={
            "items": [
                {
                    "url": long_url,
                    "visited_at": datetime.now(UTC).isoformat(),
                }
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["ingested"] == 1

    r2 = await client.get("/v1/browser/history?limit=10")
    items = r2.json()
    assert len(items[0]["url"]) <= 8000


@pytest.mark.asyncio
async def test_browser_history_pagination(client) -> None:
    """offset + limit fonctionnent."""
    now = datetime.now(UTC)
    items = [
        {"url": f"https://x.com/page{i}", "visited_at": (now - timedelta(minutes=i)).isoformat()}
        for i in range(5)
    ]
    await client.post("/v1/browser/sync", json={"items": items})

    r = await client.get("/v1/browser/history?limit=2&offset=1")
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_browser_filter_since_days(client) -> None:
    """since_days filtre par anciennete."""
    now = datetime.now(UTC)
    await client.post(
        "/v1/browser/sync",
        json={
            "items": [
                {
                    "url": "https://recent.com",
                    "visited_at": (now - timedelta(days=2)).isoformat(),
                },
                {
                    "url": "https://old.com",
                    "visited_at": (now - timedelta(days=100)).isoformat(),
                },
            ]
        },
    )

    r = await client.get("/v1/browser/history?since_days=7")
    items = r.json()
    domains = [i["domain"] for i in items]
    assert "recent.com" in domains
    assert "old.com" not in domains


@pytest.mark.asyncio
async def test_browser_stats_top_domains_sorted(client) -> None:
    """top_domains tries par count desc."""
    now = datetime.now(UTC)
    items = [
        {"url": "https://github.com/x", "visited_at": (now - timedelta(minutes=i)).isoformat()}
        for i in range(10)
    ] + [
        {
            "url": "https://news.ycombinator.com/",
            "visited_at": (now - timedelta(hours=i)).isoformat(),
        }
        for i in range(3)
    ]
    await client.post("/v1/browser/sync", json={"items": items})

    r = await client.get("/v1/browser/stats?since_days=7")
    body = r.json()
    if len(body["top_domains"]) >= 2:
        assert body["top_domains"][0]["count"] >= body["top_domains"][1]["count"]
