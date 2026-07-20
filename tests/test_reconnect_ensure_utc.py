"""Regression tests: start_match reconnect window must tolerate aware/ISO/missing created_at.

Previously `start_match` computed `datetime.utcnow() - match["created_at"]` directly,
so a timezone-aware or ISO-string created_at raised TypeError and the endpoint 500'd.
"""

from datetime import datetime, timezone

import main

MATCH_ID = "match-reconnect-utc"
PLAYER = "guest-reconnect-player"


def _seed_active_match(created_at, include_created=True):
    match = {
        "_id": MATCH_ID,
        "match_code": "reconnect-code",
        "match_type": "ranked",
        "player1_id": PLAYER,
        "player2_id": "guest-reconnect-opponent",
        "status": "active",
    }
    if include_created:
        match["created_at"] = created_at
    main.in_memory_matches[MATCH_ID] = match
    return match


def _start(client, auth_headers):
    return client.post(
        "/api/game/start",
        json={"mode": "random"},
        headers=auth_headers(PLAYER),
    )


def test_reconnect_with_timezone_aware_created_at(client, auth_headers):
    _seed_active_match(datetime.now(timezone.utc))

    response = _start(client, auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "matched"
    assert body["match_id"] == MATCH_ID


def test_reconnect_with_iso_string_created_at(client, auth_headers):
    _seed_active_match(datetime.now(timezone.utc).isoformat())

    response = _start(client, auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "matched"
    assert body["match_id"] == MATCH_ID


def test_reconnect_with_naive_created_at_still_works(client, auth_headers):
    _seed_active_match(datetime.utcnow())

    response = _start(client, auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "matched"
    assert body["match_id"] == MATCH_ID


def test_missing_created_at_does_not_500(client, auth_headers):
    match = _seed_active_match(None, include_created=False)

    response = _start(client, auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    # Missing timestamp is treated as an old match: it gets abandoned and the
    # player falls through to matchmaking instead of being trapped.
    assert body["status"] in {"searching", "matched"}
    assert match["status"] == "abandoned"


def test_none_created_at_does_not_500(client, auth_headers):
    match = _seed_active_match(None, include_created=True)

    response = _start(client, auth_headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] in {"searching", "matched"}
    assert match["status"] == "abandoned"
