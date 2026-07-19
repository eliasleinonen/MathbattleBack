"""
Tests for Google OAuth sign-in (the only account-based auth path).

The Google token verification is stubbed via ``main.verify_google_token`` so the
tests never reach Google's servers; they exercise our upsert + JWT issuance and
confirm the issued JWT resolves back to the stored user.
"""

import pytest

import main


@pytest.fixture
def google_configured(monkeypatch):
    """Pretend a Google client id is configured and stub token verification."""
    monkeypatch.setattr(main, "GOOGLE_CLIENT_ID", "test-client-id")

    def _fake_verify(token: str) -> dict:
        if token == "bad-token":
            raise ValueError("invalid token")
        if token == "wrong-issuer":
            raise main.GoogleAuthError("Wrong issuer.")
        if token == "no-email":
            return {"name": "No Email", "email_verified": True}
        if token == "unverified-email":
            return {"email": "player@example.com", "name": "Test Player", "email_verified": False}
        return {"email": "player@example.com", "name": "Test Player", "email_verified": True}

    monkeypatch.setattr(main, "verify_google_token", _fake_verify)


def _decode(token: str) -> dict:
    return main.jwt.decode(token, main.SECRET_KEY, algorithms=[main.ALGORITHM])


def test_google_auth_new_user_is_created(client, google_configured, monkeypatch):
    inserted = {}

    async def _find_one(_query):
        return None

    async def _insert_one(doc):
        inserted.update(doc)
        return None

    monkeypatch.setattr(main.users_collection, "find_one", _find_one)
    monkeypatch.setattr(main.users_collection, "insert_one", _insert_one)

    res = client.post("/api/auth/google", json={"token": "good-token"})
    assert res.status_code == 200
    body = res.json()
    assert body["token_type"] == "bearer"
    assert _decode(body["access_token"])["sub"] == "player@example.com"

    # New user persisted with sane defaults.
    assert inserted["email"] == "player@example.com"
    assert inserted["name"] == "Test Player"
    assert inserted["elo"] == 1000
    assert inserted["wins"] == 0
    assert inserted["losses"] == 0


def test_google_auth_existing_user_is_not_recreated(client, google_configured, monkeypatch):
    async def _find_one(_query):
        return {
            "_id": main.ObjectId(),
            "email": "player@example.com",
            "name": "Existing",
            "elo": 1500,
        }

    async def _insert_one(_doc):
        raise AssertionError("insert_one should not be called for an existing user")

    monkeypatch.setattr(main.users_collection, "find_one", _find_one)
    monkeypatch.setattr(main.users_collection, "insert_one", _insert_one)

    res = client.post("/api/auth/google", json={"token": "good-token"})
    assert res.status_code == 200
    assert _decode(res.json()["access_token"])["sub"] == "player@example.com"


def test_google_auth_rejects_invalid_token(client, google_configured):
    res = client.post("/api/auth/google", json={"token": "bad-token"})
    assert res.status_code == 401


def test_google_auth_rejects_wrong_issuer(client, google_configured):
    res = client.post("/api/auth/google", json={"token": "wrong-issuer"})
    assert res.status_code == 401


def test_google_auth_rejects_token_without_email(client, google_configured):
    res = client.post("/api/auth/google", json={"token": "no-email"})
    assert res.status_code == 401


def test_google_auth_rejects_unverified_email(client, google_configured, monkeypatch):
    async def _insert_one(_doc):
        raise AssertionError("must not create a user for an unverified email")

    monkeypatch.setattr(main.users_collection, "insert_one", _insert_one)
    res = client.post("/api/auth/google", json={"token": "unverified-email"})
    assert res.status_code == 401


def test_google_auth_unavailable_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(main, "GOOGLE_CLIENT_ID", "")
    res = client.post("/api/auth/google", json={"token": "good-token"})
    assert res.status_code == 503


def test_issued_jwt_resolves_to_stored_user_on_protected_route(client, monkeypatch):
    """A Google-issued JWT must authenticate against get_current_user via Mongo."""
    stored = {
        "_id": main.ObjectId(),
        "email": "player@example.com",
        "name": "Test Player",
        "elo": 1234,
        "wins": 2,
        "losses": 1,
    }

    async def _find_one(query):
        return stored if query.get("email") == "player@example.com" else None

    monkeypatch.setattr(main.users_collection, "find_one", _find_one)

    token = main.create_access_token({"sub": "player@example.com"})
    res = client.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    profile = res.json()
    assert profile["email"] == "player@example.com"
    assert profile["elo"] == 1234


def test_password_endpoints_removed(client):
    """Email/password auth no longer exists (checked at the /api paths the client uses)."""
    assert client.post("/api/auth/register", json={"email": "a@b.com", "password": "x", "name": "A"}).status_code == 404
    assert client.post("/api/auth/login", json={"email": "a@b.com", "password": "x"}).status_code == 404
