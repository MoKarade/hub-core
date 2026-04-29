"""Tests des endpoints /v1/finance/accounts."""

import pytest


@pytest.mark.asyncio
async def test_list_empty(client):
    r = await client.get("/v1/finance/accounts")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_account(client):
    payload = {
        "institution": "Desjardins",
        "account_type": "checking",
        "account_number_masked": "999999-EOP",
        "nickname": "Test courant",
        "currency": "CAD",
        "is_active": True,
    }
    r = await client.post("/v1/finance/accounts", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["institution"] == "Desjardins"
    assert body["account_type"] == "checking"
    assert body["account_number_masked"] == "999999-EOP"
    assert body["currency"] == "CAD"
    assert "id" in body
    assert "created_at" in body


@pytest.mark.asyncio
async def test_create_account_idempotent(client):
    payload = {
        "institution": "Desjardins",
        "account_type": "checking",
        "account_number_masked": "999999-EOP",
        "nickname": "First nickname",
        "currency": "CAD",
        "is_active": True,
    }
    # Premier POST
    r1 = await client.post("/v1/finance/accounts", json=payload)
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    # Deuxième POST avec mêmes (institution, account_number_masked) → même compte
    payload["nickname"] = "Different nickname"  # nickname change ne brise pas l'idempotence
    r2 = await client.post("/v1/finance/accounts", json=payload)
    assert r2.status_code == 201
    id2 = r2.json()["id"]
    assert id1 == id2

    # Liste : un seul compte
    rl = await client.get("/v1/finance/accounts")
    assert len(rl.json()) == 1


@pytest.mark.asyncio
async def test_list_accounts_after_creation(client):
    # Crée 2 comptes
    for masked in ["999999-EOP", "999999-ET1"]:
        await client.post(
            "/v1/finance/accounts",
            json={
                "institution": "Desjardins",
                "account_type": "checking" if "EOP" in masked else "savings",
                "account_number_masked": masked,
                "currency": "CAD",
            },
        )
    r = await client.get("/v1/finance/accounts")
    assert r.status_code == 200
    accounts = r.json()
    assert len(accounts) == 2
    masked_set = {a["account_number_masked"] for a in accounts}
    assert masked_set == {"999999-EOP", "999999-ET1"}


@pytest.mark.asyncio
async def test_filter_is_active(client):
    await client.post(
        "/v1/finance/accounts",
        json={
            "institution": "Desjardins",
            "account_type": "checking",
            "account_number_masked": "999999-EOP",
            "currency": "CAD",
            "is_active": True,
        },
    )
    await client.post(
        "/v1/finance/accounts",
        json={
            "institution": "Desjardins",
            "account_type": "savings",
            "account_number_masked": "999999-ET1",
            "currency": "CAD",
            "is_active": False,
        },
    )

    actives = await client.get("/v1/finance/accounts?is_active=true")
    inactives = await client.get("/v1/finance/accounts?is_active=false")
    assert len(actives.json()) == 1
    assert len(inactives.json()) == 1


@pytest.mark.asyncio
async def test_get_account_by_id(client):
    cr = await client.post(
        "/v1/finance/accounts",
        json={
            "institution": "Desjardins",
            "account_type": "checking",
            "account_number_masked": "999999-EOP",
            "currency": "CAD",
        },
    )
    aid = cr.json()["id"]

    r = await client.get(f"/v1/finance/accounts/{aid}")
    assert r.status_code == 200
    assert r.json()["id"] == aid


@pytest.mark.asyncio
async def test_get_account_404(client):
    # UUID valide mais inexistant
    r = await client.get("/v1/finance/accounts/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invalid_currency(client):
    r = await client.post(
        "/v1/finance/accounts",
        json={
            "institution": "Desjardins",
            "account_type": "checking",
            "account_number_masked": "999999-EOP",
            "currency": "CADD",  # 4 chars : invalide (min 3 max 3)
        },
    )
    assert r.status_code == 422
