"""Matchmaking regressions and small core logic tests against live main.py."""

import main


def test_random_poly_easy_always_returns_expr_deriv_pair():
    for _ in range(50):
        expr, deriv = main.random_poly_easy()
        assert isinstance(expr, str) and expr
        assert isinstance(deriv, str) and deriv


def test_calculate_elo_change_minimum_and_upset():
    even = main.calculate_elo_change(1000, 1000)
    upset = main.calculate_elo_change(1000, 1600)
    favorite = main.calculate_elo_change(1600, 1000)
    assert even >= 1
    assert upset > favorite


def test_two_guests_can_be_matched_in_ranked_queue(client, auth_headers):
    player_a = "guest-queue-aaa"
    player_b = "guest-queue-bbb"

    first = client.post(
        "/api/game/start",
        json={"mode": "random"},
        headers=auth_headers(player_a),
    )
    assert first.status_code == 200
    assert first.json()["status"] == "searching"

    second = client.post(
        "/api/game/start",
        json={"mode": "random"},
        headers=auth_headers(player_b),
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "matched"
    assert body["match_id"]

    # First player polling start again should discover the new match quickly.
    again = client.post(
        "/api/game/start",
        json={"mode": "random"},
        headers=auth_headers(player_a),
    )
    assert again.status_code == 200
    assert again.json()["status"] == "matched"
    assert again.json()["match_id"] == body["match_id"]

    match = main.in_memory_matches[body["match_id"]]
    assert match["match_type"] == "ranked"
    assert {str(match["player1_id"]), str(match["player2_id"])} == {
        player_a,
        player_b,
    }


def test_check_math_equivalence_basic_cases():
    assert main.check_math_equivalence("2*x", "x + x") is True
    assert main.check_math_equivalence("2*x", "3*x") is False
