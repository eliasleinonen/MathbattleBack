"""
Ensures unexpected server errors never leak internal details to the client.

The global exception handler should return a generic message and a 500 status
while the real error is only logged server-side. Deliberate HTTPExceptions must
keep their own intentional, user-facing detail.
"""

import pytest
from fastapi.testclient import TestClient

import main

GUEST = {"Authorization": "Bearer guest-erx1"}


@pytest.fixture
def client_no_reraise(mock_mongo):
    # raise_server_exceptions=False lets us observe the actual 500 response
    # the handler produces, instead of re-raising inside the test.
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_unhandled_exception_returns_generic_message(client_no_reraise, monkeypatch):
    secret_detail = "internal-db-password-leak-should-not-appear"

    def _boom(*_args, **_kwargs):
        raise RuntimeError(secret_detail)

    # /api/users/search calls users_collection.find(...) without a try/except,
    # so a driver-level error bubbles up to the global handler.
    monkeypatch.setattr(main.users_collection, "find", _boom)

    res = client_no_reraise.get("/api/users/search?username=ab", headers=GUEST)
    assert res.status_code == 500
    assert res.json()["detail"] == "Something went wrong. Please try again."
    assert secret_detail not in res.text


def test_known_httpexception_detail_is_preserved(client_no_reraise):
    # A deliberate 404 must keep its intended, friendly message.
    res = client_no_reraise.get("/api/game/status/does-not-exist", headers=GUEST)
    assert res.status_code == 404
    assert res.json()["detail"] == "Match not found"
