"""
Niche give-up (`/api/game/give-up`) and status-polling (`/api/game/status`)
edge cases in people-vs-people matches (friend + ranked).

Covers:
- give-up before any question / on waiting matches / on unknown matches
- give-up when the round is already won (by opponent, by self, by tie, on a
  completed match)
- nearly-concurrent give-ups via asyncio.gather over the route coroutines,
  including a lost-update race when the round is hydrated from the DB
- give-up racing / followed by a correct opponent answer
- status payload completeness for waiting/active/completed/abandoned matches
- status round_winner values: player id, "tie", null
- opponent_connected flipping across the exact 12s presence boundary while
  polling with a steppable frozen clock
- status polling on completed matches, heartbeat bookkeeping
- give-up never awarding points or ELO, repeated give-ups, ranked-vs-friend
  parity, both-players-stale resolution, outsider 403s, and polling while
  still searching (no match yet)

Conventions (same as the sibling edge-case files):
- Guest identities via "Bearer guest-xxx" tokens.
- Presence boundaries use a steppable frozen main.utc_now clock; endpoint
  tests otherwise backdate player_last_seen with comfortable margins.
- Known bugs are documented with strict xfail markers plus sibling tests
  that pin the CURRENT behavior.  See MATCH_EDGE_CASE_REPORT.md.
"""

import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest

import main


PLAYER_A = "guest-gus-aaa"
PLAYER_B = "guest-gus-bbb"
OUTSIDER = "guest-gus-zzz"

STATUS_KEYS = {
    "match_id",
    "player1_id",
    "player2_id",
    "player1_name",
    "player2_name",
    "player1_score",
    "player2_score",
    "status",
    "winner_id",
    "elo_change",
    "round_winner",
    "round_start_time",
    "player1_gave_up",
    "player2_gave_up",
    "opponent_connected",
}


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


class _Clock:
    """Steppable frozen clock substituted for main.utc_now."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds=0, microseconds=0):
        self.now += timedelta(seconds=seconds, microseconds=microseconds)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(datetime.now(timezone.utc))
    monkeypatch.setattr(main, "utc_now", c)
    return c


def _start(client, auth_headers, player):
    response = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ranked_match(client, auth_headers, player1=PLAYER_A, player2=PLAYER_B):
    """Queue player2 first so the joining player1 lands in the player1 slot."""
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


def _status(client, auth_headers, match_id, player):
    return client.get(f"/api/game/status/{match_id}", headers=auth_headers(player))


def _question(client, auth_headers, match_id, player):
    return client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )


def _answer(client, auth_headers, match_id, player, answer="2*x"):
    return client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )


def _give_up(client, auth_headers, match_id, player):
    return client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )


def _backdate_seen(match_id, player, seconds):
    match = main.in_memory_matches[match_id]
    match.setdefault("player_last_seen", {})[str(player)] = main.utc_now() - timedelta(
        seconds=seconds
    )


def _win_round(client, auth_headers, match_id, player):
    q = _question(client, auth_headers, match_id, player)
    assert q.status_code == 200, q.text
    r = _answer(client, auth_headers, match_id, player)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["correct"] is True, body
    return body


def _complete_match(client, auth_headers, match_id, winner=PLAYER_A):
    match = main.in_memory_matches[match_id]
    if str(match["player1_id"]) == str(winner):
        match["player1_score"] = 2
    else:
        match["player2_score"] = 2
    body = _win_round(client, auth_headers, match_id, winner)
    assert body["match_winner"] == str(winner)
    return body


def _tie_current_round(client, auth_headers, match_id):
    """Resolve the current round to a tie via sequential double give-up."""
    first = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert first["status"] == "gave_up"
    second = _give_up(client, auth_headers, match_id, PLAYER_B).json()
    assert second["status"] == "both_gave_up"
    return second


def _gather_give_ups(match_id, *players):
    """Fire give_up_round for every player in one event loop."""

    async def run():
        return await asyncio.gather(
            *[
                main.give_up_round(match_id, current_user={"_id": player})
                for player in players
            ]
        )

    return asyncio.run(run())


@pytest.fixture
def elo_write_spy(client, monkeypatch):
    """Record every users_collection.update_one call (ELO/W-L payouts)."""
    calls = []

    async def update_one(*args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr(main.users_collection, "update_one", update_one)
    return calls


# ---------------------------------------------------------------------------
# 1. give-up before any question exists
# ---------------------------------------------------------------------------


def test_give_up_before_first_question_friend_404(client, auth_headers):
    match_id = _friend_match(client, auth_headers)
    response = _give_up(client, auth_headers, match_id, PLAYER_A)
    assert response.status_code == 404
    assert response.json()["detail"] == "No active round"


def test_give_up_before_first_question_ranked_404(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    response = _give_up(client, auth_headers, match_id, PLAYER_B)
    assert response.status_code == 404
    assert response.json()["detail"] == "No active round"


def test_give_up_on_waiting_friend_match_404_no_round(client, auth_headers):
    # The creator can reach give-up on a match nobody joined yet; it fails on
    # the missing round, not on membership (waiting status is never checked).
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()
    response = _give_up(client, auth_headers, created["match_id"], PLAYER_A)
    assert response.status_code == 404
    assert response.json()["detail"] == "No active round"


def test_premature_give_up_leaves_no_round_state_behind(client, auth_headers):
    match_id = _friend_match(client, auth_headers)
    _give_up(client, auth_headers, match_id, PLAYER_A)
    assert main.in_memory_rounds == {}
    assert main.in_memory_matches[match_id].get("current_round_id") is None
    # The failed give-up still counted as a heartbeat for the caller.
    assert PLAYER_A in main.in_memory_matches[match_id]["player_last_seen"]


def test_give_up_on_unknown_match_404(client, auth_headers):
    response = _give_up(client, auth_headers, "match-nope", PLAYER_A)
    assert response.status_code == 404
    assert response.json()["detail"] == "Match not found"


# ---------------------------------------------------------------------------
# 2. give-up when the round is already won
# ---------------------------------------------------------------------------


def test_give_up_after_opponent_won_round(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_B)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body == {"status": "already_ended", "round_winner": PLAYER_B}


def test_give_up_after_winning_the_round_yourself(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body == {"status": "already_ended", "round_winner": PLAYER_A}


def test_give_up_after_tie_round_reports_tie_winner(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body == {"status": "already_ended", "round_winner": "tie"}


def test_match_winner_can_still_give_up_on_final_round(
    client, auth_headers, fixed_question
):
    # give_up_round never checks match status, so even the champion giving up
    # after match point just gets the final round echoed back.
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body == {"status": "already_ended", "round_winner": PLAYER_A}
    assert main.in_memory_matches[match_id]["status"] == "completed"


def test_already_ended_give_up_does_not_set_gave_up_flags(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_B)
    _give_up(client, auth_headers, match_id, PLAYER_A)

    round_doc = main.in_memory_rounds[f"round-{match_id}-1"]
    assert "player1_gave_up" not in round_doc
    assert "player2_gave_up" not in round_doc


# ---------------------------------------------------------------------------
# 3. both players give up nearly concurrently (asyncio)
# ---------------------------------------------------------------------------


def test_concurrent_give_ups_resolve_tie_on_shared_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    results = _gather_give_ups(match_id, PLAYER_A, PLAYER_B)
    # The mocked DB calls never yield, so the coroutines run back-to-back:
    # the first caller waits, the second resolves the tie.
    assert results[0]["status"] == "gave_up"
    assert results[1]["status"] == "both_gave_up"
    assert results[1]["round_winner"] == "tie"
    assert main.in_memory_rounds[f"round-{match_id}-1"]["winner_id"] == "tie"


def test_interleaved_give_ups_both_see_the_tie(
    client, auth_headers, fixed_question, monkeypatch
):
    # With a DB write that actually yields, both flag writes land on the
    # shared in-memory round doc before either both-gave-up check runs, so
    # BOTH callers get the terminal both_gave_up response (double resolution
    # of the same tie - harmless because the winner value is identical).
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    async def yielding_update_one(*args, **kwargs):
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(main.rounds_collection, "update_one", yielding_update_one)

    results = _gather_give_ups(match_id, PLAYER_A, PLAYER_B)
    assert [r["status"] for r in results] == ["both_gave_up", "both_gave_up"]
    assert {r["round_winner"] for r in results} == {"tie"}
    assert main.in_memory_rounds[f"round-{match_id}-1"]["winner_id"] == "tie"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(giveup-lost-update): give_up_round takes no per-match lock and "
        "hydrates a cache-missed round via an awaited find_one.  Two "
        "concurrent give-ups each hydrate a private copy of the round doc; "
        "the second write-back clobbers the first player's flag, so BOTH "
        "callers get 'gave_up'/waiting and the round never resolves even "
        "though both players gave up.  See the sibling test pinning current "
        "behavior."
    ),
)
def test_concurrent_give_ups_after_round_eviction_should_resolve_tie(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"
    db_round = copy.deepcopy(main.in_memory_rounds.pop(round_id))

    async def find_one(query, *args, **kwargs):
        await asyncio.sleep(0)  # both callers miss the cache before this
        if query.get("_id") == round_id:
            return copy.deepcopy(db_round)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", find_one)

    results = _gather_give_ups(match_id, PLAYER_A, PLAYER_B)
    assert any(r["status"] == "both_gave_up" for r in results)
    assert main.in_memory_rounds[round_id]["winner_id"] == "tie"


def test_current_behavior_concurrent_hydrated_give_ups_lose_one_flag(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG pin: current (wrong) behavior of the xfail above - the last
    # hydrated copy wins, player1's give-up evaporates, nobody resolves.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"
    db_round = copy.deepcopy(main.in_memory_rounds.pop(round_id))

    async def find_one(query, *args, **kwargs):
        await asyncio.sleep(0)
        if query.get("_id") == round_id:
            return copy.deepcopy(db_round)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", find_one)

    results = _gather_give_ups(match_id, PLAYER_A, PLAYER_B)
    assert [r["status"] for r in results] == ["gave_up", "gave_up"]

    survivor = main.in_memory_rounds[round_id]
    assert survivor["player1_gave_up"] is False  # A's give-up was lost
    assert survivor["player2_gave_up"] is True
    assert survivor.get("winner_id") is None  # round is stuck


# ---------------------------------------------------------------------------
# 4. give up, then the opponent answers correctly
# ---------------------------------------------------------------------------


def test_opponent_answer_after_give_up_wins_the_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)  # keep B connected
    assert _give_up(client, auth_headers, match_id, PLAYER_A).json()["status"] == (
        "gave_up"
    )

    body = _answer(client, auth_headers, match_id, PLAYER_B).json()
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER_B
    assert body["player2_score"] == 1
    assert body["player1_score"] == 0


def test_status_shows_both_give_up_flag_and_answer_winner(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _give_up(client, auth_headers, match_id, PLAYER_A)
    _answer(client, auth_headers, match_id, PLAYER_B)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["player1_gave_up"] is True
    assert body["player2_gave_up"] is False
    assert body["round_winner"] == PLAYER_B
    assert body["player2_score"] == 1


def test_second_give_up_after_opponent_answered_reports_already_ended(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _give_up(client, auth_headers, match_id, PLAYER_A)
    _answer(client, auth_headers, match_id, PLAYER_B)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body == {"status": "already_ended", "round_winner": PLAYER_B}


def test_give_up_racing_a_correct_answer_lets_the_answer_win(
    client, auth_headers, fixed_question, monkeypatch
):
    # A's give-up passes the winner check, then yields on the DB write while
    # B's correct answer lands: A still gets "gave_up"/waiting although the
    # round is already decided against them.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)

    async def yielding_update_one(*args, **kwargs):
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(main.rounds_collection, "update_one", yielding_update_one)

    async def run():
        return await asyncio.gather(
            main.give_up_round(match_id, current_user={"_id": PLAYER_A}),
            main.submit_answer(
                main.AnswerSubmit(match_id=match_id, answer="2*x"),
                current_user={"_id": PLAYER_B},
            ),
        )

    gave_up, answered = asyncio.run(run())
    assert gave_up == {"status": "gave_up", "waiting_for_opponent": True}
    assert answered["correct"] is True
    assert answered["round_winner"] == PLAYER_B
    assert str(main.in_memory_rounds[f"round-{match_id}-1"]["winner_id"]) == PLAYER_B


# ---------------------------------------------------------------------------
# 5. status payload completeness per lifecycle state
# ---------------------------------------------------------------------------


def test_status_fields_complete_on_waiting_match(client, auth_headers):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()
    body = _status(client, auth_headers, created["match_id"], PLAYER_A).json()

    assert set(body.keys()) == STATUS_KEYS
    assert body["status"] == "waiting"
    assert body["player2_id"] == "None"  # known str(None) quirk
    assert body["player2_name"] == "Player 2"
    assert body["winner_id"] is None
    assert body["round_winner"] is None
    assert body["player1_gave_up"] is False
    assert body["player2_gave_up"] is False
    assert body["opponent_connected"] is True  # never-seen ghost opponent


def test_status_fields_complete_on_active_match_with_round(
    client, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)
    question = _question(client, auth_headers, match_id, PLAYER_A).json()
    body = _status(client, auth_headers, match_id, PLAYER_A).json()

    assert set(body.keys()) == STATUS_KEYS
    assert body["status"] == "active"
    assert body["round_start_time"] == question["round_start_time"]
    assert body["winner_id"] is None
    assert body["elo_change"] == 0
    assert isinstance(body["player1_score"], int)
    assert isinstance(body["player2_score"], int)


def test_status_fields_complete_on_completed_match(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_B)
    body = _status(client, auth_headers, match_id, PLAYER_A).json()

    assert set(body.keys()) == STATUS_KEYS
    assert body["status"] == "completed"
    assert body["winner_id"] == PLAYER_B
    assert body["player2_score"] == 3
    assert body["elo_change"] == 0  # friend matches never pay ELO
    assert body["round_winner"] == PLAYER_B  # final round doc is still current


def test_status_fields_complete_on_abandoned_match(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"
    body = _status(client, auth_headers, match_id, PLAYER_B).json()

    assert set(body.keys()) == STATUS_KEYS
    assert body["status"] == "abandoned"
    assert body["winner_id"] is None
    assert body["round_winner"] is None
    assert body["round_start_time"] is None


# ---------------------------------------------------------------------------
# 6. status round_winner values: player id / tie / null
# ---------------------------------------------------------------------------


def test_status_round_winner_is_player_id_after_win(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    body = _status(client, auth_headers, match_id, PLAYER_B).json()
    assert body["round_winner"] == PLAYER_A


def test_status_round_winner_is_tie_after_double_give_up(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)

    for player in (PLAYER_A, PLAYER_B):
        body = _status(client, auth_headers, match_id, player).json()
        assert body["round_winner"] == "tie"
        assert body["player1_gave_up"] is True
        assert body["player2_gave_up"] is True


def test_status_round_winner_is_null_while_round_undecided(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    # No round at all -> null.
    assert _status(client, auth_headers, match_id, PLAYER_A).json()["round_winner"] is None
    # Fresh round, wrong answer -> still null.
    _question(client, auth_headers, match_id, PLAYER_A)
    _answer(client, auth_headers, match_id, PLAYER_B, answer="totally wrong")
    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["round_winner"] is None


def test_status_round_winner_resets_to_null_on_next_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    _question(client, auth_headers, match_id, PLAYER_A)  # starts round 2

    body = _status(client, auth_headers, match_id, PLAYER_B).json()
    assert body["round_winner"] is None
    assert body["player1_score"] == 1  # score from round 1 is kept


# ---------------------------------------------------------------------------
# 7. opponent_connected across the exact presence boundary
# ---------------------------------------------------------------------------


def test_opponent_connected_at_exactly_twelve_seconds(client, auth_headers, clock):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)  # B seen at t0

    clock.advance(seconds=main.PRESENCE_TIMEOUT_SECONDS)
    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["opponent_connected"] is True  # <= comparison: 12.0s inside


def test_opponent_disconnected_one_microsecond_past_boundary(
    client, auth_headers, clock
):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)

    clock.advance(seconds=main.PRESENCE_TIMEOUT_SECONDS, microseconds=1)
    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["opponent_connected"] is False


def test_opponent_connected_flips_false_then_true_across_polls(
    client, auth_headers, clock
):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)

    # Inside the window.
    clock.advance(seconds=11)
    assert _status(client, auth_headers, match_id, PLAYER_A).json()[
        "opponent_connected"
    ] is True

    # Cross the boundary without any B heartbeat.
    clock.advance(seconds=2)  # 13s since B's poll
    assert _status(client, auth_headers, match_id, PLAYER_A).json()[
        "opponent_connected"
    ] is False

    # B polls again: the very next A poll flips back to connected.
    _status(client, auth_headers, match_id, PLAYER_B)
    assert _status(client, auth_headers, match_id, PLAYER_A).json()[
        "opponent_connected"
    ] is True


def test_own_polling_does_not_keep_opponent_connected(client, auth_headers, clock):
    # A's frantic polling refreshes only A; B still times out on schedule.
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)
    for _ in range(5):
        clock.advance(seconds=3)
        _status(client, auth_headers, match_id, PLAYER_A)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()  # 15s+ for B
    assert body["opponent_connected"] is False
    # ...and B, whose own heartbeat is stale, still sees fresh A as connected.
    assert _status(client, auth_headers, match_id, PLAYER_B).json()[
        "opponent_connected"
    ] is True


# ---------------------------------------------------------------------------
# 8. status after match completion still works
# ---------------------------------------------------------------------------


def test_status_after_completion_stable_across_repeated_polls(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)

    bodies = [
        _status(client, auth_headers, match_id, player).json()
        for player in (PLAYER_A, PLAYER_B, PLAYER_A, PLAYER_B)
    ]
    for body in bodies:
        assert body["status"] == "completed"
        assert body["winner_id"] == PLAYER_A
        assert body["player1_score"] == 3
        assert body["round_winner"] == PLAYER_A


def test_status_heartbeats_still_recorded_after_completion(
    client, auth_headers, fixed_question, clock
):
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)

    clock.advance(seconds=100)
    _status(client, auth_headers, match_id, PLAYER_B)
    seen = main.in_memory_matches[match_id]["player_last_seen"]
    assert seen[PLAYER_B] == clock.now


def test_completed_ranked_match_status_reports_elo_change(
    client, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_B)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["status"] == "completed"
    assert body["winner_id"] == PLAYER_B
    assert body["elo_change"] > 0


# ---------------------------------------------------------------------------
# 9. status heartbeat bookkeeping
# ---------------------------------------------------------------------------


def test_status_poll_stores_exact_heartbeat_timestamp(client, auth_headers, clock):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)
    assert main.in_memory_matches[match_id]["player_last_seen"][PLAYER_B] == clock.now


def test_each_status_poll_advances_the_heartbeat(client, auth_headers, clock):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)
    t0 = main.in_memory_matches[match_id]["player_last_seen"][PLAYER_B]

    clock.advance(seconds=7)
    _status(client, auth_headers, match_id, PLAYER_B)
    t1 = main.in_memory_matches[match_id]["player_last_seen"][PLAYER_B]
    assert t1 == t0 + timedelta(seconds=7)


def test_status_poll_never_touches_the_opponent_heartbeat(
    client, auth_headers, clock
):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)
    before = main.in_memory_matches[match_id]["player_last_seen"][PLAYER_B]

    clock.advance(seconds=5)
    _status(client, auth_headers, match_id, PLAYER_A)
    seen = main.in_memory_matches[match_id]["player_last_seen"]
    assert seen[PLAYER_B] == before
    assert seen[PLAYER_A] == clock.now


# ---------------------------------------------------------------------------
# 10. give-up never awards points (or ELO)
# ---------------------------------------------------------------------------


def test_single_give_up_changes_no_scores(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    _give_up(client, auth_headers, match_id, PLAYER_A)

    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (0, 0)


def test_tie_give_up_awards_zero_points_to_both(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    body = _tie_current_round(client, auth_headers, match_id)

    assert body["player1_score"] == 0
    assert body["player2_score"] == 0
    match = main.in_memory_matches[match_id]
    assert match["status"] == "active"
    assert match["winner_id"] is None


def test_give_up_tie_in_ranked_writes_no_elo(
    client, auth_headers, fixed_question, elo_write_spy
):
    match_id = _ranked_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)

    assert elo_write_spy == []  # give-up path never touches users_collection
    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["elo_change"] == 0


def test_repeated_tie_rounds_never_finish_the_match(
    client, auth_headers, fixed_question
):
    # Four ties in a row: still 0-0, still active - only answers move scores.
    match_id = _friend_match(client, auth_headers)
    for round_number in range(1, 5):
        q = _question(client, auth_headers, match_id, PLAYER_A).json()
        assert q["round_id"] == f"round-{match_id}-{round_number}"
        _tie_current_round(client, auth_headers, match_id)

    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (0, 0)
    assert match["status"] == "active"


# ---------------------------------------------------------------------------
# 11. repeated give-up by the same player
# ---------------------------------------------------------------------------


def test_repeated_give_up_by_same_player_is_idempotent(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)

    first = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    second = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert first == second == {"status": "gave_up", "waiting_for_opponent": True}

    round_doc = main.in_memory_rounds[f"round-{match_id}-1"]
    assert round_doc["player1_gave_up"] is True
    assert round_doc["player2_gave_up"] is False
    assert round_doc.get("winner_id") is None


def test_opponent_give_up_after_repeats_still_resolves_tie(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    for _ in range(3):
        _give_up(client, auth_headers, match_id, PLAYER_A)

    body = _give_up(client, auth_headers, match_id, PLAYER_B).json()
    assert body["status"] == "both_gave_up"
    assert body["round_winner"] == "tie"


def test_give_up_repeat_while_opponent_now_stale_auto_resolves(
    client, auth_headers, fixed_question
):
    # First give-up waits (B fresh); B then goes silent; the retry auto-ties.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    assert _give_up(client, auth_headers, match_id, PLAYER_A).json()["status"] == (
        "gave_up"
    )

    _backdate_seen(match_id, PLAYER_B, 13)
    retry = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert retry["status"] == "both_gave_up"
    assert retry["round_winner"] == "tie"


# ---------------------------------------------------------------------------
# 12. ranked vs friend give-up parity
# ---------------------------------------------------------------------------


def test_single_give_up_response_identical_in_friend_and_ranked(
    client, auth_headers, fixed_question
):
    friend_id = _friend_match(client, auth_headers)
    ranked_id = _ranked_match(
        client, auth_headers, player1="guest-gus-rk1", player2="guest-gus-rk2"
    )
    _question(client, auth_headers, friend_id, PLAYER_A)
    _question(client, auth_headers, ranked_id, "guest-gus-rk1")
    _status(client, auth_headers, friend_id, PLAYER_B)
    _status(client, auth_headers, ranked_id, "guest-gus-rk2")

    friend_body = _give_up(client, auth_headers, friend_id, PLAYER_A).json()
    ranked_body = _give_up(client, auth_headers, ranked_id, "guest-gus-rk1").json()
    assert friend_body == ranked_body == {
        "status": "gave_up",
        "waiting_for_opponent": True,
    }


def test_tie_give_up_response_identical_in_friend_and_ranked(
    client, auth_headers, fixed_question
):
    friend_id = _friend_match(client, auth_headers)
    ranked_id = _ranked_match(
        client, auth_headers, player1="guest-gus-rk1", player2="guest-gus-rk2"
    )
    _question(client, auth_headers, friend_id, PLAYER_A)
    _question(client, auth_headers, ranked_id, "guest-gus-rk1")

    friend_tie = _tie_current_round(client, auth_headers, friend_id)
    first = _give_up(client, auth_headers, ranked_id, "guest-gus-rk1").json()
    assert first["status"] == "gave_up"
    ranked_tie = _give_up(client, auth_headers, ranked_id, "guest-gus-rk2").json()

    assert friend_tie == ranked_tie == {
        "status": "both_gave_up",
        "round_winner": "tie",
        "player1_score": 0,
        "player2_score": 0,
    }


def test_ranked_give_up_never_flags_bot_auto_mirror(
    client, auth_headers, fixed_question
):
    # The bot auto-give-up branch requires player2_id == "bot-opponent";
    # a human ranked opponent must NOT be auto-mirrored while connected.
    match_id = _ranked_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    _give_up(client, auth_headers, match_id, PLAYER_A)

    round_doc = main.in_memory_rounds[f"round-{match_id}-1"]
    assert round_doc["player1_gave_up"] is True
    assert round_doc["player2_gave_up"] is False


# ---------------------------------------------------------------------------
# 13. give up when both players are stale
# ---------------------------------------------------------------------------


def test_give_up_with_both_players_stale_resolves_tie(
    client, auth_headers, fixed_question
):
    # The caller's own staleness self-heals: give-up marks them seen before
    # checking presence, so only the opponent's staleness matters.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_A, 30)
    _backdate_seen(match_id, PLAYER_B, 30)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body["status"] == "both_gave_up"
    assert body["round_winner"] == "tie"


def test_both_stale_give_up_refreshes_only_the_caller(
    client, auth_headers, fixed_question, clock
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_A, 30)
    _backdate_seen(match_id, PLAYER_B, 30)

    _give_up(client, auth_headers, match_id, PLAYER_A)
    seen = main.in_memory_matches[match_id]["player_last_seen"]
    assert seen[PLAYER_A] == clock.now  # caller refreshed by the request
    assert seen[PLAYER_B] == clock.now - timedelta(seconds=30)  # B stays stale


# ---------------------------------------------------------------------------
# 14. outsider 403s
# ---------------------------------------------------------------------------


def test_outsider_403_on_ranked_status_and_give_up(
    client, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    assert _status(client, auth_headers, match_id, OUTSIDER).status_code == 403
    assert _give_up(client, auth_headers, match_id, OUTSIDER).status_code == 403
    # Neither rejected call leaked into the presence map or the round doc.
    assert OUTSIDER not in main.in_memory_matches[match_id].get(
        "player_last_seen", {}
    )
    round_doc = main.in_memory_rounds[f"round-{match_id}-1"]
    assert "player1_gave_up" not in round_doc


def test_outsider_403_even_on_completed_match(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)

    response = _status(client, auth_headers, match_id, OUTSIDER)
    assert response.status_code == 403
    assert response.json()["detail"] == "Not your match"


def test_outsider_403_on_waiting_friend_match_status(client, auth_headers):
    # Membership on a waiting match compares against str(None) == "None";
    # a normal guest outsider is still rejected.
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()
    response = _status(client, auth_headers, created["match_id"], OUTSIDER)
    assert response.status_code == 403


def test_current_behavior_identity_named_none_passes_waiting_membership(
    client, auth_headers
):
    # Latent quirk of the str(None) comparison above: an identity whose _id
    # stringifies to "None" would be admitted to ANY waiting friend match.
    # Unreachable via HTTP today (guest ids always start with "guest-",
    # JWT ids are ObjectIds), so pinned as current behavior, not xfail.
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()

    body = asyncio.run(
        main.get_game_status(created["match_id"], current_user={"_id": None})
    )
    assert body["status"] == "waiting"  # no 403 raised


# ---------------------------------------------------------------------------
# 15. status while still searching (no match yet)
# ---------------------------------------------------------------------------


def test_searching_player_has_no_pollable_match(client, auth_headers):
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    assert PLAYER_A in main.matchmaking_queue

    # No match id exists to poll; guessing the next counter id 404s.
    assert _status(client, auth_headers, "match-1", PLAYER_A).status_code == 404
    active = client.get("/api/game/active", headers=auth_headers(PLAYER_A)).json()
    assert active == {"has_active_match": False}


def test_searching_player_is_outsider_on_other_pairs_matches(
    client, auth_headers
):
    match_id = _ranked_match(
        client, auth_headers, player1="guest-gus-p1", player2="guest-gus-p2"
    )
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"

    response = _status(client, auth_headers, match_id, PLAYER_A)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# status round fields after round-cache eviction (found while testing #6/#8)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(status-round-blind-after-eviction): get_game_status reads the "
        "current round's winner/gave-up fields from in_memory_rounds ONLY - "
        "unlike question/answer/give-up it has no rounds_collection "
        "fallback.  After an eviction/restart the poller is told the round "
        "is still undecided (round_winner null, flags false) forever, even "
        "though the round doc in Mongo has a winner.  See the sibling test "
        "pinning current behavior."
    ),
)
def test_status_should_report_round_winner_after_round_cache_eviction(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)
    round_id = f"round-{match_id}-1"
    db_round = copy.deepcopy(main.in_memory_rounds.pop(round_id))

    async def find_one(query, *args, **kwargs):
        if query.get("_id") == round_id:
            return copy.deepcopy(db_round)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", find_one)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["round_winner"] == "tie"  # currently: None


def test_current_behavior_status_forgets_round_result_after_eviction(
    client, auth_headers, fixed_question
):
    # BUG pin: current (wrong) behavior of the xfail above.
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    del main.in_memory_rounds[f"round-{match_id}-1"]

    body = _status(client, auth_headers, match_id, PLAYER_B).json()
    assert body["round_winner"] is None  # the decided round looks open
    assert body["player1_gave_up"] is False
    assert body["player2_gave_up"] is False
    # The score survives (it lives on the match doc), deepening the mismatch.
    assert body["player1_score"] == 1
