"""
Regression tests: serialize mutating match writes with the per-match lock.

Without the lock, concurrent correct answers (after a DB round reload), concurrent
give-ups, and concurrent friend joins corrupt scores / seats.
"""

import asyncio
import copy

import main


PLAYER_A = "guest-lock-aaa"
PLAYER_B = "guest-lock-bbb"
PLAYER_C = "guest-lock-ccc"


def _start_friend_match(client, auth_headers):
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
    return match_id, match_code


def _open_round(client, auth_headers, match_id):
    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_concurrent_correct_answers_via_db_reload_only_one_scores(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id, _ = _start_friend_match(client, auth_headers)
    _open_round(client, auth_headers, match_id)
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    stale_round = copy.deepcopy(main.in_memory_rounds[round_id])
    del main.in_memory_rounds[round_id]

    async def slow_find_one(query, *args, **kwargs):
        await asyncio.sleep(0.01)
        if query.get("_id") == round_id:
            return copy.deepcopy(stale_round)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", slow_find_one)

    async def both_answer():
        return await asyncio.gather(
            main.submit_answer(
                main.AnswerSubmit(match_id=match_id, answer="2*x"),
                current_user={"_id": PLAYER_A, "elo": 1000},
            ),
            main.submit_answer(
                main.AnswerSubmit(match_id=match_id, answer="2*x"),
                current_user={"_id": PLAYER_B, "elo": 1000},
            ),
        )

    first, second = asyncio.run(both_answer())
    # Exactly one point total for the round.
    match = main.in_memory_matches[match_id]
    assert match["player1_score"] + match["player2_score"] == 1
    assert {match["player1_score"], match["player2_score"]} == {0, 1}
    correct_count = sum(1 for body in (first, second) if body.get("correct") is True)
    assert correct_count == 1
    winner_id = PLAYER_A if match["player1_score"] == 1 else PLAYER_B
    assert str(main.in_memory_rounds[round_id]["winner_id"]) == winner_id
    assert {str(first.get("round_winner")), str(second.get("round_winner"))} == {
        winner_id
    }


def test_concurrent_give_ups_resolve_as_tie(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id, _ = _start_friend_match(client, auth_headers)
    _open_round(client, auth_headers, match_id)
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    stale_round = copy.deepcopy(main.in_memory_rounds[round_id])
    del main.in_memory_rounds[round_id]

    async def slow_find_one(query, *args, **kwargs):
        await asyncio.sleep(0.01)
        if query.get("_id") == round_id:
            return copy.deepcopy(stale_round)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", slow_find_one)

    async def both_give_up():
        return await asyncio.gather(
            main.give_up_round(match_id, current_user={"_id": PLAYER_A, "elo": 1000}),
            main.give_up_round(match_id, current_user={"_id": PLAYER_B, "elo": 1000}),
        )

    first, second = asyncio.run(both_give_up())
    statuses = {first["status"], second["status"]}
    assert "both_gave_up" in statuses
    round_doc = main.in_memory_rounds[round_id]
    assert round_doc.get("winner_id") == "tie"
    assert round_doc.get("player1_gave_up") is True
    assert round_doc.get("player2_gave_up") is True


def test_concurrent_friend_joins_only_one_becomes_player2(
    client, auth_headers, monkeypatch
):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    )
    assert created.status_code == 200
    match_id = created.json()["match_id"]
    match_code = created.json()["match_code"]
    waiting = copy.deepcopy(main.in_memory_matches[match_id])

    async def slow_find_one(query, *args, **kwargs):
        await asyncio.sleep(0.01)
        if query.get("match_code") == match_code.upper() or query.get("_id") == match_id:
            # Serve a waiting snapshot so both callers pass the pre-lock status check.
            return copy.deepcopy(waiting)
        return None

    monkeypatch.setattr(main.matches_collection, "find_one", slow_find_one)

    async def both_join():
        return await asyncio.gather(
            main.join_friend_match(
                main.FriendMatchJoin(match_code=match_code),
                current_user={"_id": PLAYER_B, "elo": 1000},
            ),
            main.join_friend_match(
                main.FriendMatchJoin(match_code=match_code),
                current_user={"_id": PLAYER_C, "elo": 1100},
            ),
            return_exceptions=True,
        )

    results = asyncio.run(both_join())
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    match = main.in_memory_matches[match_id]
    assert match["status"] == "active"
    assert str(match["player2_id"]) in {PLAYER_B, PLAYER_C}


def test_wrong_answer_racing_correct_answer_keeps_single_winner(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id, _ = _start_friend_match(client, auth_headers)
    _open_round(client, auth_headers, match_id)
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    stale_round = copy.deepcopy(main.in_memory_rounds[round_id])
    del main.in_memory_rounds[round_id]

    async def slow_find_one(query, *args, **kwargs):
        await asyncio.sleep(0.01)
        if query.get("_id") == round_id:
            return copy.deepcopy(stale_round)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", slow_find_one)

    async def race():
        return await asyncio.gather(
            main.submit_answer(
                main.AnswerSubmit(match_id=match_id, answer="999"),
                current_user={"_id": PLAYER_B, "elo": 1000},
            ),
            main.submit_answer(
                main.AnswerSubmit(match_id=match_id, answer="2*x"),
                current_user={"_id": PLAYER_A, "elo": 1000},
            ),
        )

    wrong, correct = asyncio.run(race())
    assert correct.get("correct") is True
    assert str(correct.get("round_winner")) == PLAYER_A
    assert wrong.get("correct") is False
    match = main.in_memory_matches[match_id]
    assert match["player1_score"] == 1
    assert match["player2_score"] == 0
    assert str(main.in_memory_rounds[round_id]["winner_id"]) == PLAYER_A
