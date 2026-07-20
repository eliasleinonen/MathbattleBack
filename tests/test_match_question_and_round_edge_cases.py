"""
Edge-case tests for question serving and round progression in human matches
(main.py: get_question ~1363, _create_next_round ~1420, _question_response
~1348).

Scope:
- difficulty selection uses the LOWER of the two players' ELO snapshots
- resume semantics: the same active round is returned to both players until
  it has a winner; a new round is only created after a winner (or tie)
- deterministic round ids / round_number increments, including mixed
  win + tie sequences and a full first-to-3 match
- concurrency: simultaneous get_question before any round exists and right
  after a round is won (the per-match lock must prevent round forks)
- response shape: ask_for_derivative_only / evaluate_at / expression always
  present, round_start_time is timezone-aware ISO ~3s in the future
- error paths: unknown / empty / malformed match ids, outsiders, completed
  and abandoned matches, missing query param
- bot-vs-human differences: time_limit only on bot rounds, ELO-bracketed
  base times, difficulty driven by the bot's (lower) ELO
- generate_question blowing up or returning garbage (global 500 handler,
  no half-created round state)
- the 5-minute stale-round timeout (tie + fresh round)
- Mongo rounds-array bookkeeping (round_number desync after ties)

Known bugs are documented with strict xfail markers plus a companion test
pinning current behavior; see MATCH_EDGE_CASE_REPORT.md for the summary.
"""

import asyncio
import copy
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import main


PLAYER_A = "guest-question-aaa"
PLAYER_B = "guest-question-bbb"
OUTSIDER = "guest-question-ccc"

CORRECT = "2*x"  # matches fixed_question's answer "2·x"


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _friend_match(client, auth_headers, p1=PLAYER_A, p2=PLAYER_B):
    """Create + join a friend match, return match_id (p1 is player1)."""
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(p1)
    )
    assert created.status_code == 200, created.text
    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": created.json()["match_code"]},
        headers=auth_headers(p2),
    )
    assert joined.status_code == 200, joined.text
    return created.json()["match_id"]


def _bot_match(client, auth_headers, player=PLAYER_A):
    """Queue `player`, backdate past the 10s window, poll again -> bot match."""
    first = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    assert first.json()["status"] == "searching"
    main.matchmaking_queue[player]["joined_at"] -= timedelta(seconds=11)
    second = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    body = second.json()
    assert body["status"] == "matched", body
    assert main.in_memory_matches[body["match_id"]]["player2_id"] == "bot-opponent"
    return body["match_id"]


def _question(client, auth_headers, match_id, player):
    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _answer(client, auth_headers, match_id, player, answer):
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _win_round(client, auth_headers, match_id, player):
    """Start (or resume) a round and have `player` answer it correctly."""
    _question(client, auth_headers, match_id, player)
    body = _answer(client, auth_headers, match_id, player, CORRECT)
    assert body["correct"] is True, body
    return body


def _give_up(client, auth_headers, match_id, player):
    response = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _tie_round(client, auth_headers, match_id):
    """Both players give up the current round -> winner_id 'tie'."""
    _question(client, auth_headers, match_id, PLAYER_A)
    _question(client, auth_headers, match_id, PLAYER_B)
    first = _give_up(client, auth_headers, match_id, PLAYER_A)
    assert first["status"] == "gave_up"
    second = _give_up(client, auth_headers, match_id, PLAYER_B)
    assert second["status"] == "both_gave_up"


def _match_rounds(match_id):
    return [r for r in main.in_memory_rounds.values() if r["match_id"] == match_id]


@pytest.fixture
def question_spy(mock_mongo, monkeypatch):
    """Deterministic generate_question that records the ELO it was called with."""
    calls = []

    def _generate(elo: int) -> dict:
        calls.append(elo)
        return {
            "expression": "x^2",
            "derivative": "2·x",
            "evaluate_at": 3,
            "answer": "2·x",
            "difficulty": 1,
            "ask_for_derivative_only": True,
        }

    monkeypatch.setattr(main, "generate_question", _generate)
    return calls


@pytest.fixture
def match_update_calls(mock_mongo, monkeypatch):
    """Record every matches_collection.update_one(query, update) call."""
    calls = []

    async def _update(query, update, *args, **kwargs):
        calls.append((copy.deepcopy(query), copy.deepcopy(update)))
        return type(
            "R", (), {"modified_count": 1, "matched_count": 1, "upserted_id": None}
        )()

    monkeypatch.setattr(main.matches_collection, "update_one", _update)
    return calls


@pytest.fixture
def client_no_reraise(mock_mongo):
    # raise_server_exceptions=False lets us observe the 500 the global
    # exception handler produces instead of re-raising inside the test.
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


def _pushed_round_summaries(calls):
    return [update["$push"]["rounds"] for _q, update in calls if "$push" in update]


# ---------------------------------------------------------------------------
# question difficulty uses the lower of the two ELOs (cases 1, 22)
# ---------------------------------------------------------------------------


def test_difficulty_uses_lower_elo_when_player2_is_weaker(
    client, auth_headers, question_spy
):
    match_id = _friend_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = 1600
    main.in_memory_matches[match_id]["player2_elo"] = 900

    _question(client, auth_headers, match_id, PLAYER_A)
    assert question_spy == [900]


def test_difficulty_uses_lower_elo_when_player1_is_weaker(
    client, auth_headers, question_spy
):
    match_id = _friend_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = 950
    main.in_memory_matches[match_id]["player2_elo"] = 1700

    _question(client, auth_headers, match_id, PLAYER_B)
    assert question_spy == [950]


def test_equal_elos_pass_that_elo_through(client, auth_headers, question_spy):
    match_id = _friend_match(client, auth_headers)  # both guests: 1000 / 1000
    _question(client, auth_headers, match_id, PLAYER_A)
    assert question_spy == [1000]


def test_widely_differing_elos_lower_player_sets_difficulty(
    client, auth_headers, question_spy
):
    # Case 22: a 2500 pro matched with an 800 novice gets novice questions.
    match_id = _friend_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = 2500
    main.in_memory_matches[match_id]["player2_elo"] = 800

    _question(client, auth_headers, match_id, PLAYER_A)
    assert question_spy == [800]
    assert main.in_memory_rounds[f"round-{match_id}-1"]["difficulty"] == 1


def test_real_generator_low_elo_gives_easy_or_medium(client, auth_headers):
    # No spy: exercise the real generate_question for a 1000-ELO pair.
    match_id = _friend_match(client, auth_headers)
    body = _question(client, auth_headers, match_id, PLAYER_A)

    round_doc = main.in_memory_rounds[body["round_id"]]
    assert round_doc["difficulty"] in (1, 2)  # elo < 1200 branch
    assert isinstance(body["expression"], str) and body["expression"]
    assert body["evaluate_at"] in range(1, 6)
    assert isinstance(round_doc["answer"], str) and round_doc["answer"]


def test_real_generator_high_elo_gives_hard(client, auth_headers):
    match_id = _friend_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = 2000
    main.in_memory_matches[match_id]["player2_elo"] = 2100

    body = _question(client, auth_headers, match_id, PLAYER_A)
    assert main.in_memory_rounds[body["round_id"]]["difficulty"] == 3


# ---------------------------------------------------------------------------
# resume semantics: same round while active (case 2)
# ---------------------------------------------------------------------------


def test_both_players_get_the_same_active_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)

    first = _question(client, auth_headers, match_id, PLAYER_A)
    second = _question(client, auth_headers, match_id, PLAYER_B)

    assert first["round_id"] == second["round_id"] == f"round-{match_id}-1"
    assert first["expression"] == second["expression"]
    assert first["evaluate_at"] == second["evaluate_at"]
    assert first["round_start_time"] == second["round_start_time"]
    assert len(_match_rounds(match_id)) == 1


def test_repeated_polling_by_same_player_returns_identical_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A)
    for _ in range(3):
        again = _question(client, auth_headers, match_id, PLAYER_A)
        assert again == first


def test_wrong_answers_do_not_advance_the_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A)

    wrong = _answer(client, auth_headers, match_id, PLAYER_A, "999")
    assert wrong["correct"] is False

    again = _question(client, auth_headers, match_id, PLAYER_A)
    assert again["round_id"] == first["round_id"]
    assert len(_match_rounds(match_id)) == 1


# ---------------------------------------------------------------------------
# round progression: next round only after a winner (cases 3, 4)
# ---------------------------------------------------------------------------


def test_new_round_only_after_round_has_a_winner(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A)

    win = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert win["correct"] is True
    assert str(win["round_winner"]) == PLAYER_A

    # The finished round stays "current" until someone polls /question again.
    assert main.in_memory_matches[match_id]["current_round_id"] == first["round_id"]

    nxt = _question(client, auth_headers, match_id, PLAYER_B)
    assert nxt["round_id"] == f"round-{match_id}-2"
    assert nxt["round_id"] != first["round_id"]


def test_round_number_increments_across_rounds(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)

    _win_round(client, auth_headers, match_id, PLAYER_A)
    second = _question(client, auth_headers, match_id, PLAYER_A)
    _answer(client, auth_headers, match_id, PLAYER_B, CORRECT)
    third = _question(client, auth_headers, match_id, PLAYER_B)

    assert second["round_id"] == f"round-{match_id}-2"
    assert third["round_id"] == f"round-{match_id}-3"
    assert main.in_memory_rounds[second["round_id"]]["round_number"] == 2
    assert main.in_memory_rounds[third["round_id"]]["round_number"] == 3


def test_round_numbering_is_independent_per_match(
    client, auth_headers, fixed_question
):
    # Round ids/counts derive from in_memory_rounds filtered by match_id, so
    # interleaved matches must not bleed into each other's numbering.
    match_a = _friend_match(client, auth_headers)
    match_b = _friend_match(
        client, auth_headers, p1="guest-question-ddd", p2="guest-question-eee"
    )

    qa1 = _question(client, auth_headers, match_a, PLAYER_A)
    qb1 = _question(client, auth_headers, match_b, "guest-question-ddd")
    _answer(client, auth_headers, match_a, PLAYER_A, CORRECT)
    qa2 = _question(client, auth_headers, match_a, PLAYER_A)

    assert qa1["round_id"] == f"round-{match_a}-1"
    assert qa2["round_id"] == f"round-{match_a}-2"
    assert qb1["round_id"] == f"round-{match_b}-1"
    # Match B is untouched by match A's progress.
    resumed_b = _question(client, auth_headers, match_b, "guest-question-eee")
    assert resumed_b["round_id"] == qb1["round_id"]


def test_question_after_both_gave_up_starts_fresh_round(
    client, auth_headers, fixed_question
):
    # Case 18: a double give-up ties the round; polling again starts round 2
    # and nobody scored.
    match_id = _friend_match(client, auth_headers)
    _tie_round(client, auth_headers, match_id)

    assert main.in_memory_rounds[f"round-{match_id}-1"]["winner_id"] == "tie"

    nxt = _question(client, auth_headers, match_id, PLAYER_A)
    assert nxt["round_id"] == f"round-{match_id}-2"
    assert main.in_memory_rounds[nxt["round_id"]]["round_number"] == 2

    match = main.in_memory_matches[match_id]
    assert match["player1_score"] == 0
    assert match["player2_score"] == 0


def test_mixed_wins_and_ties_keep_round_numbers_increasing(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)

    _win_round(client, auth_headers, match_id, PLAYER_A)   # round 1: win
    _tie_round(client, auth_headers, match_id)             # round 2: tie
    third = _question(client, auth_headers, match_id, PLAYER_A)

    assert third["round_id"] == f"round-{match_id}-3"
    assert main.in_memory_rounds[third["round_id"]]["round_number"] == 3


def test_full_match_to_three_wins_has_unique_sequential_rounds(
    client, auth_headers, fixed_question
):
    # Case 20: play a whole match; every round id is unique and sequential,
    # and the completed match refuses to serve a 4th question.
    match_id = _friend_match(client, auth_headers)
    round_ids = []

    for expected_number in (1, 2, 3):
        qa = _question(client, auth_headers, match_id, PLAYER_A)
        qb = _question(client, auth_headers, match_id, PLAYER_B)
        assert qa["round_id"] == qb["round_id"]
        assert qa["round_id"] == f"round-{match_id}-{expected_number}"
        round_ids.append(qa["round_id"])
        win = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
        assert win["correct"] is True

    assert len(set(round_ids)) == 3
    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert str(match["winner_id"]) == PLAYER_A

    denied = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_B),
    )
    assert denied.status_code == 400
    assert denied.json()["detail"] == "Match is already completed"


# ---------------------------------------------------------------------------
# concurrency (cases 5, 6)
# ---------------------------------------------------------------------------


def test_concurrent_first_questions_create_exactly_one_round(
    client, auth_headers, fixed_question
):
    # Case 5: both players ask for the very first question at the same time.
    # The per-match lock must make one request create the round and the other
    # resume it.
    match_id = _friend_match(client, auth_headers)
    main.match_locks.pop(match_id, None)  # fresh lock for this event loop

    async def ask_concurrently():
        return await asyncio.gather(
            main.get_question(match_id, current_user={"_id": PLAYER_A}),
            main.get_question(match_id, current_user={"_id": PLAYER_B}),
        )

    result_a, result_b = asyncio.run(ask_concurrently())

    assert result_a["round_id"] == result_b["round_id"] == f"round-{match_id}-1"
    assert result_a["expression"] == result_b["expression"]
    assert len(_match_rounds(match_id)) == 1


def test_concurrent_questions_after_win_create_exactly_one_next_round(
    client, auth_headers, fixed_question
):
    # Case 6: right after a round is won both clients poll for the next
    # question simultaneously.  Without the lock this used to fork the match
    # into two different "current" rounds.
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    main.match_locks.pop(match_id, None)  # fresh lock for this event loop

    async def ask_concurrently():
        return await asyncio.gather(
            main.get_question(match_id, current_user={"_id": PLAYER_A}),
            main.get_question(match_id, current_user={"_id": PLAYER_B}),
        )

    result_a, result_b = asyncio.run(ask_concurrently())

    assert result_a["round_id"] == result_b["round_id"] == f"round-{match_id}-2"
    assert len(_match_rounds(match_id)) == 2
    assert main.in_memory_matches[match_id]["current_round_id"] == f"round-{match_id}-2"
    assert main.in_memory_rounds[f"round-{match_id}-2"]["winner_id"] is None


def test_concurrent_questions_from_same_player_share_the_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    main.match_locks.pop(match_id, None)

    async def ask_concurrently():
        return await asyncio.gather(
            main.get_question(match_id, current_user={"_id": PLAYER_A}),
            main.get_question(match_id, current_user={"_id": PLAYER_A}),
        )

    first, second = asyncio.run(ask_concurrently())
    assert first["round_id"] == second["round_id"]
    assert len(_match_rounds(match_id)) == 1


# ---------------------------------------------------------------------------
# response shape (cases 7, 8, 19)
# ---------------------------------------------------------------------------


def test_question_response_has_all_required_fields(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    body = _question(client, auth_headers, match_id, PLAYER_A)

    assert set(body) == {
        "round_id",
        "expression",
        "evaluate_at",
        "ask_for_derivative_only",
        "round_start_time",
    }
    assert body["expression"] == "x^2"
    assert body["evaluate_at"] == 0
    assert body["ask_for_derivative_only"] is True
    # The answer/derivative never leak to the client.
    assert "answer" not in body
    assert "derivative" not in body


def test_ask_for_derivative_only_defaults_true_when_generator_omits_it(
    client, auth_headers, monkeypatch
):
    # Case 7: even a generator that forgets the flag yields a response (and a
    # stored round) with ask_for_derivative_only defaulted to True.
    def _generate(_elo: int) -> dict:
        return {
            "expression": "x^3",
            "derivative": "3·x^2",
            "evaluate_at": 2,
            "answer": "3·x^2",
            "difficulty": 2,
            # ask_for_derivative_only intentionally missing
        }

    monkeypatch.setattr(main, "generate_question", _generate)
    match_id = _friend_match(client, auth_headers)
    body = _question(client, auth_headers, match_id, PLAYER_A)

    assert body["ask_for_derivative_only"] is True
    assert main.in_memory_rounds[body["round_id"]]["ask_for_derivative_only"] is True


def test_ask_for_derivative_only_false_is_passed_through(
    client, auth_headers, monkeypatch
):
    def _generate(_elo: int) -> dict:
        return {
            "expression": "x^3",
            "derivative": "3·x^2",
            "evaluate_at": 2,
            "answer": 12,
            "difficulty": 2,
            "ask_for_derivative_only": False,
        }

    monkeypatch.setattr(main, "generate_question", _generate)
    match_id = _friend_match(client, auth_headers)

    created = _question(client, auth_headers, match_id, PLAYER_A)
    resumed = _question(client, auth_headers, match_id, PLAYER_B)
    assert created["ask_for_derivative_only"] is False
    assert resumed["ask_for_derivative_only"] is False


def test_round_start_time_is_timezone_aware_iso_three_seconds_ahead(
    client, auth_headers, fixed_question
):
    # Case 19: the creation response carries an ISO string with a UTC offset
    # aimed ~3 seconds into the future so both clients start in sync.
    match_id = _friend_match(client, auth_headers)
    before = main.utc_now()
    body = _question(client, auth_headers, match_id, PLAYER_A)
    after = main.utc_now()

    start = datetime.fromisoformat(body["round_start_time"])
    assert start.tzinfo is not None
    assert start.utcoffset() == timedelta(0)
    assert before + timedelta(seconds=2.5) <= start <= after + timedelta(seconds=3.5)


def test_resume_returns_the_same_round_start_time_string(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    created = _question(client, auth_headers, match_id, PLAYER_A)
    resumed = _question(client, auth_headers, match_id, PLAYER_B)

    # The resume path echoes match["round_start_time"], which is exactly the
    # string stored at creation.
    assert resumed["round_start_time"] == created["round_start_time"]
    assert (
        main.in_memory_matches[match_id]["round_start_time"]
        == created["round_start_time"]
    )


# ---------------------------------------------------------------------------
# error paths (cases 9, 10, 11, 12, 16, 17)
# ---------------------------------------------------------------------------


def test_unknown_match_id_is_404(client, auth_headers):
    response = client.get(
        "/api/game/question",
        params={"match_id": "match-does-not-exist"},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Match not found"


def test_outsider_gets_403_not_your_match(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(OUTSIDER),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Not your match"
    # The outsider never becomes "seen" and no round was created for them.
    assert OUTSIDER not in main.in_memory_matches[match_id].get("player_last_seen", {})
    assert len(_match_rounds(match_id)) == 0


def test_completed_match_rejects_new_questions(client, auth_headers, fixed_question):
    # Case 11 (resolved question in the task list): completed matches CANNOT
    # serve questions -- get_question checks status == "completed" explicitly.
    match_id = _friend_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "completed"

    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Match is already completed"


def test_outsider_on_completed_match_still_gets_403_first(
    client, auth_headers, fixed_question
):
    # Membership is checked before the completed check.
    match_id = _friend_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "completed"

    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(OUTSIDER),
    )
    assert response.status_code == 403


def test_current_behavior_abandoned_match_still_serves_questions(
    client, auth_headers, fixed_question
):
    """
    Case 12, the "zombie match" bug: only status "completed" is rejected, so
    an abandoned match keeps serving fresh rounds.  The strict xfail for the
    intended behavior lives in
    test_match_presence_and_lifecycle_edge_cases.py
    (test_abandoned_match_should_not_serve_questions); this test pins the
    round-creation side of it.
    """
    match_id = _friend_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"

    body = _question(client, auth_headers, match_id, PLAYER_A)
    assert body["round_id"] == f"round-{match_id}-1"

    # The zombie match even progresses through rounds normally.
    win = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert win["correct"] is True
    nxt = _question(client, auth_headers, match_id, PLAYER_A)
    assert nxt["round_id"] == f"round-{match_id}-2"


@pytest.mark.parametrize(
    "bad_id",
    [
        "match-999999",           # plausible but never created
        "MATCH-1",                # ids are case-sensitive
        " match-1",               # leading whitespace is not trimmed
        "match-1 ",               # trailing whitespace is not trimmed
        "round-match-1-1",        # a round id is not a match id
        "../etc/passwd",          # path-ish garbage is just a dict miss
        "None",                   # stringified sentinels
        "null",
        "0",
        "🙂🙂🙂",                  # non-ASCII
        "match-1; DROP TABLE",    # injection-ish garbage
    ],
)
def test_invalid_match_id_formats_are_404(client, auth_headers, bad_id):
    # Case 16: match_id is only ever used as a dict key / _id equality, so
    # every malformed shape is a clean 404 -- no 500s, no partial matches.
    response = client.get(
        "/api/game/question",
        params={"match_id": bad_id},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Match not found"


def test_empty_match_id_is_404(client, auth_headers):
    # Case 17: empty string is a valid query value that misses everything.
    response = client.get(
        "/api/game/question",
        params={"match_id": ""},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404


def test_missing_match_id_param_is_422(client, auth_headers):
    response = client.get("/api/game/question", headers=auth_headers(PLAYER_A))
    assert response.status_code == 422


def test_question_loads_match_from_db_on_memory_miss(
    client, auth_headers, fixed_question, monkeypatch
):
    # A match known only to Mongo (worker restart) is loaded, cached and
    # served normally.
    match_doc = {
        "_id": "match-db-only-1",
        "match_type": "friend",
        "player1_id": PLAYER_A,
        "player2_id": PLAYER_B,
        "player1_elo": 1000,
        "player2_elo": 1000,
        "player1_score": 0,
        "player2_score": 0,
        "status": "active",
        "rounds": [],
    }

    async def find_one(query, *args, **kwargs):
        if query.get("_id") == match_doc["_id"]:
            return copy.deepcopy(match_doc)
        return None

    monkeypatch.setattr(main.matches_collection, "find_one", find_one)

    body = _question(client, auth_headers, match_doc["_id"], PLAYER_A)
    assert body["round_id"] == f"round-{match_doc['_id']}-1"
    assert match_doc["_id"] in main.in_memory_matches


# ---------------------------------------------------------------------------
# bot vs human differences (cases 13, 14, 15)
# ---------------------------------------------------------------------------


def test_bot_match_question_has_time_limit(client, auth_headers, question_spy):
    # Case 14: guest user ELO 1000 -> base 15s, spy difficulty 1 -> +1s.
    match_id = _bot_match(client, auth_headers)
    body = _question(client, auth_headers, match_id, PLAYER_A)

    assert body["time_limit"] == 16
    assert main.in_memory_rounds[body["round_id"]]["time_limit"] == 16


def test_bot_match_resume_also_carries_time_limit(
    client, auth_headers, question_spy
):
    match_id = _bot_match(client, auth_headers)
    created = _question(client, auth_headers, match_id, PLAYER_A)
    resumed = _question(client, auth_headers, match_id, PLAYER_A)
    assert resumed["time_limit"] == created["time_limit"] == 16


@pytest.mark.parametrize(
    "user_elo,expected_time_limit",
    [
        (800, 16),    # <=1000 -> 15 + difficulty 1
        (1000, 16),   # inclusive bracket boundary
        (1001, 13),   # <=1400 -> 12 + 1
        (1400, 13),
        (1401, 11),   # <=1800 -> 10 + 1
        (1800, 11),
        (1801, 9),    # >1800 -> 8 + 1
        (2500, 9),
    ],
)
def test_bot_time_limit_brackets_follow_player1_elo(
    client, auth_headers, question_spy, user_elo, expected_time_limit
):
    match_id = _bot_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = user_elo

    body = _question(client, auth_headers, match_id, PLAYER_A)
    assert body["time_limit"] == expected_time_limit


def test_friend_match_question_has_no_time_limit(
    client, auth_headers, fixed_question
):
    # Case 15: human rounds are untimed -- the key is entirely absent, not
    # null, so clients can key off its presence.
    match_id = _friend_match(client, auth_headers)
    body = _question(client, auth_headers, match_id, PLAYER_A)
    assert "time_limit" not in body
    assert "time_limit" not in main.in_memory_rounds[body["round_id"]]


def test_ranked_human_match_question_has_no_time_limit(
    client, auth_headers, fixed_question
):
    # match_type "ranked" (human vs human) never enters the bot branch.
    searching = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_A)
    )
    assert searching.json()["status"] == "searching"
    matched = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_B)
    )
    match_id = matched.json()["match_id"]

    body = _question(client, auth_headers, match_id, PLAYER_B)
    assert "time_limit" not in body


def test_bot_match_difficulty_driven_by_bot_elo_not_user_elo(
    client, auth_headers, question_spy
):
    # Case 13: the bot spawns 50-150 ELO below the user, so min() picks the
    # BOT's elo for question difficulty -- while time_limit uses the USER's
    # elo.  Two different ELOs feed one round.
    match_id = _bot_match(client, auth_headers)
    match = main.in_memory_matches[match_id]
    assert match["player2_elo"] < match["player1_elo"]

    _question(client, auth_headers, match_id, PLAYER_A)
    assert question_spy == [match["player2_elo"]]


def test_bot_and_friend_rounds_share_the_same_creation_path_otherwise(
    client, auth_headers, question_spy
):
    # Same deterministic ids, same round_number bookkeeping, same fields
    # apart from time_limit.
    bot_match = _bot_match(client, auth_headers)
    friend_match = _friend_match(
        client, auth_headers, p1="guest-question-fff", p2="guest-question-ggg"
    )

    bot_q = _question(client, auth_headers, bot_match, PLAYER_A)
    friend_q = _question(client, auth_headers, friend_match, "guest-question-fff")

    assert bot_q["round_id"] == f"round-{bot_match}-1"
    assert friend_q["round_id"] == f"round-{friend_match}-1"
    assert set(bot_q) - set(friend_q) == {"time_limit"}


# ---------------------------------------------------------------------------
# generate_question failures (case 21)
# ---------------------------------------------------------------------------


def test_generator_exception_is_a_generic_500(
    client_no_reraise, auth_headers, monkeypatch
):
    match_id = _friend_match(client_no_reraise, auth_headers)

    def _boom(_elo: int) -> dict:
        raise RuntimeError("sympy exploded: secret internals")

    monkeypatch.setattr(main, "generate_question", _boom)

    response = client_no_reraise.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."
    assert "secret internals" not in response.text
    # No half-created round state leaks out of the failure.
    assert len(_match_rounds(match_id)) == 0
    assert main.in_memory_matches[match_id].get("current_round_id") is None


def test_generator_missing_required_key_is_500_without_partial_round(
    client_no_reraise, auth_headers, monkeypatch
):
    match_id = _friend_match(client_no_reraise, auth_headers)

    def _bad(_elo: int) -> dict:
        return {"expression": "x", "answer": "1", "evaluate_at": 1}  # no difficulty

    monkeypatch.setattr(main, "generate_question", _bad)

    response = client_no_reraise.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 500
    assert len(_match_rounds(match_id)) == 0
    assert main.in_memory_matches[match_id].get("current_round_id") is None
    # Quirk: round_start_time IS already stamped on the match before the
    # crash (it is set before the round doc is built), i.e. the failure is
    # not perfectly atomic -- but no round or current_round_id ever appears.
    assert "round_start_time" in main.in_memory_matches[match_id]


def test_generator_returning_none_is_500(client_no_reraise, auth_headers, monkeypatch):
    match_id = _friend_match(client_no_reraise, auth_headers)
    monkeypatch.setattr(main, "generate_question", lambda _elo: None)

    response = client_no_reraise.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 500


def test_match_recovers_after_generator_failure(
    client_no_reraise, auth_headers, monkeypatch
):
    # A transient generator failure must not brick the match: the next poll
    # with a healthy generator creates round 1 normally.
    match_id = _friend_match(client_no_reraise, auth_headers)

    def _boom(_elo: int) -> dict:
        raise RuntimeError("transient")

    monkeypatch.setattr(main, "generate_question", _boom)
    failed = client_no_reraise.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert failed.status_code == 500

    def _ok(_elo: int) -> dict:
        return {
            "expression": "x^2",
            "derivative": "2·x",
            "evaluate_at": 1,
            "answer": "2·x",
            "difficulty": 1,
            "ask_for_derivative_only": True,
        }

    monkeypatch.setattr(main, "generate_question", _ok)
    recovered = client_no_reraise.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert recovered.status_code == 200
    assert recovered.json()["round_id"] == f"round-{match_id}-1"


# ---------------------------------------------------------------------------
# 5-minute stale-round timeout
# ---------------------------------------------------------------------------


def test_stale_round_over_five_minutes_ties_and_creates_fresh_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A)

    main.in_memory_rounds[first["round_id"]]["created_at"] = main.utc_now() - timedelta(
        seconds=301
    )

    nxt = _question(client, auth_headers, match_id, PLAYER_B)
    assert nxt["round_id"] == f"round-{match_id}-2"
    assert main.in_memory_rounds[first["round_id"]]["winner_id"] == "tie"
    # A timed-out round awards no points.
    match = main.in_memory_matches[match_id]
    assert match["player1_score"] == 0
    assert match["player2_score"] == 0


def test_round_under_five_minutes_is_still_served(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A)

    main.in_memory_rounds[first["round_id"]]["created_at"] = main.utc_now() - timedelta(
        seconds=299
    )

    again = _question(client, auth_headers, match_id, PLAYER_B)
    assert again["round_id"] == first["round_id"]
    assert main.in_memory_rounds[first["round_id"]]["winner_id"] is None


def test_stale_round_timeout_works_with_iso_string_created_at(
    client, auth_headers, fixed_question
):
    # After a Mongo round-trip created_at may be an ISO string;
    # parse_round_start must still detect the timeout.
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A)

    main.in_memory_rounds[first["round_id"]]["created_at"] = (
        main.utc_now() - timedelta(seconds=301)
    ).isoformat()

    nxt = _question(client, auth_headers, match_id, PLAYER_A)
    assert nxt["round_id"] == f"round-{match_id}-2"


def test_unparseable_created_at_never_times_out(
    client, auth_headers, fixed_question
):
    # Defensive quirk: a corrupted created_at parses to None, which the
    # timeout check treats as "not timed out", so the round is simply
    # resumed instead of crashing.
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A)

    main.in_memory_rounds[first["round_id"]]["created_at"] = "not-a-timestamp"

    again = _question(client, auth_headers, match_id, PLAYER_A)
    assert again["round_id"] == first["round_id"]


# ---------------------------------------------------------------------------
# Mongo rounds-array bookkeeping: round_number desync after ties (BUG)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: _create_next_round stores round_number = round count in the "
        "round doc, but pushes round_number = player1_score+player2_score+1 "
        "into the match's rounds array. Any tie round desyncs the two: the "
        "next round doc is numbered N+1 while its Mongo summary repeats an "
        "earlier number, so the positional winner updates that filter on "
        "rounds.round_number hit the wrong (or no) array entry."
    ),
)
def test_round_summary_number_should_match_round_doc_after_tie(
    client, auth_headers, fixed_question, match_update_calls
):
    match_id = _friend_match(client, auth_headers)
    _tie_round(client, auth_headers, match_id)  # round 1 ties, scores stay 0-0
    second = _question(client, auth_headers, match_id, PLAYER_A)

    summaries = _pushed_round_summaries(match_update_calls)
    assert summaries[-1]["round_number"] == main.in_memory_rounds[
        second["round_id"]
    ]["round_number"]


def test_current_behavior_tie_desyncs_mongo_round_numbers(
    client, auth_headers, fixed_question, match_update_calls
):
    """
    Pins the bug above: after a tied round 1, round 2's doc is numbered 2
    but its pushed Mongo summary is numbered 1 (scores are still 0-0), so
    the match's rounds array now holds TWO entries with round_number 1.
    When round 2 later resolves, the tie/winner updates filter on
    {"rounds.round_number": 2}, which matches no array entry at all.
    """
    match_id = _friend_match(client, auth_headers)
    _tie_round(client, auth_headers, match_id)
    second = _question(client, auth_headers, match_id, PLAYER_A)

    summaries = _pushed_round_summaries(match_update_calls)
    assert [s["round_number"] for s in summaries] == [1, 1]  # duplicate!
    assert main.in_memory_rounds[second["round_id"]]["round_number"] == 2

    # Tie round 2 as well and inspect the winner update it issues.
    _give_up(client, auth_headers, match_id, PLAYER_A)
    _give_up(client, auth_headers, match_id, PLAYER_B)
    tie_updates = [
        query
        for query, update in match_update_calls
        if update.get("$set", {}).get("rounds.$.winner") == "tie"
    ]
    # The last tie update targets round_number 2 -- which no summary has.
    assert tie_updates[-1]["rounds.round_number"] == 2
    assert all(s["round_number"] != 2 for s in summaries)


# ---------------------------------------------------------------------------
# round-id reuse after cache eviction (BUG)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: round ids are 'deterministic' only against the CURRENT "
        "in-memory round count. If in_memory_rounds is lost (restart, "
        "eviction, second worker) while the match survives, the next round "
        "recounts from zero and reissues round-<match>-1, overwriting the "
        "original round's history in memory and colliding with the _id "
        "already persisted in Mongo (the insert is skipped, so the DB keeps "
        "the OLD question while players see the new one)."
    ),
)
def test_round_ids_should_stay_unique_after_round_cache_loss(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A)
    _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)

    main.in_memory_rounds.clear()  # simulate cache loss / restart

    nxt = _question(client, auth_headers, match_id, PLAYER_A)
    assert nxt["round_id"] != first["round_id"]


def test_current_behavior_round_cache_loss_reuses_round_one_id(
    client, auth_headers, fixed_question
):
    """Pins the bug above: after cache loss the match restarts at round 1."""
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A)
    win = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert win["correct"] is True

    main.in_memory_rounds.clear()

    nxt = _question(client, auth_headers, match_id, PLAYER_A)
    assert nxt["round_id"] == first["round_id"] == f"round-{match_id}-1"
    # The reissued round 1 has forgotten the original winner.
    assert main.in_memory_rounds[nxt["round_id"]]["winner_id"] is None
    # The score from the overwritten round survives on the match, though.
    assert main.in_memory_matches[match_id]["player1_score"] == 1
