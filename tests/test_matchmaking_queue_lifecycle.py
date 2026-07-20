"""Matchmaking queue lifecycle regressions: cancel flags, ghosts, and races."""

import asyncio
from datetime import datetime, timedelta

from bson import ObjectId

import main


def test_cancel_then_requeue_then_opponent_arrives_matches(client, auth_headers):
    player_a = "guest-lifecycle-aaa"
    player_b = "guest-lifecycle-bbb"

    # A queues up, then cancels.
    resp = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(player_a))
    assert resp.json()["status"] == "searching"
    resp = client.post("/api/game/cancel", headers=auth_headers(player_a))
    assert resp.json()["status"] == "cancelled"

    # A changes their mind and queues again; the stale cancel flag must not
    # poison the next pairing.
    resp = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(player_a))
    assert resp.json()["status"] == "searching"

    resp = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(player_b))
    body = resp.json()
    assert body["status"] == "matched", body
    match = main.in_memory_matches[body["match_id"]]
    assert {str(match["player1_id"]), str(match["player2_id"])} == {player_a, player_b}


def test_stray_cancel_when_never_queued_does_not_poison_first_pairing(client, auth_headers):
    player_a = "guest-stray-aaa"
    player_b = "guest-stray-bbb"

    # Cancel without ever entering the queue must not plant a cancel flag.
    resp = client.post("/api/game/cancel", headers=auth_headers(player_a))
    assert resp.json()["status"] == "cancelled"
    assert player_a not in main.cancelled_users

    resp = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(player_a))
    assert resp.json()["status"] == "searching"

    resp = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(player_b))
    body = resp.json()
    assert body["status"] == "matched", body


def test_reconnect_to_recent_match_removes_user_from_queue(client, auth_headers):
    player_a = "guest-reconnect-aaa"
    player_b = "guest-reconnect-bbb"
    searcher = "guest-reconnect-ccc"

    main.in_memory_matches["match-77"] = {
        "_id": "match-77",
        "match_code": "code77",
        "match_type": "ranked",
        "player1_id": player_a,
        "player2_id": player_b,
        "status": "active",
        "created_at": datetime.utcnow(),
    }
    # Simulate the race: A is still sitting in the queue while their match
    # already exists.
    main.matchmaking_queue[player_a] = {"elo": 1000, "joined_at": datetime.utcnow()}

    resp = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(player_a))
    body = resp.json()
    assert body["status"] == "matched"
    assert body["match_id"] == "match-77"
    assert player_a not in main.matchmaking_queue

    # A later searcher must not get ghost-paired with the reconnected player.
    resp = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(searcher))
    assert resp.json()["status"] == "searching"


def test_hour_stale_queue_entry_is_not_matched(client, auth_headers):
    stale_player = "guest-stale-aaa"
    searcher = "guest-stale-bbb"

    main.matchmaking_queue[stale_player] = {
        "elo": 1000,
        "joined_at": datetime.utcnow() - timedelta(hours=1),
    }

    resp = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(searcher))
    assert resp.json()["status"] == "searching"
    # The ghost entry is evicted rather than paired.
    assert stale_player not in main.matchmaking_queue
    assert searcher in main.matchmaking_queue


def test_concurrent_pairing_does_not_double_match(mock_mongo, monkeypatch):
    queued_id = str(ObjectId())
    main.matchmaking_queue[queued_id] = {"elo": 1000, "joined_at": datetime.utcnow()}

    async def yielding_find_one(query, *args, **kwargs):
        # Force a real event-loop yield so the second searcher runs while the
        # first is mid-pairing, exactly the old double-match window.
        await asyncio.sleep(0)
        return {"_id": query["_id"], "elo": 1000, "username": "Queued"}

    monkeypatch.setattr(main.users_collection, "find_one", yielding_find_one)

    user_c = {"_id": ObjectId(), "elo": 1000}
    user_d = {"_id": ObjectId(), "elo": 1000}

    async def race():
        return await asyncio.gather(
            main.start_match(main.MatchStart(mode="random"), user_c),
            main.start_match(main.MatchStart(mode="random"), user_d),
        )

    result_c, result_d = asyncio.run(race())
    statuses = sorted([result_c["status"], result_d["status"]])
    assert statuses == ["matched", "searching"], (result_c, result_d)

    # The queued player ended up in exactly one match.
    matches_with_queued = [
        m for m in main.in_memory_matches.values()
        if queued_id in {str(m["player1_id"]), str(m["player2_id"])}
    ]
    assert len(matches_with_queued) == 1
