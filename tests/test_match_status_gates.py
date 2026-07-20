"""Match status gating: only active matches accept gameplay requests.

Also covers ranked /start not hijacking or abandoning friend matches, and
abandonment being persisted to Mongo.
"""

from datetime import datetime, timedelta

import main


PLAYER_A = "guest-gates-aaa"
PLAYER_B = "guest-gates-bbb"


def _create_friend_match(client, auth_headers, player_id=PLAYER_A):
    response = client.post(
        "/api/game/friend/create",
        json={},
        headers=auth_headers(player_id),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _join_friend_match(client, auth_headers, match_code, player_id=PLAYER_B):
    response = client.post(
        "/api/game/friend/join",
        json={"match_code": match_code},
        headers=auth_headers(player_id),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _plant_match(match_id, status, match_type="friend"):
    """Insert a match doc directly into in-memory state with a given status."""
    match_doc = {
        "_id": match_id,
        "match_code": f"CODE-{match_id}",
        "match_type": match_type,
        "player1_id": PLAYER_A,
        "player2_id": PLAYER_B,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": 1000,
        "player2_elo": 1000,
        "status": status,
        "winner_id": None,
        "elo_change": 0,
        "rounds": [],
        "created_at": datetime.utcnow(),
    }
    main.in_memory_matches[match_id] = match_doc
    return match_doc


def _assert_gameplay_rejected(client, auth_headers, match_id, detail=None):
    question = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert question.status_code == 400, question.text

    answer = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_A),
    )
    assert answer.status_code == 400, answer.text

    give_up = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert give_up.status_code == 400, give_up.text

    if detail is not None:
        assert question.json()["detail"] == detail
        assert answer.json()["detail"] == detail
        assert give_up.json()["detail"] == detail


def test_waiting_friend_match_rejects_gameplay(client, auth_headers):
    created = _create_friend_match(client, auth_headers)
    assert created["status"] == "waiting"

    _assert_gameplay_rejected(
        client, auth_headers, created["match_id"], detail="Match is not active"
    )
    assert main.in_memory_matches[created["match_id"]]["status"] == "waiting"


def test_pending_challenge_rejects_gameplay(client, auth_headers):
    _plant_match("match-pending-1", status="pending")

    _assert_gameplay_rejected(
        client, auth_headers, "match-pending-1", detail="Match is not active"
    )


def test_abandoned_match_rejects_gameplay(client, auth_headers):
    _plant_match("match-abandoned-1", status="abandoned")

    _assert_gameplay_rejected(
        client, auth_headers, "match-abandoned-1", detail="Match is not active"
    )


def test_completed_match_rejects_gameplay(client, auth_headers):
    _plant_match("match-completed-1", status="completed")

    _assert_gameplay_rejected(
        client,
        auth_headers,
        "match-completed-1",
        detail="Match is already completed",
    )


def test_active_friend_match_serves_question(client, auth_headers, fixed_question):
    created = _create_friend_match(client, auth_headers)
    _join_friend_match(client, auth_headers, created["match_code"])

    question = client.get(
        "/api/game/question",
        params={"match_id": created["match_id"]},
        headers=auth_headers(PLAYER_A),
    )
    assert question.status_code == 200, question.text
    assert question.json()["round_id"]


def test_ranked_start_does_not_hijack_fresh_active_friend_match(
    client, auth_headers
):
    created = _create_friend_match(client, auth_headers)
    _join_friend_match(client, auth_headers, created["match_code"])
    match_id = created["match_id"]

    # Friend match is < 5 seconds old; previously /start returned it as a
    # ranked "matched" result.
    start = client.post(
        "/api/game/start",
        json={"mode": "random"},
        headers=auth_headers(PLAYER_A),
    )
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["status"] == "searching"
    assert body.get("match_id") != match_id
    assert main.in_memory_matches[match_id]["status"] == "active"


def test_ranked_start_does_not_abandon_stale_active_friend_match(
    client, auth_headers
):
    created = _create_friend_match(client, auth_headers)
    _join_friend_match(client, auth_headers, created["match_code"])
    match_id = created["match_id"]

    # Age the friend match past the 5-second reconnect window.
    main.in_memory_matches[match_id]["created_at"] = (
        datetime.utcnow() - timedelta(seconds=30)
    )

    start = client.post(
        "/api/game/start",
        json={"mode": "random"},
        headers=auth_headers(PLAYER_A),
    )
    assert start.status_code == 200, start.text
    assert start.json()["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "active"


def test_stale_ranked_match_abandon_is_persisted(client, auth_headers, monkeypatch):
    stale = _plant_match("match-stale-ranked", status="active", match_type="ranked")
    stale["created_at"] = datetime.utcnow() - timedelta(seconds=30)

    update_calls = []

    async def _spy_update_one(filter_doc, update_doc, *args, **kwargs):
        update_calls.append((filter_doc, update_doc))

    monkeypatch.setattr(main.matches_collection, "update_one", _spy_update_one)

    start = client.post(
        "/api/game/start",
        json={"mode": "random"},
        headers=auth_headers(PLAYER_A),
    )
    assert start.status_code == 200, start.text
    assert start.json()["status"] == "searching"

    assert main.in_memory_matches["match-stale-ranked"]["status"] == "abandoned"
    abandon_calls = [
        (filter_doc, update_doc)
        for filter_doc, update_doc in update_calls
        if filter_doc == {"_id": "match-stale-ranked"}
        and update_doc.get("$set", {}).get("status") == "abandoned"
    ]
    assert abandon_calls, f"no abandoned update_one persisted; calls: {update_calls}"
    assert "updated_at" in abandon_calls[0][1]["$set"]
