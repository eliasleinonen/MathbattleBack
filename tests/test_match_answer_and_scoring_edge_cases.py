"""
Edge-case tests for human-vs-human answer submission, scoring and win
conditions (main.py: submit_answer ~1610, give_up_round ~1509, first-to-3).

Scope:
- wrong/correct answer scoring, exactly-one-point invariants
- already_won semantics once a round has a winner
- malformed / weird payloads (empty, whitespace, null, wrong types, unicode,
  very long strings)
- mathematically equivalent answer forms via the inline SymPy checker
- match/round state errors (invalid match, no round yet, completed match,
  outsiders, waiting matches)
- first-to-3 win conditions (3-0 / 3-1 / 3-2, player1 vs player2 symmetry),
  round-number progression, ELO application for friend vs ranked vs bot
- give-up interplay (alone waits, both tie, answering after giving up,
  disconnected opponent auto-tie)
- concurrent correct answers from both players (in-memory vs DB-reload path)
- bot-match time-limit forfeits driven to a full match loss

Friend matches are used for controlled 1v1 unless the ranked queue or the
bot path is the point of the test.  Known bugs are documented with strict
xfail markers plus a companion test pinning current behavior; see
MATCH_EDGE_CASE_REPORT.md for the summary.
"""

import asyncio
import copy
from datetime import timedelta

import pytest

import main


PLAYER_A = "guest-ans-aaa"
PLAYER_B = "guest-ans-bbb"
PLAYER_C = "guest-ans-outsider"

CORRECT = "2*x"  # matches fixed_question's answer "2·x"
WRONG = "999"


# ---------------------------------------------------------------------------
# helpers
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


def _ranked_match(client, auth_headers, first=PLAYER_A, second=PLAYER_B):
    """Queue `first`, then `second` arrives and pairs. player1 is `second`."""
    searching = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(first)
    )
    assert searching.json()["status"] == "searching"
    matched = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(second)
    )
    assert matched.json()["status"] == "matched", matched.text
    return matched.json()["match_id"]


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
    match = main.in_memory_matches[body["match_id"]]
    assert match["player2_id"] == "bot-opponent"
    return body["match_id"]


def _expire_bot_round(match_id):
    """Backdate the synced round start so the bot time limit is exceeded."""
    main.in_memory_matches[match_id]["round_start_time"] = (
        main.utc_now() - timedelta(seconds=300)
    ).isoformat()


@pytest.fixture
def elo_writes(monkeypatch):
    """Spy on users_collection.update_one to capture ELO/wins/losses $inc."""
    calls = []

    async def spy_update_one(query, update, *args, **kwargs):
        calls.append((query, update))

    monkeypatch.setattr(main.users_collection, "update_one", spy_update_one)
    return calls


# ---------------------------------------------------------------------------
# basic scoring: wrong vs correct answers (cases 1, 2, 19, 20)
# ---------------------------------------------------------------------------


def test_wrong_answer_awards_no_score(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, WRONG)

    assert body["correct"] is False
    assert body["player1_score"] == 0
    assert body["player2_score"] == 0
    assert body["round_winner"] is None
    assert body["match_winner"] is None
    assert body["elo_change"] == 0


def test_wrong_answer_is_stored_on_round_but_round_stays_open(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    _answer(client, auth_headers, match_id, PLAYER_A, WRONG)

    round_id = main.in_memory_matches[match_id]["current_round_id"]
    round_doc = main.in_memory_rounds[round_id]
    assert round_doc["player1_answer"] == WRONG
    assert round_doc["winner_id"] is None  # still winnable by either player


def test_correct_answer_awards_exactly_one_point_to_winner_only(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)

    assert body["correct"] is True
    assert body["player1_score"] == 1
    assert body["player2_score"] == 0
    assert body["round_winner"] == PLAYER_A
    # The in-memory match agrees: exactly one score moved.
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (1, 0)
    assert match["status"] == "active"  # 1 < 3, match continues


def test_scores_never_go_negative_under_wrong_answer_spam(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    for i in range(15):
        body = _answer(
            client, auth_headers, match_id, PLAYER_A, f"totally-wrong-{i}"
        )
        assert body["correct"] is False
        assert body["player1_score"] >= 0
        assert body["player2_score"] >= 0
        assert (body["player1_score"], body["player2_score"]) == (0, 0)

    # Round is still winnable after the spam.
    winner = _answer(client, auth_headers, match_id, PLAYER_B, CORRECT)
    assert winner["correct"] is True
    assert winner["player2_score"] == 1


# ---------------------------------------------------------------------------
# already_won semantics (cases 3, 30)
# ---------------------------------------------------------------------------


def test_second_correct_answer_after_winner_gets_already_won(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    late = _answer(client, auth_headers, match_id, PLAYER_B, CORRECT)

    assert late["already_won"] is True
    # QUIRK: the already_won payload reports correct=False even though the
    # submitted answer is mathematically right; the answer is never checked.
    assert late["correct"] is False
    assert late["round_winner"] == PLAYER_A
    assert late["player1_score"] == 1
    assert late["player2_score"] == 0


def test_double_submit_by_winner_correct_then_wrong_changes_nothing(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    again = _answer(client, auth_headers, match_id, PLAYER_A, WRONG)

    assert again["already_won"] is True
    assert again["player1_score"] == 1
    assert again["player2_score"] == 0
    # The winning answer on the round doc is not overwritten by the retry.
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    assert main.in_memory_rounds[round_id]["player1_answer"] == CORRECT


def test_repeated_correct_submits_by_winner_never_double_score(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    for _ in range(5):
        body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
        assert body["already_won"] is True
        assert body["player1_score"] == 1


def test_both_players_wrong_then_one_correct_wins_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _question(client, auth_headers, match_id, PLAYER_B)

    wrong_a = _answer(client, auth_headers, match_id, PLAYER_A, "x^3")
    wrong_b = _answer(client, auth_headers, match_id, PLAYER_B, "sin(x)")
    assert wrong_a["correct"] is False
    assert wrong_b["correct"] is False

    winner = _answer(client, auth_headers, match_id, PLAYER_B, CORRECT)
    assert winner["correct"] is True
    assert winner["round_winner"] == PLAYER_B
    assert winner["player1_score"] == 0
    assert winner["player2_score"] == 1

    # Both wrong attempts remain recorded on the round.
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    round_doc = main.in_memory_rounds[round_id]
    assert round_doc["player1_answer"] == "x^3"
    assert round_doc["player2_answer"] == CORRECT  # overwritten by the win


# ---------------------------------------------------------------------------
# weird payloads: empty / whitespace / null / wrong types (cases 5, 24, 25, 26)
# ---------------------------------------------------------------------------


def test_empty_string_answer_is_graded_wrong_not_500(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, "")
    assert body["correct"] is False
    assert body["player1_score"] == 0


def test_whitespace_only_answer_is_graded_wrong(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, "   \t  ")
    assert body["correct"] is False


def test_json_null_answer_is_rejected_by_validation(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": None},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("bad_answer", [[1, 2], {"expr": "2*x"}])
def test_container_typed_answers_are_422(
    client, auth_headers, fixed_question, bad_answer
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": bad_answer},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 422


def test_missing_answer_or_match_id_is_422(client, auth_headers):
    assert (
        client.post(
            "/api/game/answer",
            json={"match_id": "m"},
            headers=auth_headers(PLAYER_A),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/game/answer",
            json={"answer": "2*x"},
            headers=auth_headers(PLAYER_A),
        ).status_code
        == 422
    )


def test_integer_match_id_is_422(client, auth_headers):
    response = client.post(
        "/api/game/answer",
        json={"match_id": 123, "answer": "2*x"},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 422


def test_boolean_answer_is_coerced_to_float_and_graded_wrong(
    client, auth_headers, fixed_question
):
    # QUIRK: Union[str, float] lets pydantic coerce JSON true -> 1.0, so a
    # boolean sails through validation and is graded as the answer "1.0".
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, True)
    assert body["correct"] is False
    assert body["player1_score"] == 0


def test_numeric_answer_against_symbolic_expected_is_wrong(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, 2.0)
    assert body["correct"] is False


def test_very_long_answer_strings_do_not_crash_grading(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    garbage = "z" * 5000
    body = _answer(client, auth_headers, match_id, PLAYER_A, garbage)
    assert body["correct"] is False

    # A long but valid expression still grades correct.
    padded_correct = "0 + " * 200 + CORRECT
    body = _answer(client, auth_headers, match_id, PLAYER_B, padded_correct)
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER_B


@pytest.mark.parametrize(
    "unicode_answer",
    [
        "２ｘ",  # fullwidth digits/letters -> SymPy parse error
        "𝟐𝐱",  # mathematical alphanumerics
        "٢x",  # arabic-indic digit
        "2✕x",  # heavy multiplication x
    ],
)
def test_unmapped_unicode_answers_are_graded_wrong_not_500(
    client, auth_headers, fixed_question, unicode_answer
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, unicode_answer)
    assert body["correct"] is False


def test_unicode_multiplication_sign_is_rejected_unlike_middle_dot(
    client, auth_headers, fixed_question
):
    # QUIRK/INCONSISTENCY: submit_answer's inline preprocess only maps the
    # middle dot "·" to "*". The standalone check_math_equivalence helper also
    # maps "×", but the PvP answer route does not use it, so "2×x" is wrong
    # here while the identical string would pass the daily-challenge checker.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    rejected = _answer(client, auth_headers, match_id, PLAYER_A, "2×x")
    assert rejected["correct"] is False

    accepted = _answer(client, auth_headers, match_id, PLAYER_B, "2·x")
    assert accepted["correct"] is True


# ---------------------------------------------------------------------------
# mathematically equivalent forms (case 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "equivalent_form",
    [
        "2*x",
        "2x",  # implicit multiplication
        "x+x",
        "2 x",  # implicit multiplication with space
        "x*2",
        "2·x",  # unicode middle dot (verbatim server form)
        "2.0*x",
        "4*x/2",
        "2*x + 0",
        "(2)(x)",
    ],
)
def test_equivalent_math_forms_are_accepted(
    client, auth_headers, fixed_question, equivalent_form
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, equivalent_form)
    assert body["correct"] is True, equivalent_form
    assert body["player1_score"] == 1


@pytest.mark.parametrize(
    "near_miss",
    ["2", "x", "-2*x", "2*x + 1", "x^2", "2*x^2"],
)
def test_near_miss_forms_are_rejected(
    client, auth_headers, fixed_question, near_miss
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, near_miss)
    assert body["correct"] is False, near_miss
    assert body["player1_score"] == 0


def test_numeric_expected_answer_uses_tolerance(
    client, auth_headers, fixed_question
):
    # Exercise the non-string expected-answer branch (abs diff < 0.1) by
    # mutating the live round doc the way a numeric question would store it.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    main.in_memory_rounds[round_id]["answer"] = 2.0

    close_enough = _answer(client, auth_headers, match_id, PLAYER_A, "2.05")
    assert close_enough["correct"] is True

    # New round for the out-of-tolerance probe.
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    main.in_memory_rounds[round_id]["answer"] = 2.0

    too_far = _answer(client, auth_headers, match_id, PLAYER_B, "2.2")
    assert too_far["correct"] is False

    not_a_number = _answer(client, auth_headers, match_id, PLAYER_B, "two")
    assert not_a_number["correct"] is False


# ---------------------------------------------------------------------------
# match/round state errors (cases 8, 9, 10, 16, 22)
# ---------------------------------------------------------------------------


def test_answer_with_unknown_match_id_is_404(client, auth_headers):
    response = client.post(
        "/api/game/answer",
        json={"match_id": "match-does-not-exist", "answer": "2*x"},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Match not found"


def test_answer_before_any_round_started_is_404_no_active_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    # Nobody has requested a question yet -> no current_round_id.
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "No active round"


def test_creator_answering_waiting_unjoined_match_gets_no_active_round(
    client, auth_headers, fixed_question
):
    # QUIRK: submit_answer never checks for status "waiting"; the creator only
    # bounces off the missing round, not off the unjoined match itself.
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    )
    match_id = created.json()["match_id"]

    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "No active round"


def test_outsider_cannot_answer_active_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": CORRECT},
        headers=auth_headers(PLAYER_C),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Not your match"
    # And the outsider's "correct" answer must not have scored for anyone.
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (0, 0)


def test_answer_after_match_completed_is_400(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, PLAYER_A)

    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": CORRECT},
        headers=auth_headers(PLAYER_B),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Match is already completed"

    # Scores frozen at the final 3-0.
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (3, 0)


def test_question_after_match_completed_is_400(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, PLAYER_A)

    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_B),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Match is already completed"


# ---------------------------------------------------------------------------
# first-to-3 win conditions (cases 11, 12, 21, 28)
# ---------------------------------------------------------------------------


def test_match_completes_at_exactly_3_0(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)

    for expected_score in (1, 2):
        body = _win_round(client, auth_headers, match_id, PLAYER_A)
        assert body["player1_score"] == expected_score
        assert body["match_winner"] is None  # not over yet
        assert body["elo_change"] == 0

    final = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert final["player1_score"] == 3
    assert final["player2_score"] == 0
    assert final["match_winner"] == PLAYER_A

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert str(match["winner_id"]) == PLAYER_A


def test_match_completes_at_3_1(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    _win_round(client, auth_headers, match_id, PLAYER_B)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    final = _win_round(client, auth_headers, match_id, PLAYER_A)

    assert final["player1_score"] == 3
    assert final["player2_score"] == 1
    assert final["match_winner"] == PLAYER_A
    assert main.in_memory_matches[match_id]["status"] == "completed"


def test_match_completes_at_3_2_after_2_2_tie_score(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    _win_round(client, auth_headers, match_id, PLAYER_B)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    fourth = _win_round(client, auth_headers, match_id, PLAYER_B)

    # 2-2: nobody has won, match still active.
    assert fourth["player1_score"] == 2
    assert fourth["player2_score"] == 2
    assert fourth["match_winner"] is None
    assert main.in_memory_matches[match_id]["status"] == "active"

    final = _win_round(client, auth_headers, match_id, PLAYER_B)
    assert final["player2_score"] == 3
    assert final["match_winner"] == PLAYER_B
    assert main.in_memory_matches[match_id]["status"] == "completed"


def test_player2_win_is_symmetric_to_player1_win(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    for _ in range(3):
        body = _win_round(client, auth_headers, match_id, PLAYER_B)

    assert body["player1_score"] == 0
    assert body["player2_score"] == 3
    assert body["match_winner"] == PLAYER_B

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert str(match["winner_id"]) == PLAYER_B


def test_status_endpoint_reflects_completed_match_and_winner(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, PLAYER_A)

    status = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_B)
    )
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "completed"
    assert body["winner_id"] == PLAYER_A
    assert body["player1_score"] == 3
    assert body["player2_score"] == 0


def test_round_numbers_progress_across_rounds(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)

    seen_round_ids = []
    for expected_number in (1, 2, 3):
        question = _question(client, auth_headers, match_id, PLAYER_A)
        round_id = question["round_id"]
        assert round_id == f"round-{match_id}-{expected_number}"
        assert main.in_memory_rounds[round_id]["round_number"] == expected_number
        seen_round_ids.append(round_id)
        _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)

    assert len(set(seen_round_ids)) == 3


def test_no_new_round_until_question_requested_again(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    # Without a fresh GET /question the finished round stays current, so any
    # answer just echoes already_won instead of silently starting round 2.
    stale = _answer(client, auth_headers, match_id, PLAYER_B, CORRECT)
    assert stale["already_won"] is True

    question = _question(client, auth_headers, match_id, PLAYER_B)
    assert question["round_id"] == f"round-{match_id}-2"


# ---------------------------------------------------------------------------
# ELO application: friend vs ranked vs bot (cases 18, 29)
# ---------------------------------------------------------------------------


def test_friend_match_completion_applies_no_elo(
    client, auth_headers, fixed_question, elo_writes
):
    match_id = _friend_match(client, auth_headers)
    for _ in range(3):
        final = _win_round(client, auth_headers, match_id, PLAYER_A)

    assert final["match_winner"] == PLAYER_A
    assert final["elo_change"] == 0
    assert main.in_memory_matches[match_id]["elo_change"] == 0
    # match_type "friend" is excluded from the ELO branch entirely.
    assert elo_writes == []


def test_ranked_match_answer_flow_and_elo_on_completion(
    client, auth_headers, fixed_question, elo_writes
):
    # PLAYER_A queues first; PLAYER_B pairs and becomes player1.
    match_id = _ranked_match(client, auth_headers, first=PLAYER_A, second=PLAYER_B)
    match = main.in_memory_matches[match_id]
    assert match["match_type"] == "ranked"
    assert str(match["player1_id"]) == PLAYER_B
    assert str(match["player2_id"]) == PLAYER_A

    # Normal answer flow works exactly like friend matches.
    _question(client, auth_headers, match_id, PLAYER_B)
    wrong = _answer(client, auth_headers, match_id, PLAYER_A, WRONG)
    assert wrong["correct"] is False

    for _ in range(3):
        final = _win_round(client, auth_headers, match_id, PLAYER_A)

    expected_change = main.calculate_elo_change(1000, 1000)
    assert final["match_winner"] == PLAYER_A
    assert final["elo_change"] == expected_change
    assert final["player2_score"] == 3

    # Winner got +elo/+1 win, loser got -elo/+1 loss.
    incs = {str(query["_id"]): update["$inc"] for query, update in elo_writes}
    assert incs[PLAYER_A] == {"elo": expected_change, "wins": 1}
    assert incs[PLAYER_B] == {"elo": -expected_change, "losses": 1}


def test_mid_match_ranked_round_win_applies_no_elo(
    client, auth_headers, fixed_question, elo_writes
):
    match_id = _ranked_match(client, auth_headers)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)

    assert body["match_winner"] is None
    assert body["elo_change"] == 0
    assert elo_writes == []


# ---------------------------------------------------------------------------
# give-up interplay (cases 13, 14, 15)
# ---------------------------------------------------------------------------


def test_give_up_alone_waits_for_opponent(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _question(client, auth_headers, match_id, PLAYER_B)  # B is "connected"

    body = _give_up(client, auth_headers, match_id, PLAYER_A)

    assert body == {"status": "gave_up", "waiting_for_opponent": True}
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    assert main.in_memory_rounds[round_id]["winner_id"] is None

    status = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_B)
    ).json()
    assert status["player1_gave_up"] is True
    assert status["player2_gave_up"] is False


def test_both_players_giving_up_ties_round_and_advances(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    first_round = _question(client, auth_headers, match_id, PLAYER_A)
    _question(client, auth_headers, match_id, PLAYER_B)

    _give_up(client, auth_headers, match_id, PLAYER_A)
    body = _give_up(client, auth_headers, match_id, PLAYER_B)

    assert body["status"] == "both_gave_up"
    assert body["round_winner"] == "tie"
    assert body["player1_score"] == 0  # nobody scores on a tie
    assert body["player2_score"] == 0

    # Answering the tied round reports already_won with the tie marker.
    late = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert late["already_won"] is True
    assert late["round_winner"] == "tie"

    # And the next question is a brand-new round.
    next_round = _question(client, auth_headers, match_id, PLAYER_B)
    assert next_round["round_id"] != first_round["round_id"]
    assert next_round["round_id"] == f"round-{match_id}-2"


def test_player_who_gave_up_can_still_answer_and_win_round(
    client, auth_headers, fixed_question
):
    # QUIRK: giving up records a flag but does not lock the player out of the
    # round; until the opponent also gives up, the quitter can still snipe the
    # point with a correct answer.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _question(client, auth_headers, match_id, PLAYER_B)
    _give_up(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER_A
    assert body["player1_score"] == 1


def test_give_up_with_disconnected_opponent_auto_ties(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _question(client, auth_headers, match_id, PLAYER_B)

    # Backdate B's heartbeat past PRESENCE_TIMEOUT_SECONDS so B counts as gone.
    main.in_memory_matches[match_id]["player_last_seen"][PLAYER_B] = (
        main.utc_now() - timedelta(seconds=main.PRESENCE_TIMEOUT_SECONDS + 5)
    )

    body = _give_up(client, auth_headers, match_id, PLAYER_A)
    assert body["status"] == "both_gave_up"
    assert body["round_winner"] == "tie"


def test_give_up_after_round_won_reports_already_ended(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    body = _give_up(client, auth_headers, match_id, PLAYER_B)
    assert body == {"status": "already_ended", "round_winner": PLAYER_A}


def test_give_up_without_round_is_404(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    response = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "No active round"


# ---------------------------------------------------------------------------
# concurrent correct answers (case 7)
# ---------------------------------------------------------------------------


def test_concurrent_correct_answers_in_memory_only_one_scores(
    client, auth_headers, fixed_question
):
    """
    With the round doc in memory the winner check and winner write happen
    with no await between them, so the two coroutines serialize on the event
    loop: exactly one scores, the other sees already_won.
    """
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    async def submit_concurrently():
        data = main.AnswerSubmit(match_id=match_id, answer=CORRECT)
        return await asyncio.gather(
            main.submit_answer(data, current_user={"_id": PLAYER_A}),
            main.submit_answer(data, current_user={"_id": PLAYER_B}),
        )

    result_a, result_b = asyncio.run(submit_concurrently())

    winners = [r for r in (result_a, result_b) if r.get("correct") is True]
    losers = [r for r in (result_a, result_b) if r.get("already_won")]
    assert len(winners) == 1
    assert len(losers) == 1

    match = main.in_memory_matches[match_id]
    assert match["player1_score"] + match["player2_score"] == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: submit_answer holds no match lock. When the round doc has to be "
        "re-read from the DB (memory miss + any latency), both players pass "
        "the winner_id check on their own copies and BOTH score the same round."
    ),
)
def test_concurrent_correct_answers_via_db_reload_only_one_scores(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = main.in_memory_matches[match_id]["current_round_id"]

    # Simulate a worker whose in-memory cache lost the round (restart,
    # eviction, other process) so submit_answer falls back to the DB.
    snapshot = copy.deepcopy(main.in_memory_rounds.pop(round_id))

    async def db_find_one_with_latency(query, *args, **kwargs):
        await asyncio.sleep(0)  # any real DB round-trip yields at least once
        if query.get("_id") == round_id:
            return copy.deepcopy(snapshot)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", db_find_one_with_latency)

    async def submit_concurrently():
        data = main.AnswerSubmit(match_id=match_id, answer=CORRECT)
        return await asyncio.gather(
            main.submit_answer(data, current_user={"_id": PLAYER_A}),
            main.submit_answer(data, current_user={"_id": PLAYER_B}),
        )

    asyncio.run(submit_concurrently())

    match = main.in_memory_matches[match_id]
    # One round must award exactly one point in total.
    assert match["player1_score"] + match["player2_score"] == 1


def test_current_behavior_db_reload_race_double_scores_one_round(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG (companion to the xfail above): pins today's behavior — the single
    # round pays out one point to EACH player (1-1), both responses claim
    # correct=True and round_winner=self.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = main.in_memory_matches[match_id]["current_round_id"]

    snapshot = copy.deepcopy(main.in_memory_rounds.pop(round_id))

    async def db_find_one_with_latency(query, *args, **kwargs):
        await asyncio.sleep(0)
        if query.get("_id") == round_id:
            return copy.deepcopy(snapshot)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", db_find_one_with_latency)

    async def submit_concurrently():
        data = main.AnswerSubmit(match_id=match_id, answer=CORRECT)
        return await asyncio.gather(
            main.submit_answer(data, current_user={"_id": PLAYER_A}),
            main.submit_answer(data, current_user={"_id": PLAYER_B}),
        )

    result_a, result_b = asyncio.run(submit_concurrently())

    assert result_a["correct"] is True
    assert result_b["correct"] is True
    assert result_a["round_winner"] == PLAYER_A
    assert result_b["round_winner"] == PLAYER_B

    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (1, 1)


# ---------------------------------------------------------------------------
# bot match time-limit forfeits (case 17)
# ---------------------------------------------------------------------------


def test_bot_match_timeout_awards_round_to_bot(
    client, auth_headers, fixed_question
):
    match_id = _bot_match(client, auth_headers, PLAYER_A)
    question = _question(client, auth_headers, match_id, PLAYER_A)
    assert "time_limit" in question  # bot rounds are timed

    _expire_bot_round(match_id)
    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)

    assert body["already_won"] is True
    assert body["correct"] is False  # even a correct answer forfeits on time
    assert body["round_winner"] == "bot-opponent"
    assert body["player1_score"] == 0
    assert body["player2_score"] == 1
    assert body["match_winner"] is None
    assert body["message"] == "Time limit exceeded"


def test_three_bot_timeouts_lose_match_and_deduct_elo(
    client, auth_headers, fixed_question, elo_writes
):
    match_id = _bot_match(client, auth_headers, PLAYER_A)

    for expected_bot_score in (1, 2, 3):
        _question(client, auth_headers, match_id, PLAYER_A)
        _expire_bot_round(match_id)
        body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
        assert body["player2_score"] == expected_bot_score

    assert body["match_winner"] == "bot-opponent"
    assert body["elo_change"] > 0

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["winner_id"] == "bot-opponent"

    # The human loser's ELO is decremented and a loss recorded.
    expected_change = main.calculate_elo_change(
        match["player2_elo"], match["player1_elo"]
    )
    assert body["elo_change"] == expected_change
    incs = {str(query["_id"]): update["$inc"] for query, update in elo_writes}
    assert incs[PLAYER_A] == {"elo": -expected_change, "losses": 1}

    # And the completed match rejects any further answers.
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": CORRECT},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 400
