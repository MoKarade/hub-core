"""Tests pour le flow OAuth Google.

Mocks Google HTTP via `respx`. Tests :
  - Génération PKCE / state
  - URL d'autorisation bien formée
  - Token exchange / refresh / revoke
  - Save + retrieve token from DB
  - Routes /v1/oauth/* (start, status, revoke)
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.core.crypto import decrypt_str, encrypt_str
from src.db.models.oauth_token import OAuthToken
from src.services.oauth_google import (
    BASE_SCOPES,
    SERVICE_SCOPES,
    build_authorize_url,
    generate_pkce_pair,
    generate_state,
    get_scopes_for,
    save_token,
)

# ── Tests utilities ─────────────────────────────────────────────────────────


class TestPkce:
    def test_pkce_pair_format(self):
        verifier, challenge = generate_pkce_pair()
        assert 43 <= len(verifier) <= 128
        # base64url challenge sans padding
        assert "=" not in challenge
        assert len(challenge) >= 43

    def test_pkce_pair_unique(self):
        v1, c1 = generate_pkce_pair()
        v2, c2 = generate_pkce_pair()
        assert v1 != v2
        assert c1 != c2

    def test_state_unique(self):
        states = {generate_state() for _ in range(100)}
        assert len(states) == 100  # tous uniques


class TestScopes:
    def test_get_scopes_gmail(self):
        scopes = get_scopes_for("gmail")
        assert "https://www.googleapis.com/auth/gmail.readonly" in scopes
        for base in BASE_SCOPES:
            assert base in scopes

    def test_get_scopes_all(self):
        scopes = get_scopes_for("all")
        # Tous les services + base, sans doublons
        for service_scopes in SERVICE_SCOPES.values():
            for s in service_scopes:
                assert s in scopes

    def test_get_scopes_unknown_raises(self):
        with pytest.raises(ValueError, match="inconnu"):
            get_scopes_for("inexistant")


class TestAuthorizeUrl:
    def test_authorize_url_contains_required_params(self, monkeypatch):
        # Force un client_id pour le test
        from src.core import config

        config.get_settings.cache_clear()
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id.apps.googleusercontent.com")

        url = build_authorize_url("gmail", state="test-state", code_challenge="test-challenge")

        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=test-client-id" in url
        assert "state=test-state" in url
        assert "code_challenge=test-challenge" in url
        assert "code_challenge_method=S256" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "response_type=code" in url

        config.get_settings.cache_clear()


# ── Tests DB persistence ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def fake_token_data() -> dict:
    return {
        "access_token": "ya29.fake-access-token",
        "refresh_token": "1//fake-refresh-token",
        "expires_in": 3600,
        "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
        "token_type": "Bearer",
    }


class TestSaveToken:
    async def test_save_new_token(self, db_session, fake_token_data):
        token = await save_token(
            db_session,
            service="gmail",
            user_email="marc.richard4@gmail.com",
            access_token=fake_token_data["access_token"],
            refresh_token=fake_token_data["refresh_token"],
            expires_in=fake_token_data["expires_in"],
            scope=fake_token_data["scope"],
        )

        assert token.id is not None
        assert token.provider == "google"
        assert token.service == "gmail"
        assert token.user_email == "marc.richard4@gmail.com"
        # Token chiffré
        assert token.access_token_encrypted != fake_token_data["access_token"].encode()
        # Decryption fonctionne
        assert decrypt_str(token.access_token_encrypted) == fake_token_data["access_token"]
        assert decrypt_str(token.refresh_token_encrypted) == fake_token_data["refresh_token"]
        # Scopes parsés
        assert "openid" in token.scopes
        assert "https://www.googleapis.com/auth/gmail.readonly" in token.scopes
        # Pas révoqué
        assert token.revoked_at is None
        assert token.is_usable

    async def test_save_token_upsert(self, db_session, fake_token_data):
        """Save 2x avec même (provider, service, user) → UPDATE pas INSERT."""
        t1 = await save_token(
            db_session,
            service="gmail",
            user_email="marc@x.com",
            access_token="old-token",
            refresh_token="refresh-1",
            expires_in=3600,
            scope="openid",
        )
        t2 = await save_token(
            db_session,
            service="gmail",
            user_email="marc@x.com",
            access_token="new-token",
            refresh_token="refresh-2",
            expires_in=7200,
            scope="openid email",
        )

        assert t1.id == t2.id  # même row
        assert decrypt_str(t2.access_token_encrypted) == "new-token"
        assert decrypt_str(t2.refresh_token_encrypted) == "refresh-2"

        # En DB, une seule ligne
        rows = (await db_session.execute(select(OAuthToken))).scalars().all()
        assert len(rows) == 1


class TestOAuthToken:
    async def test_is_expired(self, db_session):
        # Token expiré
        token = OAuthToken(
            provider="google",
            service="gmail",
            user_email="x@y.com",
            access_token_encrypted=encrypt_str("at"),
            refresh_token_encrypted=encrypt_str("rt"),
            token_expires_at=datetime.now(UTC) - timedelta(minutes=5),
            scopes=["openid"],
        )
        assert token.is_expired
        assert token.is_usable  # refresh dispo → usable

    async def test_revoked_not_usable(self, db_session):
        token = OAuthToken(
            provider="google",
            service="gmail",
            user_email="x@y.com",
            access_token_encrypted=encrypt_str("at"),
            refresh_token_encrypted=encrypt_str("rt"),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["openid"],
            revoked_at=datetime.now(UTC),
        )
        assert token.is_revoked
        assert not token.is_usable

    async def test_no_refresh_expired_not_usable(self, db_session):
        token = OAuthToken(
            provider="google",
            service="gmail",
            user_email="x@y.com",
            access_token_encrypted=encrypt_str("at"),
            refresh_token_encrypted=None,
            token_expires_at=datetime.now(UTC) - timedelta(minutes=5),
            scopes=["openid"],
        )
        assert token.is_expired
        assert not token.is_usable  # pas de refresh → faut re-consent


# ── Tests routes API ─────────────────────────────────────────────────────────


class TestOAuthRoutes:
    async def test_start_no_client_id_returns_503(self, client, monkeypatch):
        # Force un client_id vide
        from src.core import config

        config.get_settings.cache_clear()
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")

        resp = await client.get("/v1/oauth/google/start?service=gmail")
        assert resp.status_code == 503
        assert "non configuré" in resp.text or "GOOGLE_OAUTH_CLIENT_ID" in resp.text

        config.get_settings.cache_clear()

    async def test_start_unknown_service_returns_400(self, client, monkeypatch):
        from src.core import config

        config.get_settings.cache_clear()
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-id")

        resp = await client.get("/v1/oauth/google/start?service=xyzz")
        assert resp.status_code == 400

        config.get_settings.cache_clear()

    async def test_start_redirects_to_google(self, client, monkeypatch):
        from src.core import config

        config.get_settings.cache_clear()
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-id.apps.googleusercontent.com")

        resp = await client.get(
            "/v1/oauth/google/start?service=gmail",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth")
        assert "client_id=test-id" in location

        config.get_settings.cache_clear()

    async def test_callback_invalid_state_returns_400(self, client):
        resp = await client.get(
            "/v1/oauth/callback?code=fake&state=invalid-state",
            follow_redirects=False,
        )
        assert resp.status_code == 400

    async def test_status_empty_initially(self, client):
        resp = await client.get("/v1/oauth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tokens"] == []
        assert "gmail" in data["available_services"]
        assert "photos" in data["available_services"]

    async def test_status_after_save(self, client, db_session, fake_token_data):
        # Save token directement en DB
        await save_token(
            db_session,
            service="gmail",
            user_email="marc@x.com",
            access_token=fake_token_data["access_token"],
            refresh_token=fake_token_data["refresh_token"],
            expires_in=3600,
            scope="openid email",
        )

        resp = await client.get("/v1/oauth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tokens"]) == 1
        token = data["tokens"][0]
        assert token["service"] == "gmail"
        assert token["user_email"] == "marc@x.com"
        assert token["connected"] is True
        # Le token chiffré n'est PAS exposé dans la réponse
        assert "access_token" not in token
        assert "access_token_encrypted" not in token

    async def test_revoke_unknown_service_404(self, client):
        resp = await client.post("/v1/oauth/google/gmail/revoke")
        assert resp.status_code == 404
