"""Tests endpoints /v1/notifications/* (Web Push subscriptions)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_vapid_public_key_returned(client) -> None:
    """GET /vapid-public-key retourne la cle publique (config-dependent)."""
    r = await client.get("/v1/notifications/vapid-public-key")
    # En tests sans config VAPID, peut etre 503 ou 200 avec cle vide
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        body = r.json()
        assert "public_key" in body


@pytest.mark.asyncio
async def test_subscribe_validation(client) -> None:
    """POST /subscribe sans keys requis => 422."""
    r = await client.post(
        "/v1/notifications/subscribe",
        json={"endpoint": "https://fcm.googleapis.com/fcm/send/test"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_subscribe_creates(client) -> None:
    """POST /subscribe avec donnees valides cree une subscription."""
    r = await client.post(
        "/v1/notifications/subscribe",
        json={
            "endpoint": "https://fcm.googleapis.com/fcm/send/test123",
            "keys": {
                "p256dh": "BNbPsM3-NbXNb3JoUJK7ot0i65d5XyaTM6Jd5D1c8fM",
                "auth": "abcdef1234567890abcdef",
            },
            "label": "Test Device",
        },
    )
    assert r.status_code in (200, 201)
    body = r.json()
    assert "id" in body
    assert body["status"] in ("created", "updated")


@pytest.mark.asyncio
async def test_subscriptions_list(client) -> None:
    """GET /subscriptions retourne la liste."""
    # Cree d'abord une sub
    await client.post(
        "/v1/notifications/subscribe",
        json={
            "endpoint": "https://example.com/push/x",
            "keys": {"p256dh": "test_pkey", "auth": "test_auth"},
            "label": "ListTest",
        },
    )

    r = await client.get("/v1/notifications/subscriptions")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert any(s["label"] == "ListTest" for s in body)


@pytest.mark.asyncio
async def test_unsubscribe(client) -> None:
    """POST /unsubscribe revoque la subscription."""
    endpoint = "https://example.com/push/unsub_test"
    await client.post(
        "/v1/notifications/subscribe",
        json={
            "endpoint": endpoint,
            "keys": {"p256dh": "k", "auth": "a"},
        },
    )

    r = await client.post(
        "/v1/notifications/unsubscribe",
        json={"endpoint": endpoint},
    )
    assert r.status_code in (200, 204)
