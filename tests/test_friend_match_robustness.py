"""
Regression tests for friend-match robustness:

- concurrent next-round requests must not fork the match into two rounds
- round timestamps must be timezone-aware so browsers parse them correctly
- presence tracking: status reports whether the opponent is still polling
- give-up must not dead-lock when the opponent has left the match
"""

import asyncio
import itertools
from datetime import datetime, timedelta

import main


PLAYER_A = "guest-player-aaa"
PLAYER_B = "guest-player-bbb"


def _start_match(client, auth_headers):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    )
    assert created.status_code == 200, created.text
    match_code = created.json()["match_code"]
    match_id = created.json()["match_id"]

    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": match_code},
        headers=auth_headers(PLAYER_B),
    )
    assert joined.status_code == 200, joined.text
    return match_id


def _get_question(client, auth_headers, match_id, player_id):
    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player_id),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _mark_stale(match_id, player_id, seconds=60):
    """Backdate a player's heartbeat so the backend treats them as gone."""
    match = main.in_memory_matches[match_id]
    match.setdefault("player_last_seen", {})[player_id] = main.utc_now() - timedelta(
        seconds=seconds
    )


def test_simultaneous_next_round_requests_return_the_same_round(
    client, auth_headers, monkeypatch
):
    """
    After a round is won, both clients ask for the next question at nearly the
    same time. Both must get the SAME round; before the per-match lock each
    request created its own round and the players saw different questions.
    """
    numbers = itertools.count(1)

    def numbered_question(_elo):
        return {
            "expression": f"question-{next(numbers)}",
            "derivative": "2·x",
            "evaluate_at": 0,
            "answer": "2·x",
            "difficulty": 1,
            "ask_for_derivative_only": True,
        }

    monkeypatch.setattr(main, "generate_question", numbered_question)

    match_id = _start_match(client, auth_headers)
    _get_question(client, auth_headers, match_id, PLAYER_A)

    won = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_A),
    )
    assert won.status_code == 200 and won.json()["correct"] is True

    # Force real interleaving: yield to the event loop inside the round
    # creation path so, without the lock, both requests overlap.
    async def yielding_find_one(*args, **kwargs):
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", yielding_find_one)
    monkeypatch.setattr(main.matches_collection, "find_one", yielding_find_one)

    async def request_next_round_concurrently():
        return await asyncio.gather(
            main.get_question(match_id, current_user={"_id": PLAYER_A}),
            main.get_question(match_id, current_user={"_id": PLAYER_B}),
        )

    first, second = asyncio.run(request_next_round_concurrently())

    assert first["round_id"] == second["round_id"]
    assert first["expression"] == second["expression"]

    match_rounds = [
        r for r in main.in_memory_rounds.values() if r["match_id"] == match_id
    ]
    assert len(match_rounds) == 2  # round 1 (won) + one shared round 2


def test_round_start_time_is_timezone_aware(client, auth_headers, fixed_question):
    match_id = _start_match(client, auth_headers)
    question = _get_question(client, auth_headers, match_id, PLAYER_A)

    parsed = datetime.fromisoformat(question["round_start_time"])
    assert parsed.tzinfo is not None, (
        "round_start_time must carry a UTC offset; naive strings are parsed "
        "as local time by browsers and break the countdown"
    )


def test_resumed_question_includes_ask_for_derivative_only(
    client, auth_headers, fixed_question
):
    match_id = _start_match(client, auth_headers)
    _get_question(client, auth_headers, match_id, PLAYER_A)

    resumed = _get_question(client, auth_headers, match_id, PLAYER_B)
    assert resumed["ask_for_derivative_only"] is True


def test_status_reports_opponent_connected(client, auth_headers, fixed_question):
    match_id = _start_match(client, auth_headers)
    _get_question(client, auth_headers, match_id, PLAYER_A)

    # Both players poll: each sees the other as connected.
    for player in (PLAYER_A, PLAYER_B):
        status = client.get(
            f"/api/game/status/{match_id}", headers=auth_headers(player)
        )
        assert status.status_code == 200
        assert status.json()["opponent_connected"] is True

    # Opponent's heartbeat goes stale -> reported as disconnected.
    _mark_stale(match_id, PLAYER_B)
    status = client.get(f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_A))
    assert status.json()["opponent_connected"] is False


def test_give_up_advances_round_when_opponent_left(
    client, auth_headers, fixed_question
):
    match_id = _start_match(client, auth_headers)
    first_round = _get_question(client, auth_headers, match_id, PLAYER_A)

    # Opponent connected: giving up alone just waits.
    client.get(f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_B))
    waiting = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert waiting.status_code == 200
    assert waiting.json()["status"] == "gave_up"

    # Opponent gone: give-up resolves the round as a tie so play continues.
    _mark_stale(match_id, PLAYER_B)
    resolved = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "both_gave_up"
    assert resolved.json()["round_winner"] == "tie"

    next_round = _get_question(client, auth_headers, match_id, PLAYER_A)
    assert next_round["round_id"] != first_round["round_id"]
