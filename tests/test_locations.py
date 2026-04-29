"""Tests des endpoints /v1/locations/points."""

import pytest


def _point_payload(dedup_hash: str, **overrides):
    base = {
        "timestamp_utc": "2026-01-15T14:30:00Z",
        "latitude": "46.7383000",
        "longitude": "-71.2433000",
        "accuracy_m": 25,
        "altitude_m": 50,
        "activity_type": "walking",
        "source": "test",
        "source_file": "test.json",
        "latitude_e7": 467383000,
        "longitude_e7": -712433000,
        "dedup_hash": dedup_hash,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_list_empty(client):
    r = await client.get("/v1/locations/points")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_point(client, dedup):
    r = await client.post("/v1/locations/points", json=_point_payload(dedup("p1")))
    assert r.status_code == 201
    body = r.json()
    assert body["activity_type"] == "walking"
    assert body["source"] == "test"


@pytest.mark.asyncio
async def test_idempotence(client, dedup):
    payload = _point_payload(dedup("p-idem"))
    r1 = await client.post("/v1/locations/points", json=payload)
    r2 = await client.post("/v1/locations/points", json=payload)
    assert r1.json()["id"] == r2.json()["id"]

    rl = await client.get("/v1/locations/points")
    assert len(rl.json()) == 1


@pytest.mark.asyncio
async def test_filter_activity_type(client, dedup):
    await client.post("/v1/locations/points", json=_point_payload(dedup("walk-1"), activity_type="walking"))
    await client.post("/v1/locations/points", json=_point_payload(
        dedup("drive-1"),
        activity_type="driving",
        timestamp_utc="2026-01-15T15:00:00Z",
        latitude_e7=467383001,
        longitude_e7=-712433001,
    ))

    walks = await client.get("/v1/locations/points?activity_type=walking")
    drives = await client.get("/v1/locations/points?activity_type=driving")
    assert len(walks.json()) == 1
    assert len(drives.json()) == 1


@pytest.mark.asyncio
async def test_filter_source(client, dedup):
    await client.post("/v1/locations/points", json=_point_payload(
        dedup("google-1"),
        source="google_takeout_timeline",
    ))
    await client.post("/v1/locations/points", json=_point_payload(
        dedup("manual-1"),
        source="manual_pin",
        latitude_e7=467383002,
        longitude_e7=-712433002,
    ))

    google = await client.get("/v1/locations/points?source=google_takeout_timeline")
    manual = await client.get("/v1/locations/points?source=manual_pin")
    assert len(google.json()) == 1
    assert len(manual.json()) == 1


@pytest.mark.asyncio
async def test_get_404(client):
    r = await client.get("/v1/locations/points/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invalid_lat_out_of_range(client, dedup):
    r = await client.post(
        "/v1/locations/points",
        json=_point_payload(dedup("bad-lat"), latitude="100.0"),  # > 90
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_invalid_lng_out_of_range(client, dedup):
    r = await client.post(
        "/v1/locations/points",
        json=_point_payload(dedup("bad-lng"), longitude="-200.0"),  # < -180
    )
    assert r.status_code == 422
