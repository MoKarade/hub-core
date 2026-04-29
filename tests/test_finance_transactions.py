"""Tests des endpoints /v1/finance/transactions."""

import pytest


async def _create_account(
    client,
    masked: str = "999999-EOP",
    account_type: str = "checking",
) -> str:
    r = await client.post(
        "/v1/finance/accounts",
        json={
            "institution": "Desjardins",
            "account_type": account_type,
            "account_number_masked": masked,
            "currency": "CAD",
        },
    )
    return r.json()["id"]


@pytest.mark.asyncio
async def test_list_empty(client):
    r = await client.get("/v1/finance/transactions")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_transaction_debit(client, dedup):
    aid = await _create_account(client)
    payload = {
        "account_id": aid,
        "transaction_date": "2026-03-15",
        "description": "Test debit",
        "debit": "100.00",
        "credit": None,
        "balance_after": "900.00",
        "source_format": "test_csv",
        "source_file": "test.csv",
        "source_seq_num": 1,
        "dedup_hash": dedup("test-debit-1"),
    }
    r = await client.post("/v1/finance/transactions", json=payload)
    assert r.status_code == 201
    body = r.json()
    from decimal import Decimal
    assert Decimal(body["debit"]) == Decimal("100.00")
    assert body["credit"] is None


@pytest.mark.asyncio
async def test_create_transaction_credit(client, dedup):
    aid = await _create_account(client)
    payload = {
        "account_id": aid,
        "transaction_date": "2026-03-15",
        "description": "Test credit",
        "debit": None,
        "credit": "250.00",
        "balance_after": "1150.00",
        "source_format": "test_csv",
        "dedup_hash": dedup("test-credit-1"),
    }
    r = await client.post("/v1/finance/transactions", json=payload)
    assert r.status_code == 201
    from decimal import Decimal
    assert Decimal(r.json()["credit"]) == Decimal("250.00")


@pytest.mark.asyncio
async def test_validation_debit_xor_credit(client, dedup):
    aid = await _create_account(client)
    # Les deux NULL → 422
    r = await client.post(
        "/v1/finance/transactions",
        json={
            "account_id": aid,
            "transaction_date": "2026-03-15",
            "description": "Both null",
            "debit": None,
            "credit": None,
            "source_format": "test_csv",
            "dedup_hash": dedup("test-bothnull"),
        },
    )
    assert r.status_code == 422

    # Les deux remplis → 422
    r2 = await client.post(
        "/v1/finance/transactions",
        json={
            "account_id": aid,
            "transaction_date": "2026-03-15",
            "description": "Both filled",
            "debit": "100.00",
            "credit": "100.00",
            "source_format": "test_csv",
            "dedup_hash": dedup("test-bothfilled"),
        },
    )
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_create_transaction_unknown_account(client, dedup):
    r = await client.post(
        "/v1/finance/transactions",
        json={
            "account_id": "00000000-0000-0000-0000-000000000000",
            "transaction_date": "2026-03-15",
            "description": "Test",
            "debit": "100.00",
            "source_format": "test_csv",
            "dedup_hash": dedup("test-unknownacct"),
        },
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_idempotence_dedup_hash(client, dedup):
    aid = await _create_account(client)
    payload = {
        "account_id": aid,
        "transaction_date": "2026-03-15",
        "description": "Test idempotence",
        "debit": "42.00",
        "source_format": "test_csv",
        "dedup_hash": dedup("test-idem"),
    }

    r1 = await client.post("/v1/finance/transactions", json=payload)
    r2 = await client.post("/v1/finance/transactions", json=payload)
    assert r1.json()["id"] == r2.json()["id"]

    # Une seule transaction en DB
    rl = await client.get("/v1/finance/transactions")
    assert len(rl.json()) == 1


@pytest.mark.asyncio
async def test_filter_by_account(client, dedup):
    a1 = await _create_account(client, "999999-EOP")
    a2 = await _create_account(client, "999999-ET1", "savings")

    for i in range(3):
        await client.post(
            "/v1/finance/transactions",
            json={
                "account_id": a1,
                "transaction_date": "2026-03-15",
                "description": f"a1-{i}",
                "debit": "10.00",
                "source_format": "test_csv",
                "dedup_hash": dedup(f"a1-{i}"),
            },
        )
    await client.post(
        "/v1/finance/transactions",
        json={
            "account_id": a2,
            "transaction_date": "2026-03-15",
            "description": "a2-0",
            "credit": "50.00",
            "source_format": "test_csv",
            "dedup_hash": dedup("a2-0"),
        },
    )

    r1 = await client.get(f"/v1/finance/transactions?account_id={a1}")
    r2 = await client.get(f"/v1/finance/transactions?account_id={a2}")
    assert len(r1.json()) == 3
    assert len(r2.json()) == 1


@pytest.mark.asyncio
async def test_filter_by_date_range(client, dedup):
    aid = await _create_account(client)
    for i, dt in enumerate(["2026-01-15", "2026-02-15", "2026-03-15"]):
        await client.post(
            "/v1/finance/transactions",
            json={
                "account_id": aid,
                "transaction_date": dt,
                "description": f"txn-{i}",
                "debit": "10.00",
                "source_format": "test_csv",
                "dedup_hash": dedup(f"date-{dt}"),
            },
        )

    # Range février uniquement
    r = await client.get(
        "/v1/finance/transactions?start_date=2026-02-01&end_date=2026-02-28"
    )
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["transaction_date"] == "2026-02-15"


@pytest.mark.asyncio
async def test_invalid_dedup_hash_length(client, dedup):
    aid = await _create_account(client)
    r = await client.post(
        "/v1/finance/transactions",
        json={
            "account_id": aid,
            "transaction_date": "2026-03-15",
            "description": "Bad hash",
            "debit": "10.00",
            "source_format": "test_csv",
            "dedup_hash": "tooshort",  # < 64 chars
        },
    )
    assert r.status_code == 422
