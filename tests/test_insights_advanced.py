"""Tests avances /v1/insights : structure, severity ordering, sources."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_insights_returns_response_shape(client) -> None:
    """L'endpoint retourne InsightsResponse avec total + by_severity."""
    r = await client.get("/v1/insights")
    assert r.status_code == 200
    body = r.json()
    assert "insights" in body
    assert "total" in body
    assert "by_severity" in body
    assert "generated_at" in body
    assert isinstance(body["insights"], list)
    assert isinstance(body["by_severity"], dict)


@pytest.mark.asyncio
async def test_insights_total_matches_list_length(client) -> None:
    r = await client.get("/v1/insights")
    body = r.json()
    assert body["total"] == len(body["insights"])


@pytest.mark.asyncio
async def test_insights_severity_distribution(client) -> None:
    """by_severity sums to total (chaque insight a une severity)."""
    r = await client.get("/v1/insights")
    body = r.json()
    by_sev_sum = sum(body["by_severity"].values())
    assert by_sev_sum == body["total"]


@pytest.mark.asyncio
async def test_insights_each_has_required_fields(client) -> None:
    """Tous les insights ont les champs critiques."""
    r = await client.get("/v1/insights")
    for ins in r.json()["insights"]:
        assert "severity" in ins
        assert "title" in ins
        assert "source" in ins
        assert ins["severity"] in ("critical", "warning", "info", "positive")


@pytest.mark.asyncio
async def test_insights_sorted_by_severity(client) -> None:
    """Les critical viennent avant les warning, etc."""
    r = await client.get("/v1/insights")
    insights = r.json()["insights"]
    if len(insights) < 2:
        pytest.skip("not enough insights to verify ordering")

    rank = {"critical": 0, "warning": 1, "info": 2, "positive": 3}
    prev = -1
    for ins in insights:
        cur = rank[ins["severity"]]
        assert cur >= prev, "insights not sorted by severity"
        prev = cur
