"""
Comprehensive End-to-End Verification for 2 Real Human Players:
1. Matchmaking & queue pairing
2. Question synchronization & round countdown
3. Real-time answer racing & collision prevention
4. Wrong answer retry vs opponent winning
5. Match point settlement (first to 3)
6. Zero-sum ELO calculation & database updates
7. Post-completion lockdown
8. Clean rematch isolation
"""

import pytest
import main
from main import calculate_elo_change

PLAYER_1 = "guest-human-p1"
PLAYER_2 = "guest-human-p2"


def test_two_real_players_full_match_loop(client, auth_headers, fixed_question, monkeypatch):
    user_updates = []

    async def spy_update_user(filt, update, *args, **kwargs):
        user_updates.append({"filter": filt, "update": update})
        class R:
            modified_count = 1
        return R()

    monkeypatch.setattr(main.users_collection, "update_one", spy_update_user)

    # 1. Player 1 queues up for matchmaking
    res1 = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_1))
    assert res1.status_code == 200
    assert res1.json()["status"] == "searching"

    # 2. Player 2 queues up within 10s -> Matchmaker pairs them
    res2 = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_2))
    assert res2.status_code == 200
    p2_body = res2.json()
    assert p2_body["status"] == "matched"
    match_id = p2_body["match_id"]

    # 3. Player 1 polls /api/game/start (within 5s) -> Receives the matched state
    res1_poll = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_1))
    assert res1_poll.status_code == 200
    p1_body = res1_poll.json()
    assert p1_body["status"] == "matched"
    assert p1_body["match_id"] == match_id

    match = main.in_memory_matches[match_id]
    p1_is_doc_player1 = str(match["player1_id"]) == PLAYER_1

    # 4. Round 1: Both players request the question
    q1 = client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_1))
    q2 = client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_2))
    assert q1.status_code == 200 and q2.status_code == 200
    assert q1.json()["round_id"] == q2.json()["round_id"]
    assert q1.json()["expression"] == q2.json()["expression"]
    assert q1.json()["round_start_time"] == q2.json()["round_start_time"]

    # Player 1 answers correctly first
    ans1 = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_1),
    )
    assert ans1.status_code == 200
    assert ans1.json()["correct"] is True
    assert str(ans1.json()["round_winner"]) == PLAYER_1

    # Player 2 submits answer slightly later -> rejected as already won
    ans2 = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_2),
    )
    assert ans2.status_code == 200
    assert ans2.json()["already_won"] is True

    # Player 2 polls status -> confirms Player 1 won round 1
    st = client.get(f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_2))
    assert st.status_code == 200
    assert st.json()["round_winner"] == PLAYER_1

    # 5. Round 2: Both advance to round 2
    r2_q1 = client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_1))
    r2_q2 = client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_2))
    assert r2_q1.status_code == 200 and r2_q2.status_code == 200
    assert r2_q1.json()["round_id"] == r2_q2.json()["round_id"]
    assert r2_q1.json()["round_id"] != q1.json()["round_id"]

    # Player 1 makes a typo / wrong answer
    ans_wrong = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "999"},
        headers=auth_headers(PLAYER_1),
    )
    assert ans_wrong.status_code == 200
    assert ans_wrong.json()["correct"] is False

    # Player 2 answers correctly
    ans2_r2 = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_2),
    )
    assert ans2_r2.status_code == 200
    assert ans2_r2.json()["correct"] is True
    assert str(ans2_r2.json()["round_winner"]) == PLAYER_2

    # Scores are now 1 - 1
    st_r2 = client.get(f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_1))
    assert st_r2.json()["player1_score"] == 1
    assert st_r2.json()["player2_score"] == 1

    # 6. Round 3: Player 1 wins
    client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_1))
    client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_2))
    r3 = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_1),
    )
    assert r3.json()["correct"] is True
    assert str(r3.json()["round_winner"]) == PLAYER_1

    # 7. Round 4: Player 2 wins -> 2 - 2 (MATCH POINT)
    client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_1))
    client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_2))
    r4 = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_2),
    )
    assert r4.json()["correct"] is True
    assert str(r4.json()["round_winner"]) == PLAYER_2

    # Verify both at 2 wins
    st_r4 = client.get(f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_1))
    assert st_r4.json()["player1_score"] == 2
    assert st_r4.json()["player2_score"] == 2

    # 8. Round 5: Final round, deciding match point!
    client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_1))
    client.get("/api/game/question", params={"match_id": match_id}, headers=auth_headers(PLAYER_2))

    # Player 1 submits correct answer and wins match 3 - 2
    r5 = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_1),
    )
    assert r5.status_code == 200
    body5 = r5.json()
    assert body5["correct"] is True
    assert str(body5["match_winner"]) == PLAYER_1
    expected_elo = calculate_elo_change(1000, 1000)
    assert body5["elo_change"] == expected_elo

    # Check match doc in memory
    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert str(match["winner_id"]) == PLAYER_1

    # Verify Mongo received exactly two updates for ELO (winner +elo, loser -elo)
    assert len(user_updates) == 2
    # Winner
    assert user_updates[0]["filter"]["_id"] == PLAYER_1
    assert user_updates[0]["update"]["$inc"]["elo"] == expected_elo
    assert user_updates[0]["update"]["$inc"]["wins"] == 1
    # Loser
    assert user_updates[1]["filter"]["_id"] == PLAYER_2
    assert user_updates[1]["update"]["$inc"]["elo"] == -expected_elo
    assert user_updates[1]["update"]["$inc"]["losses"] == 1

    # 9. Verify post-completion lockdown: match rejects further actions
    post_ans = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_2),
    )
    assert post_ans.status_code == 400
    assert "already completed" in post_ans.json()["detail"].lower()

    post_q = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_1),
    )
    assert post_q.status_code == 400
    assert "already completed" in post_q.json()["detail"].lower()

    # 10. Clean rematch: both players can queue again and match in a fresh match
    rematch_res1 = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_1))
    assert rematch_res1.json()["status"] == "searching"

    rematch_res2 = client.post("/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_2))
    assert rematch_res2.json()["status"] == "matched"
    new_match_id = rematch_res2.json()["match_id"]
    assert new_match_id != match_id

    new_match = main.in_memory_matches[new_match_id]
    assert new_match["status"] == "active"
    assert new_match["player1_score"] == 0
    assert new_match["player2_score"] == 0


def test_concurrent_answers_mutex_lock(mock_mongo, fixed_question):
    """
    Assert that if two real players submit the correct answer in the exact same
    event-loop tick, the match lock guarantees:
    - Exactly one player wins the round
    - The other player receives already_won: True
    - Total score increments by exactly 1
    """
    match_id = "test-concurrent-match-1"
    main.in_memory_matches[match_id] = {
        "_id": match_id,
        "match_type": "ranked",
        "player1_id": PLAYER_1,
        "player2_id": PLAYER_2,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": 1200,
        "player2_elo": 1200,
        "status": "active",
        "winner_id": None,
        "elo_change": 0,
        "rounds": [],
    }

    import asyncio

    async def run_race():
        q1 = await main.get_question(match_id, current_user={"_id": PLAYER_1, "elo": 1200})
        q2 = await main.get_question(match_id, current_user={"_id": PLAYER_2, "elo": 1200})
        assert q1["round_id"] == q2["round_id"]

        data = main.AnswerSubmit(match_id=match_id, answer="2*x")
        results = await asyncio.gather(
            main.submit_answer(data, current_user={"_id": PLAYER_1}),
            main.submit_answer(data, current_user={"_id": PLAYER_2}),
        )
        return results

    results = asyncio.run(run_race())

    correct_results = [r for r in results if r.get("correct") is True]
    already_won_results = [r for r in results if r.get("already_won") is True]

    assert len(correct_results) == 1
    assert len(already_won_results) == 1

    match = main.in_memory_matches[match_id]
    assert (match["player1_score"] + match["player2_score"]) == 1

    assert len(already_won_results) == 1

    match = main.in_memory_matches[match_id]
    assert (match["player1_score"] + match["player2_score"]) == 1
