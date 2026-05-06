"""Tests endpoints /v1/privacy/* — Loi 25 / PIPEDA / RGPD removal tracker."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_privacy_summary_empty(client) -> None:
    """Sans aucune demande, summary retourne tous les counts a 0."""
    r = await client.get("/v1/privacy/summary")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "total": 0,
        "draft": 0,
        "sent": 0,
        "overdue": 0,
        "resolved": 0,
        "refused": 0,
    }


@pytest.mark.asyncio
async def test_privacy_create_draft_request(client) -> None:
    """POST /requests cree un draft + genere subject + body FR avec specifiques legaux."""
    r = await client.post(
        "/v1/privacy/requests",
        json={
            "company_name": "Spokeo",
            "request_type": "deletion",
            "legal_basis": "loi25",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["company_name"] == "Spokeo"
    assert body["request_type"] == "deletion"
    assert body["legal_basis"] == "loi25"
    assert body["status"] == "draft"
    assert body["sent_at"] is None
    assert body["deadline_at"] is None
    # Email genere : subject + body avec mention Loi 25 + 30 jours
    assert "Loi 25" in body["subject"]
    assert "30 jours" in body["body"]
    assert "Marc Richard" in body["body"]


@pytest.mark.asyncio
async def test_privacy_status_transition_sent_calculates_deadline(client) -> None:
    """Quand on passe une demande de draft -> sent, deadline_at = sent_at + 30j."""
    r = await client.post(
        "/v1/privacy/requests",
        json={"company_name": "Acxiom", "request_type": "access"},
    )
    req_id = r.json()["id"]

    r = await client.patch(
        f"/v1/privacy/requests/{req_id}",
        json={"status": "sent"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "sent"
    assert body["sent_at"] is not None
    assert body["deadline_at"] is not None


@pytest.mark.asyncio
async def test_privacy_filter_by_status(client) -> None:
    """GET /requests?status_filter=draft retourne uniquement les drafts."""
    await client.post(
        "/v1/privacy/requests",
        json={"company_name": "BeenVerified", "request_type": "deletion"},
    )
    r = await client.post(
        "/v1/privacy/requests",
        json={"company_name": "Whitepages", "request_type": "deletion"},
    )
    req_id_2 = r.json()["id"]
    await client.patch(
        f"/v1/privacy/requests/{req_id_2}",
        json={"status": "sent"},
    )

    r = await client.get("/v1/privacy/requests?status_filter=draft")
    assert r.status_code == 200
    drafts = r.json()
    assert len(drafts) == 1
    assert drafts[0]["company_name"] == "BeenVerified"

    r = await client.get("/v1/privacy/requests?status_filter=sent")
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_privacy_invalid_status_filter_400(client) -> None:
    """Filtre status invalide => 400."""
    r = await client.get("/v1/privacy/requests?status_filter=foo")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_privacy_delete_request(client) -> None:
    """DELETE /requests/{id} supprime."""
    r = await client.post(
        "/v1/privacy/requests",
        json={"company_name": "TruePeopleSearch", "request_type": "deletion"},
    )
    req_id = r.json()["id"]

    r = await client.delete(f"/v1/privacy/requests/{req_id}")
    assert r.status_code == 204

    r = await client.get(f"/v1/privacy/requests/{req_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_privacy_templates_endpoint(client) -> None:
    """GET /templates expose les constantes legales pour le frontend."""
    r = await client.get("/v1/privacy/templates")
    assert r.status_code == 200
    body = r.json()
    assert body["deadline_days"] == 30
    assert "loi25" in [b["value"] for b in body["legal_bases"]]
    assert "deletion" in body["request_types"]
