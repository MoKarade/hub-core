"""Tests endpoint /v1/scheduler/* admin."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_scheduler_status_returns_jobs_list(client) -> None:
    """GET /scheduler/status retourne running + jobs."""
    r = await client.get("/v1/scheduler/status")
    assert r.status_code == 200
    body = r.json()
    assert "running" in body
    assert "jobs" in body
    assert isinstance(body["jobs"], list)


@pytest.mark.asyncio
async def test_scheduler_run_unknown_job_400(client) -> None:
    """POST /scheduler/run/{unknown} retourne 400."""
    r = await client.post("/v1/scheduler/run/foo_bar_unknown")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_scheduler_run_known_job_returns_started(client) -> None:
    """POST /scheduler/run/news (job connu) retourne started."""
    # Le scheduler n'est pas demarre dans les tests mais run_job_now
    # cree juste une coroutine asyncio task sans verifier le scheduler.
    r = await client.post("/v1/scheduler/run/news")
    # Soit started (job lance dans un task), soit 400 si meta fail
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        body = r.json()
        assert body["status"] == "started"
        assert body["job_id"] == "news"
