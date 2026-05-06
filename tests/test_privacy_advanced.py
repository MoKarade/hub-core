"""Tests avances /v1/privacy : workflow complet + edge cases."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_privacy_full_lifecycle(client) -> None:
    """draft -> sent -> data_deleted : transitions completes."""
    # Create
    r = await client.post(
        "/v1/privacy/requests",
        json={"company_name": "TestCorp", "request_type": "deletion"},
    )
    req_id = r.json()["id"]

    # draft -> sent
    r = await client.patch(
        f"/v1/privacy/requests/{req_id}",
        json={"status": "sent"},
    )
    body = r.json()
    assert body["status"] == "sent"
    assert body["sent_at"] is not None
    assert body["deadline_at"] is not None

    # sent -> data_deleted
    r = await client.patch(
        f"/v1/privacy/requests/{req_id}",
        json={"status": "data_deleted"},
    )
    body = r.json()
    assert body["status"] == "data_deleted"
    assert body["resolved_at"] is not None


@pytest.mark.asyncio
async def test_privacy_summary_after_workflow(client) -> None:
    """Apres draft + sent + resolved, summary reflete les counts."""
    # Cree 3 requetes
    for company in ["A", "B", "C"]:
        await client.post(
            "/v1/privacy/requests",
            json={"company_name": company, "request_type": "deletion"},
        )

    # Passe B et C en sent
    rs = await client.get("/v1/privacy/requests?status_filter=draft")
    requests_drafts = rs.json()
    assert len(requests_drafts) == 3

    for r_data in requests_drafts[:2]:
        await client.patch(
            f"/v1/privacy/requests/{r_data['id']}",
            json={"status": "sent"},
        )

    # Passe l'un des sent en data_deleted
    rs = await client.get("/v1/privacy/requests?status_filter=sent")
    sent_reqs = rs.json()
    if sent_reqs:
        await client.patch(
            f"/v1/privacy/requests/{sent_reqs[0]['id']}",
            json={"status": "data_deleted"},
        )

    r = await client.get("/v1/privacy/summary")
    s = r.json()
    assert s["total"] == 3
    assert s["draft"] == 1
    assert s["sent"] == 1
    assert s["resolved"] == 1


@pytest.mark.asyncio
async def test_privacy_create_with_extra_emails(client) -> None:
    """extra_emails sont inclus dans le body genere."""
    r = await client.post(
        "/v1/privacy/requests",
        json={
            "company_name": "Test",
            "request_type": "access",
            "extra_emails": ["other1@example.com", "other2@example.com"],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert "other1@example.com" in body["body"]
    assert "other2@example.com" in body["body"]


@pytest.mark.asyncio
async def test_privacy_legal_basis_pipeda(client) -> None:
    """legal_basis=pipeda genere le bon template."""
    r = await client.post(
        "/v1/privacy/requests",
        json={
            "company_name": "TestCanada",
            "legal_basis": "pipeda",
            "request_type": "deletion",
        },
    )
    body = r.json()
    assert "PIPEDA" in body["subject"]


@pytest.mark.asyncio
async def test_privacy_invalid_legal_basis(client) -> None:
    """legal_basis invalide => 422 Pydantic."""
    r = await client.post(
        "/v1/privacy/requests",
        json={"company_name": "Test", "legal_basis": "fakelaw"},
    )
    assert r.status_code == 422
