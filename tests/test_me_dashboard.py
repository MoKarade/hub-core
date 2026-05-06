"""Tests endpoint /v1/me/dashboard — vue cross-domain."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_me_dashboard_default_period(client) -> None:
    """Sans param, period par defaut = 30d."""
    r = await client.get("/v1/me/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == "30d"
    assert body["period_days"] == 30


@pytest.mark.asyncio
async def test_me_dashboard_period_validation(client) -> None:
    """period invalide retourne 400."""
    r = await client.get("/v1/me/dashboard?period=42d")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_me_dashboard_all_periods(client) -> None:
    """Toutes les periodes valides repondent 200."""
    for p in ["7d", "30d", "90d", "365d", "all"]:
        r = await client.get(f"/v1/me/dashboard?period={p}")
        assert r.status_code == 200, f"period={p} fail"
        body = r.json()
        assert body["period"] == p


@pytest.mark.asyncio
async def test_me_dashboard_structure(client) -> None:
    """Le dashboard a tous les sections attendues."""
    r = await client.get("/v1/me/dashboard?period=7d")
    body = r.json()
    assert "counts" in body
    assert "finance" in body
    assert "health" in body
    assert "locations" in body
    assert "screen_time" in body
    assert "productivity" in body
    assert "generated_at" in body


@pytest.mark.asyncio
async def test_me_dashboard_empty_db_zero_counts(client) -> None:
    """Sur DB vide tous les counts a 0 (pas de KeyError)."""
    r = await client.get("/v1/me/dashboard?period=30d")
    body = r.json()
    assert body["counts"]["transactions"] == 0
    assert body["counts"]["photos"] == 0
    assert body["counts"]["emails"] == 0
    assert body["finance"]["total_spend_cad"] == 0
    assert body["finance"]["net_cad"] == 0
    assert body["productivity"]["tasks_completed"] == 0
    assert body["screen_time"]["browser_visits"] == 0


@pytest.mark.asyncio
async def test_me_dashboard_period_all_returns_no_days_filter(client) -> None:
    """period=all => period_days=None."""
    r = await client.get("/v1/me/dashboard?period=all")
    body = r.json()
    assert body["period_days"] is None


@pytest.mark.asyncio
async def test_me_dashboard_completion_rate_null_when_no_tasks(client) -> None:
    """Sans aucune tache, completion_rate_pct = null (pas 0)."""
    r = await client.get("/v1/me/dashboard?period=30d")
    body = r.json()
    assert body["productivity"]["completion_rate_pct"] is None
