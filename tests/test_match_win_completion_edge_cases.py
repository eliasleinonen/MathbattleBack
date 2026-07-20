"""
Edge-case tests for first-to-3 WIN COMPLETION in people-vs-people matches
(friend + ranked), main.py: submit_answer ~1609 (completion block ~1953-1997),
get_question ~1362, get_active_match ~1271.

Scope (niche completion mechanics, complementing the ELO-math suite):
- every reachable completion scoreline (3-0, 3-1, 3-2) for friend AND ranked
- winner_id correctness when player2 is the one who closes the match
- the match-status transition to "completed" happens exactly once, and the
  completion write persists winner_id/elo_change to matches_collection
- post-completion lockout: further answers and further questions are 400
- ELO applied exactly once on ranked (spying users_collection.update_one),
  and never for friend matches
- winning via the very last answer of a 2-2 match
- the match-point race: both players at 2 points submit a correct answer
  concurrently (in-memory path is safe; the DB-reload path double-completes
  the match and pays ELO twice - new xfail)
- elo_change magnitude mirrors calculate_elo_change on the stored snapshots
- /api/game/active flips to false for both players after completion
- an immediate rematch after completion starts clean and leaves the old
  match untouched

Conventions (same as the sibling edge-case files):
- Guest identities via "Bearer guest-xxx" tokens.
- Known bugs are documented with strict xfail markers plus sibling tests
  pinning the CURRENT behavior. See MATCH_EDGE_CASE_REPORT.md.
"""

import asyncio
import copy

import pytest
from fastapi import HTTPException

import main
from main import calculate_elo_change


PLAYER_A = "guest-wincomp-aaa"
PLAYER_B = "guest-wincomp-bbb"
CORRECT = "2*x"


# ---------------------------------------------------------------------------
# spies
# ---------------------------------------------------------------------------


class _SpyResult:
    modified_count = 1
    matched_count = 1
    upserted_id = None


class UpdateSpy:
    """Records every update_one call made against a collection."""

    def __init__(self):
        self.calls = []

    async def update_one(self, filt, update, upsert=False):
        self.calls.append(
            {"filter": copy.deepcopy(filt), "update": copy.deepcopy(update)}
        )
        return _SpyResult()

    def inc_calls(self):
        return [c for c in self.calls if "$inc" in c["update"]]

    def completed_status_calls(self):
        return [
            c
            for c in self.calls
            if c["update"].get("$set", {}).get("status") == "completed"
        ]


@pytest.fixture
def users_spy(mock_mongo, monkeypatch):
    spy = UpdateSpy()
    monkeypatch.setattr(main.users_collection, "update_one", spy.update_one)
    return spy


@pytest.fixture
def matches_spy(mock_mongo, monkeypatch):
    spy = UpdateSpy()
    monkeypatch.setattr(main.matches_collection, "update_one", spy.update_one)
    return spy


# ---------------------------------------------------------------------------
# gameplay helpers
# ---------------------------------------------------------------------------


def _start(client, auth_headers, player):
    response = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ranked_match(client, auth_headers, player1=PLAYER_A, player2=PLAYER_B):
    """Queue player2 first so `player1` lands in the player1 slot."""
    assert _start(client, auth_headers, player2)["status"] == "searching"
    body = _start(client, auth_headers, player1)
    assert body["status"] == "matched", body
    return body["match_id"]


def _friend_match(client, auth_headers, player1=PLAYER_A, player2=PLAYER_B):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(player1)
    )
    assert created.status_code == 200, created.text
    body = created.json()
    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": body["match_code"]},
        headers=auth_headers(player2),
    )
    assert joined.status_code == 200, joined.text
    return body["match_id"]


def _make_match(client, auth_headers, match_type):
    if match_type == "friend":
        return _friend_match(client, auth_headers)
    return _ranked_match(client, auth_headers)


def _question(client, auth_headers, match_id, player):
    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    return response


def _answer(client, auth_headers, match_id, player, answer=CORRECT):
    return client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )


def _win_round(client, auth_headers, match_id, player):
    q = _question(client, auth_headers, match_id, player)
    assert q.status_code == 200, q.text
    r = _answer(client, auth_headers, match_id, player)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["correct"] is True, body
    return body


def _play_to_completion(client, auth_headers, match_id, winners):
    """Play the given per-round winner sequence; return the final response."""
    body = None
    for index, winner in enumerate(winners):
        body = _win_round(client, auth_headers, match_id, winner)
        if index < len(winners) - 1:
            assert body["match_winner"] is None, body
    return body


def _set_score(match_id, player, score):
    match = main.in_memory_matches[match_id]
    key = (
        "player1_score"
        if str(match["player1_id"]) == str(player)
        else "player2_score"
    )
    match[key] = score


def _status(client, auth_headers, match_id, player):
    response = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. every completion scoreline: 3-0, 3-1, 3-2, friend and ranked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("match_type", ["friend", "ranked"])
@pytest.mark.parametrize("loser_points", [0, 1, 2])
def test_completion_scorelines(
    client, auth_headers, fixed_question, users_spy, match_type, loser_points
):
    match_id = _make_match(client, auth_headers, match_type)
    winners = [PLAYER_A, PLAYER_B] * loser_points + [PLAYER_A] * (
        3 - loser_points
    )
    final = _play_to_completion(client, auth_headers, match_id, winners)

    assert final["player1_score"] == 3
    assert final["player2_score"] == loser_points
    assert final["match_winner"] == PLAYER_A
    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["winner_id"] == PLAYER_A
    assert match["player1_score"] == 3
    assert match["player2_score"] == loser_points


# ---------------------------------------------------------------------------
# 2. winner_id when player2 closes the match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("match_type", ["friend", "ranked"])
def test_player2_win_sets_winner_id_to_player2(
    client, auth_headers, fixed_question, users_spy, match_type
):
    match_id = _make_match(client, auth_headers, match_type)
    final = _play_to_completion(
        client, auth_headers, match_id, [PLAYER_B, PLAYER_B, PLAYER_B]
    )

    assert final["match_winner"] == PLAYER_B
    assert final["player1_score"] == 0
    assert final["player2_score"] == 3
    match = main.in_memory_matches[match_id]
    assert match["winner_id"] == PLAYER_B
    assert match["winner_id"] != match["player1_id"]
    for viewer in (PLAYER_A, PLAYER_B):
        assert _status(client, auth_headers, match_id, viewer)["winner_id"] == PLAYER_B


# ---------------------------------------------------------------------------
# 3 + 10. completed exactly once, and completion persists to the DB
# ---------------------------------------------------------------------------


def test_status_becomes_completed_exactly_once(
    client, auth_headers, fixed_question, users_spy, matches_spy
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    assert len(matches_spy.completed_status_calls()) == 1

    # Rejected post-completion traffic must not re-write the status.
    assert _answer(client, auth_headers, match_id, PLAYER_B).status_code == 400
    assert _question(client, auth_headers, match_id, PLAYER_B).status_code == 400
    for viewer in (PLAYER_A, PLAYER_B):
        _status(client, auth_headers, match_id, viewer)
    assert len(matches_spy.completed_status_calls()) == 1


def test_ranked_completion_persists_winner_and_elo_to_db(
    client, auth_headers, fixed_question, users_spy, matches_spy
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    (call,) = matches_spy.completed_status_calls()
    assert call["filter"] == {"_id": match_id}
    written = call["update"]["$set"]
    assert written["status"] == "completed"
    assert written["winner_id"] == PLAYER_A
    assert written["elo_change"] == 20
    assert "updated_at" in written


def test_friend_completion_persists_with_zero_elo_change(
    client, auth_headers, fixed_question, users_spy, matches_spy
):
    match_id = _friend_match(client, auth_headers)
    _set_score(match_id, PLAYER_B, 2)
    _win_round(client, auth_headers, match_id, PLAYER_B)

    (call,) = matches_spy.completed_status_calls()
    assert call["filter"] == {"_id": match_id}
    written = call["update"]["$set"]
    assert written["winner_id"] == PLAYER_B
    assert written["elo_change"] == 0


# ---------------------------------------------------------------------------
# 4 + 5. post-completion lockout: answers and questions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("match_type", ["friend", "ranked"])
def test_answers_after_completion_rejected_for_both_players(
    client, auth_headers, fixed_question, users_spy, match_type
):
    match_id = _make_match(client, auth_headers, match_type)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    for player in (PLAYER_A, PLAYER_B):
        response = _answer(client, auth_headers, match_id, player)
        assert response.status_code == 400
        assert response.json()["detail"] == "Match is already completed"
        # Even a wrong answer is gated before grading.
        wrong = _answer(client, auth_headers, match_id, player, answer="nope")
        assert wrong.status_code == 400


@pytest.mark.parametrize("match_type", ["friend", "ranked"])
def test_questions_after_completion_rejected_for_both_players(
    client, auth_headers, fixed_question, users_spy, match_type
):
    match_id = _make_match(client, auth_headers, match_type)
    _set_score(match_id, PLAYER_B, 2)
    _win_round(client, auth_headers, match_id, PLAYER_B)

    for player in (PLAYER_A, PLAYER_B):
        response = _question(client, auth_headers, match_id, player)
        assert response.status_code == 400
        assert response.json()["detail"] == "Match is already completed"


def test_post_completion_traffic_leaves_result_intact(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _set_score(match_id, PLAYER_B, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    for player in (PLAYER_A, PLAYER_B):
        _answer(client, auth_headers, match_id, player)
        _question(client, auth_headers, match_id, player)

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["winner_id"] == PLAYER_A
    assert match["player1_score"] == 3
    assert match["player2_score"] == 2
    assert match["elo_change"] == 20


# ---------------------------------------------------------------------------
# 6 + 7. ELO applied once on ranked, never on friend (update_one spy)
# ---------------------------------------------------------------------------


def test_ranked_completion_applies_elo_exactly_once(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _ranked_match(client, auth_headers)
    _play_to_completion(
        client, auth_headers, match_id, [PLAYER_A, PLAYER_A, PLAYER_A]
    )

    incs = users_spy.inc_calls()
    assert len(incs) == 2
    assert incs[0]["filter"] == {"_id": PLAYER_A}
    assert incs[0]["update"] == {"$inc": {"elo": 20, "wins": 1}}
    assert incs[1]["filter"] == {"_id": PLAYER_B}
    assert incs[1]["update"] == {"$inc": {"elo": -20, "losses": 1}}

    # Post-completion retries by either player add nothing.
    _answer(client, auth_headers, match_id, PLAYER_A)
    _answer(client, auth_headers, match_id, PLAYER_B)
    assert len(users_spy.inc_calls()) == 2


def test_ranked_no_elo_writes_before_the_final_round(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _ranked_match(client, auth_headers)
    for winner in (PLAYER_A, PLAYER_B, PLAYER_A, PLAYER_B):
        _win_round(client, auth_headers, match_id, winner)
        assert users_spy.inc_calls() == []
    _win_round(client, auth_headers, match_id, PLAYER_A)
    assert len(users_spy.inc_calls()) == 2


def test_friend_completion_never_touches_users_collection(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _friend_match(client, auth_headers)
    _play_to_completion(
        client, auth_headers, match_id, [PLAYER_B, PLAYER_B, PLAYER_B]
    )

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert users_spy.calls == []


def test_friend_post_completion_polls_never_touch_users_collection(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _friend_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    for _ in range(3):
        for viewer in (PLAYER_A, PLAYER_B):
            _status(client, auth_headers, match_id, viewer)
    assert users_spy.calls == []


# ---------------------------------------------------------------------------
# 8. winning via the last answer of a 2-2 match
# ---------------------------------------------------------------------------


def test_ranked_win_via_last_answer_after_two_all(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _ranked_match(client, auth_headers)
    for winner in (PLAYER_A, PLAYER_B, PLAYER_A, PLAYER_B):
        _win_round(client, auth_headers, match_id, winner)
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (2, 2)
    assert match["status"] == "active"

    final = _win_round(client, auth_headers, match_id, PLAYER_B)
    assert final["match_winner"] == PLAYER_B
    assert final["player1_score"] == 2
    assert final["player2_score"] == 3
    assert final["elo_change"] == 20
    assert match["status"] == "completed"
    assert match["winner_id"] == PLAYER_B


def test_friend_win_via_last_answer_after_two_all(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _friend_match(client, auth_headers)
    for winner in (PLAYER_B, PLAYER_A, PLAYER_B, PLAYER_A):
        _win_round(client, auth_headers, match_id, winner)

    final = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert final["match_winner"] == PLAYER_A
    assert final["player1_score"] == 3
    assert final["player2_score"] == 2
    assert final["elo_change"] == 0
    assert users_spy.calls == []


# ---------------------------------------------------------------------------
# 9. match-point race: both players at 2 submit correct concurrently
# ---------------------------------------------------------------------------


def test_race_at_match_point_in_memory_completes_once(
    client, auth_headers, fixed_question, users_spy
):
    """
    Both players sit at 2 points and submit a correct answer "concurrently"
    (two coroutines on one loop, round doc in memory). The in-memory path has
    no await between the completed-status check and the round/score writes,
    so the first submitter runs to completion and the second is bounced by
    the completed gate with a 400 - the match completes exactly once and ELO
    is paid exactly once.
    """
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _set_score(match_id, PLAYER_B, 2)
    assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 200

    async def submit_concurrently():
        data = main.AnswerSubmit(match_id=match_id, answer=CORRECT)
        return await asyncio.gather(
            main.submit_answer(data, current_user={"_id": PLAYER_A}),
            main.submit_answer(data, current_user={"_id": PLAYER_B}),
            return_exceptions=True,
        )

    result_a, result_b = asyncio.run(submit_concurrently())

    assert result_a["correct"] is True
    assert result_a["match_winner"] == PLAYER_A
    assert isinstance(result_b, HTTPException)
    assert result_b.status_code == 400
    assert result_b.detail == "Match is already completed"

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["winner_id"] == PLAYER_A
    assert (match["player1_score"], match["player2_score"]) == (3, 2)
    assert len(users_spy.inc_calls()) == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(match-point-db-race-double-elo): submit_answer holds no match "
        "lock, so when the round doc is re-read from the DB (memory miss + "
        "any latency) BOTH match-point submitters pass the completed/"
        "winner_id checks, both score, and BOTH run the completion block. "
        "The match is 'won' twice, users_collection receives FOUR $inc "
        "updates instead of two (winner paid double, loser drained double), "
        "and the final score is an impossible 3-3. The second completion "
        "even credits player1 as the winner because player1_score >= 3 is "
        "checked first."
    ),
)
def test_race_at_match_point_via_db_reload_should_pay_elo_once(
    client, auth_headers, fixed_question, users_spy, monkeypatch
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _set_score(match_id, PLAYER_B, 2)
    assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 200
    round_id = main.in_memory_matches[match_id]["current_round_id"]

    # Simulate a worker whose round cache was lost (restart/eviction).
    snapshot = copy.deepcopy(main.in_memory_rounds.pop(round_id))

    async def db_find_one_with_latency(query, *args, **kwargs):
        await asyncio.sleep(0)  # any real DB round-trip yields at least once
        if query.get("_id") == round_id:
            return copy.deepcopy(snapshot)
        return None

    monkeypatch.setattr(
        main.rounds_collection, "find_one", db_find_one_with_latency
    )

    async def submit_concurrently():
        data = main.AnswerSubmit(match_id=match_id, answer=CORRECT)
        return await asyncio.gather(
            main.submit_answer(data, current_user={"_id": PLAYER_A}),
            main.submit_answer(data, current_user={"_id": PLAYER_B}),
            return_exceptions=True,
        )

    asyncio.run(submit_concurrently())

    match = main.in_memory_matches[match_id]
    assert match["player1_score"] + match["player2_score"] == 5  # currently 6
    assert len(users_spy.inc_calls()) == 2  # currently 4


def test_current_behavior_db_reload_race_double_completes_and_double_pays(
    client, auth_headers, fixed_question, users_spy, monkeypatch
):
    # BUG: pins the current behavior of the xfail above.
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _set_score(match_id, PLAYER_B, 2)
    assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 200
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    snapshot = copy.deepcopy(main.in_memory_rounds.pop(round_id))

    async def db_find_one_with_latency(query, *args, **kwargs):
        await asyncio.sleep(0)
        if query.get("_id") == round_id:
            return copy.deepcopy(snapshot)
        return None

    monkeypatch.setattr(
        main.rounds_collection, "find_one", db_find_one_with_latency
    )

    async def submit_concurrently():
        data = main.AnswerSubmit(match_id=match_id, answer=CORRECT)
        return await asyncio.gather(
            main.submit_answer(data, current_user={"_id": PLAYER_A}),
            main.submit_answer(data, current_user={"_id": PLAYER_B}),
            return_exceptions=True,
        )

    result_a, result_b = asyncio.run(submit_concurrently())

    # Both racers scored the same round and both "won" the match...
    assert result_a["correct"] is True and result_b["correct"] is True
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (3, 3)
    # ...and both completions credit player1 (player1_score >= 3 wins the
    # tie-break), so player2's own match point evaporates.
    assert result_a["match_winner"] == PLAYER_A
    assert result_b["match_winner"] == PLAYER_A
    assert match["winner_id"] == PLAYER_A

    # ELO paid twice: four $inc updates, winner +40 net, loser -40 net.
    incs = users_spy.inc_calls()
    assert len(incs) == 4
    winner_incs = [c for c in incs if c["filter"] == {"_id": PLAYER_A}]
    loser_incs = [c for c in incs if c["filter"] == {"_id": PLAYER_B}]
    assert sum(c["update"]["$inc"]["elo"] for c in winner_incs) == 40
    assert sum(c["update"]["$inc"].get("wins", 0) for c in winner_incs) == 2
    assert sum(c["update"]["$inc"]["elo"] for c in loser_incs) == -40
    assert sum(c["update"]["$inc"].get("losses", 0) for c in loser_incs) == 2


# ---------------------------------------------------------------------------
# 11. elo_change magnitude mirrors the stored snapshots
# ---------------------------------------------------------------------------


def test_elo_change_magnitude_even_snapshots(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    final = _win_round(client, auth_headers, match_id, PLAYER_A)

    assert final["elo_change"] == calculate_elo_change(1000, 1000) == 20
    assert main.in_memory_matches[match_id]["elo_change"] == 20
    # Both players see the same (winner-perspective) magnitude via status.
    for viewer in (PLAYER_A, PLAYER_B):
        assert _status(client, auth_headers, match_id, viewer)["elo_change"] == 20


def test_elo_change_magnitude_underdog_win(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = 1000
    main.in_memory_matches[match_id]["player2_elo"] = 1400
    _set_score(match_id, PLAYER_A, 2)
    final = _win_round(client, auth_headers, match_id, PLAYER_A)

    assert final["elo_change"] == calculate_elo_change(1000, 1400) == 36
    incs = users_spy.inc_calls()
    assert incs[0]["update"]["$inc"]["elo"] == 36
    assert incs[1]["update"]["$inc"]["elo"] == -36


def test_elo_change_magnitude_favorite_win(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = 1400
    main.in_memory_matches[match_id]["player2_elo"] = 1000
    _set_score(match_id, PLAYER_A, 2)
    final = _win_round(client, auth_headers, match_id, PLAYER_A)

    assert final["elo_change"] == calculate_elo_change(1400, 1000) == 3


def test_friend_elo_change_is_zero_everywhere(
    client, auth_headers, fixed_question, users_spy
):
    match_id = _friend_match(client, auth_headers)
    _set_score(match_id, PLAYER_B, 2)
    final = _win_round(client, auth_headers, match_id, PLAYER_B)

    assert final["elo_change"] == 0
    assert main.in_memory_matches[match_id]["elo_change"] == 0
    for viewer in (PLAYER_A, PLAYER_B):
        assert _status(client, auth_headers, match_id, viewer)["elo_change"] == 0


# ---------------------------------------------------------------------------
# 12. /api/game/active flips to false after completion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("match_type", ["friend", "ranked"])
def test_active_match_false_after_completion(
    client, auth_headers, fixed_question, users_spy, match_type
):
    match_id = _make_match(client, auth_headers, match_type)

    for player in (PLAYER_A, PLAYER_B):
        body = client.get(
            "/api/game/active", headers=auth_headers(player)
        ).json()
        assert body["has_active_match"] is True
        assert body["match_id"] == match_id

    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    for player in (PLAYER_A, PLAYER_B):
        body = client.get(
            "/api/game/active", headers=auth_headers(player)
        ).json()
        assert body == {"has_active_match": False}


# ---------------------------------------------------------------------------
# 13. rematch after completion starts clean
# ---------------------------------------------------------------------------


def test_ranked_rematch_after_completion_is_clean(
    client, auth_headers, fixed_question, users_spy
):
    first_id = _ranked_match(client, auth_headers)
    _set_score(first_id, PLAYER_A, 2)
    _win_round(client, auth_headers, first_id, PLAYER_A)

    # The completed match is not offered as a reconnect; a fresh queue+join
    # forms a brand-new match with zeroed state.
    assert _start(client, auth_headers, PLAYER_B)["status"] == "searching"
    rematch = _start(client, auth_headers, PLAYER_A)
    assert rematch["status"] == "matched"
    second_id = rematch["match_id"]
    assert second_id != first_id

    second = main.in_memory_matches[second_id]
    assert second["status"] == "active"
    assert second["winner_id"] is None
    assert (second["player1_score"], second["player2_score"]) == (0, 0)
    assert second["elo_change"] == 0

    # The rematch is playable and scores independently of the old match.
    body = _win_round(client, auth_headers, second_id, PLAYER_B)
    assert body["player2_score"] == 1
    first = main.in_memory_matches[first_id]
    assert first["status"] == "completed"
    assert first["winner_id"] == PLAYER_A
    assert (first["player1_score"], first["player2_score"]) == (3, 0)


def test_friend_rematch_after_completion_is_clean(
    client, auth_headers, fixed_question, users_spy
):
    first_id = _friend_match(client, auth_headers)
    _set_score(first_id, PLAYER_A, 2)
    _win_round(client, auth_headers, first_id, PLAYER_A)

    second_id = _friend_match(client, auth_headers)
    assert second_id != first_id
    second = main.in_memory_matches[second_id]
    assert second["status"] == "active"
    assert (second["player1_score"], second["player2_score"]) == (0, 0)

    # Play the rematch to a different winner; both results stand separately.
    final = _play_to_completion(
        client, auth_headers, second_id, [PLAYER_B, PLAYER_B, PLAYER_B]
    )
    assert final["match_winner"] == PLAYER_B
    assert main.in_memory_matches[second_id]["winner_id"] == PLAYER_B
    assert main.in_memory_matches[first_id]["winner_id"] == PLAYER_A
    assert users_spy.calls == []
