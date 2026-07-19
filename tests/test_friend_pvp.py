"""Friend match / human vs human gameplay tests."""

import main


PLAYER_A = "guest-player-aaa"
PLAYER_B = "guest-player-bbb"
PLAYER_C = "guest-outsider-ccc"


def _create_friend_match(client, auth_headers, player_id=PLAYER_A):
    response = client.post(
        "/api/game/friend/create",
        json={},
        headers=auth_headers(player_id),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "waiting"
    assert body["match_code"]
    assert body["match_id"]
    return body


def _join_friend_match(client, auth_headers, match_code, player_id=PLAYER_B):
    response = client.post(
        "/api/game/friend/join",
        json={"match_code": match_code},
        headers=auth_headers(player_id),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "active"
    return body


def test_friend_match_create_and_join_activates_match(client, auth_headers):
    created = _create_friend_match(client, auth_headers)
    joined = _join_friend_match(client, auth_headers, created["match_code"])

    assert joined["match_id"] == created["match_id"]

    status = client.get(f"/api/game/friend/status/{created['match_code']}")
    assert status.status_code == 200
    assert status.json()["status"] == "active"
    assert status.json()["player1_ready"] is True
    assert status.json()["player2_ready"] is True

    match = main.in_memory_matches[created["match_id"]]
    assert str(match["player1_id"]) == PLAYER_A
    assert str(match["player2_id"]) == PLAYER_B
    assert match["match_type"] == "friend"


def test_cannot_join_own_friend_match(client, auth_headers):
    created = _create_friend_match(client, auth_headers, PLAYER_A)

    response = client.post(
        "/api/game/friend/join",
        json={"match_code": created["match_code"]},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 400
    assert "own match" in response.json()["detail"].lower()


def test_outsider_forbidden_on_friend_gameplay_routes(
    client, auth_headers, fixed_question
):
    created = _create_friend_match(client, auth_headers)
    _join_friend_match(client, auth_headers, created["match_code"])
    match_id = created["match_id"]

    question = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert question.status_code == 200

    outsider_question = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_C),
    )
    assert outsider_question.status_code == 403

    outsider_answer = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_C),
    )
    assert outsider_answer.status_code == 403

    outsider_status = client.get(
        f"/api/game/status/{match_id}",
        headers=auth_headers(PLAYER_C),
    )
    assert outsider_status.status_code == 403

    outsider_give_up = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_C),
    )
    assert outsider_give_up.status_code == 403


def test_both_players_see_same_question(client, auth_headers, fixed_question):
    created = _create_friend_match(client, auth_headers)
    _join_friend_match(client, auth_headers, created["match_code"])
    match_id = created["match_id"]

    q1 = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    q2 = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_B),
    )

    assert q1.status_code == 200
    assert q2.status_code == 200
    assert q1.json()["expression"] == q2.json()["expression"]
    assert q1.json()["round_id"] == q2.json()["round_id"]


def test_first_correct_answer_wins_round_for_human_vs_human(
    client, auth_headers, fixed_question
):
    created = _create_friend_match(client, auth_headers)
    _join_friend_match(client, auth_headers, created["match_code"])
    match_id = created["match_id"]

    question = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert question.status_code == 200

    winner = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_A),
    )
    assert winner.status_code == 200, winner.text
    winner_body = winner.json()
    assert winner_body["correct"] is True
    assert winner_body["player1_score"] == 1
    assert winner_body["player2_score"] == 0
    assert str(winner_body["round_winner"]) == PLAYER_A

    late = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_B),
    )
    assert late.status_code == 200
    late_body = late.json()
    assert late_body.get("already_won") is True
    assert late_body["player1_score"] == 1
    assert late_body["player2_score"] == 0


def test_player2_can_win_round_and_scores_increment(
    client, auth_headers, fixed_question
):
    created = _create_friend_match(client, auth_headers)
    _join_friend_match(client, auth_headers, created["match_code"])
    match_id = created["match_id"]

    assert (
        client.get(
            "/api/game/question",
            params={"match_id": match_id},
            headers=auth_headers(PLAYER_B),
        ).status_code
        == 200
    )

    wrong = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "999"},
        headers=auth_headers(PLAYER_A),
    )
    assert wrong.status_code == 200
    assert wrong.json()["correct"] is False
    assert wrong.json()["player1_score"] == 0
    assert wrong.json()["player2_score"] == 0

    winner = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_B),
    )
    assert winner.status_code == 200, winner.text
    body = winner.json()
    assert body["correct"] is True
    assert body["player1_score"] == 0
    assert body["player2_score"] == 1
    assert str(body["round_winner"]) == PLAYER_B


def test_status_poll_shows_both_players_and_scores(
    client, auth_headers, fixed_question
):
    created = _create_friend_match(client, auth_headers)
    _join_friend_match(client, auth_headers, created["match_code"])
    match_id = created["match_id"]

    client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_A),
    )

    status = client.get(
        f"/api/game/status/{match_id}",
        headers=auth_headers(PLAYER_B),
    )
    assert status.status_code == 200
    body = status.json()
    assert body["player1_id"] == PLAYER_A
    assert body["player2_id"] == PLAYER_B
    assert body["player1_score"] == 1
    assert body["player2_score"] == 0
    assert body["round_winner"] == PLAYER_A
