"""Tests avances /v1/me/dashboard avec data inseree."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.asyncio
async def test_me_with_browser_data_counts_visits(client) -> None:
    """Apres insert browser, counts.browser_visits reflete."""
    now = datetime.now(UTC)
    await client.post(
        "/v1/browser/sync",
        json={
            "items": [
                {
                    "url": f"https://example{i}.com",
                    "visited_at": now.isoformat(),
                }
                for i in range(5)
            ]
        },
    )

    r = await client.get("/v1/me/dashboard?period=30d")
    body = r.json()
    assert body["counts"]["browser_visits"] >= 5
    assert body["counts"]["browser_unique_domains"] >= 5


@pytest.mark.asyncio
async def test_me_with_privacy_request(client) -> None:
    """Apres creation privacy request, counts.privacy_requests = 1."""
    await client.post(
        "/v1/privacy/requests",
        json={"company_name": "TestMeAdv", "request_type": "deletion"},
    )

    r = await client.get("/v1/me/dashboard?period=all")
    body = r.json()
    assert body["counts"]["privacy_requests"] >= 1


@pytest.mark.asyncio
async def test_me_period_7d_excludes_old_data(client) -> None:
    """Avec period=7d, le data >7j ago n'est pas compte."""
    now = datetime.now(UTC)
    # Insert browser visit il y a 100 jours
    await client.post(
        "/v1/browser/sync",
        json={
            "items": [
                {
                    "url": "https://very-old.com",
                    "visited_at": (now - timedelta(days=100)).isoformat(),
                }
            ]
        },
    )

    r7 = await client.get("/v1/me/dashboard?period=7d")
    rall = await client.get("/v1/me/dashboard?period=all")

    # 7d ne devrait pas compter cette visite, all oui
    assert r7.json()["counts"]["browser_visits"] == 0
    assert rall.json()["counts"]["browser_visits"] >= 1


@pytest.mark.asyncio
async def test_me_screen_time_browser_top_domains(client) -> None:
    """top_domains apparait apres insert."""
    now = datetime.now(UTC)
    await client.post(
        "/v1/browser/sync",
        json={
            "items": [
                {
                    "url": "https://github.com/x",
                    "visited_at": (now - timedelta(minutes=i)).isoformat(),
                }
                for i in range(10)
            ]
        },
    )

    r = await client.get("/v1/me/dashboard?period=30d")
    body = r.json()
    domains = [d["domain"] for d in body["screen_time"]["browser_top_domains"]]
    assert "github.com" in domains
