"""Liveness endpoint used by uptime monitors."""


def test_health_get_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_head_is_allowed(client):
    response = client.head("/health")
    assert response.status_code == 200
    # HEAD must not include a response body
    assert response.content == b""


def test_root_still_get_only(client):
    assert client.get("/").status_code == 200
    assert client.head("/").status_code == 405
