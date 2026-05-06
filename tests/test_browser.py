"""Tests endpoints /v1/browser/* — historique navigateur."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_browser_stats_empty(client) -> None:
    """Stats sans aucune visite : tous les counts a 0."""
    r = await client.get("/v1/browser/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_visits"] == 0
    assert body["unique_domains"] == 0
    assert body["top_domains"] == []


@pytest.mark.asyncio
async def test_browser_sync_inserts_unique_visits(client) -> None:
    """POST /sync ingere les items + dedup auto sur (url, visited_at)."""
    now = datetime.now(UTC)
    payload = {
        "items": [
            {
                "source": "chrome",
                "external_id": "1",
                "url": "https://example.com/page1",
                "title": "Page 1",
                "visited_at": now.isoformat(),
                "visit_duration_s": 30,
                "transition": "link",
            },
            {
                "source": "chrome",
                "external_id": "2",
                "url": "https://github.com/MoKarade",
                "title": "GitHub",
                "visited_at": (now - timedelta(hours=1)).isoformat(),
                "visit_duration_s": 120,
                "transition": "typed",
            },
        ]
    }
    r = await client.post("/v1/browser/sync", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ingested"] == 2
    assert body["skipped_dedup"] == 0
    assert body["errors"] == 0


@pytest.mark.asyncio
async def test_browser_sync_dedup_on_replay(client) -> None:
    """Re-poster les memes items = 0 ingested + N skipped_dedup."""
    now = datetime.now(UTC)
    payload = {
        "items": [
            {
                "url": "https://example.com",
                "visited_at": now.isoformat(),
            }
        ]
    }
    r1 = await client.post("/v1/browser/sync", json=payload)
    assert r1.json()["ingested"] == 1

    r2 = await client.post("/v1/browser/sync", json=payload)
    assert r2.json()["ingested"] == 0
    assert r2.json()["skipped_dedup"] == 1


@pytest.mark.asyncio
async def test_browser_history_filters(client) -> None:
    """GET /history avec ?domain= et ?q= filtre correctement."""
    now = datetime.now(UTC)
    await client.post(
        "/v1/browser/sync",
        json={
            "items": [
                {
                    "url": "https://github.com/MoKarade/hub-core",
                    "title": "hub-core repo",
                    "visited_at": now.isoformat(),
                },
                {
                    "url": "https://news.ycombinator.com/",
                    "title": "Hacker News",
                    "visited_at": now.isoformat(),
                },
            ]
        },
    )

    # Filter par domain
    r = await client.get("/v1/browser/history?domain=github.com")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["domain"] == "github.com"

    # Recherche q
    r = await client.get("/v1/browser/history?q=hacker")
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_browser_extracts_domain(client) -> None:
    """Le domain est extrait correctement de l'URL."""
    now = datetime.now(UTC)
    await client.post(
        "/v1/browser/sync",
        json={
            "items": [
                {
                    "url": "https://www.netflix.com/watch/123?trackId=abc",
                    "visited_at": now.isoformat(),
                }
            ]
        },
    )
    r = await client.get("/v1/browser/history")
    items = r.json()
    assert items[0]["domain"] == "www.netflix.com"


@pytest.mark.asyncio
async def test_browser_wipe_requires_confirm(client) -> None:
    """DELETE /wipe sans ?confirm=true => 400."""
    r = await client.delete("/v1/browser/wipe")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_browser_wipe_confirmed(client) -> None:
    """DELETE /wipe?confirm=true vide tout."""
    now = datetime.now(UTC)
    await client.post(
        "/v1/browser/sync",
        json={"items": [{"url": "https://x.com", "visited_at": now.isoformat()}]},
    )
    r = await client.delete("/v1/browser/wipe?confirm=true")
    assert r.status_code == 200
    assert r.json()["deleted"] >= 1
