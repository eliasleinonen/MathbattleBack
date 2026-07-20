"""Answer-grading DoS safety tests.

Hostile answers like power towers (9**9**9) used to hang the worker inside
sympy's evaluate=True parsing / numeric fallback. These tests assert that such
answers are graded incorrect quickly instead of hanging.
"""

import time

import main


PLAYER_A = "guest-safety-aaa"
PLAYER_B = "guest-safety-bbb"

# Generous bound: guarded grading returns in milliseconds; unguarded hangs forever.
MAX_GRADING_SECONDS = 2.0


def _start_friend_match(client, auth_headers):
    created = client.post(
        "/api/game/friend/create",
        json={},
        headers=auth_headers(PLAYER_A),
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

    question = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert question.status_code == 200, question.text
    return match_id


def _submit(client, auth_headers, match_id, answer):
    start = time.monotonic()
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(PLAYER_A),
    )
    elapsed = time.monotonic() - start
    assert response.status_code == 200, response.text
    return response.json(), elapsed


def test_numeric_power_tower_rejected_quickly(client, auth_headers, fixed_question):
    match_id = _start_friend_match(client, auth_headers)
    body, elapsed = _submit(client, auth_headers, match_id, "9**9**9")
    assert body["correct"] is False
    assert elapsed < MAX_GRADING_SECONDS


def test_symbolic_power_tower_rejected_quickly(client, auth_headers, fixed_question):
    match_id = _start_friend_match(client, auth_headers)
    body, elapsed = _submit(client, auth_headers, match_id, "x**x**x**x**x")
    assert body["correct"] is False
    assert elapsed < MAX_GRADING_SECONDS


def test_parenthesized_power_tower_rejected_quickly(
    client, auth_headers, fixed_question
):
    match_id = _start_friend_match(client, auth_headers)
    body, elapsed = _submit(client, auth_headers, match_id, "9**((9**9))")
    assert body["correct"] is False
    assert elapsed < MAX_GRADING_SECONDS


def test_caret_power_tower_rejected_quickly(client, auth_headers, fixed_question):
    match_id = _start_friend_match(client, auth_headers)
    body, elapsed = _submit(client, auth_headers, match_id, "9^9^9")
    assert body["correct"] is False
    assert elapsed < MAX_GRADING_SECONDS


def test_overlong_answer_rejected_quickly(client, auth_headers, fixed_question):
    match_id = _start_friend_match(client, auth_headers)
    long_answer = "x+" * 300 + "x"
    body, elapsed = _submit(client, auth_headers, match_id, long_answer)
    assert body["correct"] is False
    assert elapsed < MAX_GRADING_SECONDS


def test_normal_answer_still_correct(client, auth_headers, fixed_question):
    match_id = _start_friend_match(client, auth_headers)
    body, elapsed = _submit(client, auth_headers, match_id, "2*x")
    assert body["correct"] is True
    assert elapsed < MAX_GRADING_SECONDS


def test_unicode_multiplication_signs_accepted(client, auth_headers, fixed_question):
    for answer in ("2×x", "2·x"):
        match_id = _start_friend_match(client, auth_headers)
        body, _ = _submit(client, auth_headers, match_id, answer)
        assert body["correct"] is True, f"expected {answer!r} to be graded correct"


def test_check_math_equivalence_guards_daily_challenge_path():
    """Direct check of the helper used by the daily challenge grading path."""
    for hostile in ("9**9**9", "x**x**x**x**x", "9**((9**9))", "x" * 300):
        start = time.monotonic()
        assert main.check_math_equivalence("2*x", hostile) is False
        assert time.monotonic() - start < MAX_GRADING_SECONDS

    assert main.check_math_equivalence("2·x", "2×x") is True
    assert main.check_math_equivalence("2·x", "2*x") is True
