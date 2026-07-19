"""
Edge-case tests for presence tracking and match lifecycle in people-vs-people
matches (friend + ranked).

Covers:
- mark_player_seen / is_player_connected unit behavior around the
  PRESENCE_TIMEOUT_SECONDS (12s) boundary (11.9s vs 12.1s), bots, and
  never-seen players
- /api/game/status/{id} presence reporting (opponent_connected) for both
  players, including leave/rejoin patterns
- stale-opponent give-up auto-resolution in /api/game/give-up
- lifecycle: active -> completed / abandoned, /api/game/active filtering,
  cancel_challenge on waiting friend matches, reconnect-window interplay
- match detail endpoints (/api/game/match/{code}, /match/{id}/details,
  /matches/all)

Conventions (same as the sibling edge-case files):
- Guest identities via "Bearer guest-xxx" tokens.
- Presence boundaries are tested deterministically by freezing main.utc_now;
  endpoint-level tests backdate player_last_seen with comfortable margins.
- Known bugs are documented with strict xfail markers plus sibling tests that
  pin the CURRENT behavior.  See MATCH_EDGE_CASE_REPORT.md.
"""

import copy
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

import main


PLAYER_A = "guest-pres-aaa"
PLAYER_B = "guest-pres-bbb"
OUTSIDER = "guest-pres-zzz"


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze main.utc_now so presence boundary math is exact."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(main, "utc_now", lambda: now)
    return now


def _start(client, auth_headers, player):
    response = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ranked_match(client, auth_headers, player1=PLAYER_A, player2=PLAYER_B):
    """
    Create a ranked match with `player1` in the player1 slot.

    start_match builds the match doc from the *joining* caller's perspective
    (the joiner becomes player1), so queue player2 first and join as player1.
    """
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
    """Make `player` look like their last status poll was `seconds` ago."""
    match = main.in_memory_matches[match_id]
    match.setdefault("player_last_seen", {})[str(player)] = main.utc_now() - timedelta(
        seconds=seconds
    )


def _win_round(client, auth_headers, match_id, player):
    """Fetch the current question and answer it correctly as `player`."""
    q = _question(client, auth_headers, match_id, player)
    assert q.status_code == 200, q.text
    r = _answer(client, auth_headers, match_id, player)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["correct"] is True, body
    return body


def _complete_match(client, auth_headers, match_id, winner=PLAYER_A):
    """Fast-forward to match point, then win the final round as `winner`."""
    match = main.in_memory_matches[match_id]
    if str(match["player1_id"]) == str(winner):
        match["player1_score"] = 2
    else:
        match["player2_score"] = 2
    body = _win_round(client, auth_headers, match_id, winner)
    assert body["match_winner"] == str(winner)
    return body


# ---------------------------------------------------------------------------
# is_player_connected / mark_player_seen unit behavior
# ---------------------------------------------------------------------------


def test_presence_timeout_constant_is_twelve_seconds():
    assert main.PRESENCE_TIMEOUT_SECONDS == 12


def test_never_seen_player_counts_as_connected():
    match = {"player_last_seen": {}}
    assert main.is_player_connected(match, PLAYER_A) is True


def test_match_without_last_seen_map_counts_connected():
    assert main.is_player_connected({}, PLAYER_A) is True


def test_connected_at_11_9_seconds(frozen_now):
    match = {
        "player_last_seen": {PLAYER_A: frozen_now - timedelta(seconds=11.9)}
    }
    assert main.is_player_connected(match, PLAYER_A) is True


def test_disconnected_at_12_1_seconds(frozen_now):
    match = {
        "player_last_seen": {PLAYER_A: frozen_now - timedelta(seconds=12.1)}
    }
    assert main.is_player_connected(match, PLAYER_A) is False


def test_exactly_twelve_seconds_is_still_connected(frozen_now):
    # The comparison is <= PRESENCE_TIMEOUT_SECONDS, so 12.000000 is inside.
    match = {"player_last_seen": {PLAYER_A: frozen_now - timedelta(seconds=12)}}
    assert main.is_player_connected(match, PLAYER_A) is True


def test_one_microsecond_past_twelve_seconds_disconnects(frozen_now):
    match = {
        "player_last_seen": {
            PLAYER_A: frozen_now - timedelta(seconds=12, microseconds=1)
        }
    }
    assert main.is_player_connected(match, PLAYER_A) is False


def test_bot_opponent_always_connected_even_when_stale(frozen_now):
    match = {
        "player_last_seen": {
            "bot-opponent": frozen_now - timedelta(seconds=9999)
        }
    }
    assert main.is_player_connected(match, "bot-opponent") is True


def test_bot_opponent_connected_without_any_bookkeeping():
    assert main.is_player_connected({}, "bot-opponent") is True


def test_mark_player_seen_creates_map_and_stores_now(frozen_now):
    match = {}
    main.mark_player_seen(match, PLAYER_A)
    assert match["player_last_seen"] == {PLAYER_A: frozen_now}


def test_mark_player_seen_updates_existing_timestamp(frozen_now):
    stale = frozen_now - timedelta(seconds=100)
    match = {"player_last_seen": {PLAYER_A: stale}}
    main.mark_player_seen(match, PLAYER_A)
    assert match["player_last_seen"][PLAYER_A] == frozen_now
    assert main.is_player_connected(match, PLAYER_A) is True


def test_mark_player_seen_stringifies_objectid_keys(frozen_now):
    oid = ObjectId()
    match = {}
    main.mark_player_seen(match, oid)
    assert list(match["player_last_seen"].keys()) == [str(oid)]
    # Lookup works with either the ObjectId or its string form.
    assert main.is_player_connected(match, oid) is True
    assert main.is_player_connected(match, str(oid)) is True


def test_stale_objectid_player_is_disconnected_via_either_form(frozen_now):
    oid = ObjectId()
    match = {"player_last_seen": {str(oid): frozen_now - timedelta(seconds=13)}}
    assert main.is_player_connected(match, oid) is False
    assert main.is_player_connected(match, str(oid)) is False


def test_naive_last_seen_datetime_is_treated_as_utc(frozen_now):
    # Mongo round-trips lose tzinfo; ensure_utc must keep the math correct.
    naive_recent = (frozen_now - timedelta(seconds=5)).replace(tzinfo=None)
    naive_stale = (frozen_now - timedelta(seconds=30)).replace(tzinfo=None)
    assert main.is_player_connected(
        {"player_last_seen": {PLAYER_A: naive_recent}}, PLAYER_A
    ) is True
    assert main.is_player_connected(
        {"player_last_seen": {PLAYER_A: naive_stale}}, PLAYER_A
    ) is False


# ---------------------------------------------------------------------------
# /api/game/status presence reporting
# ---------------------------------------------------------------------------


def test_first_status_poll_reports_never_seen_opponent_connected(
    client, auth_headers
):
    match_id = _ranked_match(client, auth_headers)
    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    # B has never polled anything, so they count as connected.
    assert body["opponent_connected"] is True


def test_status_poll_marks_caller_seen(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    assert "player_last_seen" not in main.in_memory_matches[match_id]

    _status(client, auth_headers, match_id, PLAYER_A)
    seen = main.in_memory_matches[match_id]["player_last_seen"]
    assert PLAYER_A in seen
    assert PLAYER_B not in seen


def test_opponent_connected_after_recent_poll(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["opponent_connected"] is True


def test_opponent_disconnected_after_long_silence(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_B, 14)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["opponent_connected"] is False


def test_opponent_still_connected_within_timeout(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_B, 10)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["opponent_connected"] is True


def test_leave_then_rejoin_restores_connected(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    _backdate_seen(match_id, PLAYER_B, 60)
    assert (
        _status(client, auth_headers, match_id, PLAYER_A).json()[
            "opponent_connected"
        ]
        is False
    )

    # B "rejoins" simply by polling again: their heartbeat refreshes.
    _status(client, auth_headers, match_id, PLAYER_B)
    assert (
        _status(client, auth_headers, match_id, PLAYER_A).json()[
            "opponent_connected"
        ]
        is True
    )


def test_presence_is_tracked_per_player_not_per_match(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_A, 20)

    # B sees A as gone; A (whose own heartbeat is stale) still sees B as here.
    assert (
        _status(client, auth_headers, match_id, PLAYER_B).json()[
            "opponent_connected"
        ]
        is False
    )
    assert (
        _status(client, auth_headers, match_id, PLAYER_A).json()[
            "opponent_connected"
        ]
        is True
    )


def test_question_endpoint_counts_as_heartbeat(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_B)
    seen = main.in_memory_matches[match_id]["player_last_seen"]
    assert PLAYER_B in seen


def test_answer_endpoint_counts_as_heartbeat(client, auth_headers, fixed_question):
    match_id = _ranked_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _answer(client, auth_headers, match_id, PLAYER_B, answer="wrong")
    seen = main.in_memory_matches[match_id]["player_last_seen"]
    assert PLAYER_B in seen


def test_give_up_endpoint_counts_as_heartbeat(client, auth_headers, fixed_question):
    match_id = _ranked_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _give_up(client, auth_headers, match_id, PLAYER_B)
    seen = main.in_memory_matches[match_id]["player_last_seen"]
    assert PLAYER_B in seen


def test_bot_opponent_reported_connected_even_with_stale_entry(
    client, auth_headers
):
    _start(client, auth_headers, PLAYER_A)
    main.matchmaking_queue[PLAYER_A]["joined_at"] = datetime.utcnow() - timedelta(
        seconds=11
    )
    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    match_id = body["match_id"]

    # Even a poisoned stale heartbeat for the bot must not matter.
    _backdate_seen(match_id, "bot-opponent", 9999)
    status = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert status["opponent_connected"] is True
    assert status["player2_name"] == "AI Opponent"


def test_status_404_for_unknown_match(client, auth_headers):
    response = _status(client, auth_headers, "match-does-not-exist", PLAYER_A)
    assert response.status_code == 404


def test_status_403_for_outsider(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    response = _status(client, auth_headers, match_id, OUTSIDER)
    assert response.status_code == 403
    # An outsider poll must not pollute the presence map either.
    assert OUTSIDER not in main.in_memory_matches[match_id].get(
        "player_last_seen", {}
    )


def test_status_response_has_exactly_expected_fields(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert set(body.keys()) == {
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


def test_status_field_values_identical_for_both_players_except_presence(
    client, auth_headers
):
    match_id = _ranked_match(client, auth_headers)
    body_a = _status(client, auth_headers, match_id, PLAYER_A).json()
    body_b = _status(client, auth_headers, match_id, PLAYER_B).json()

    # Player slots do not flip per caller: both players see the same board.
    for key in (
        "match_id",
        "player1_id",
        "player2_id",
        "player1_score",
        "player2_score",
        "status",
        "winner_id",
        "elo_change",
    ):
        assert body_a[key] == body_b[key]
    assert body_a["player1_id"] == PLAYER_A
    assert body_a["player2_id"] == PLAYER_B
    assert body_a["status"] == "active"
    assert body_a["winner_id"] is None
    assert body_a["elo_change"] == 0
    # opponent_connected is the only per-caller field.
    assert body_a["opponent_connected"] is True
    assert body_b["opponent_connected"] is True


def test_status_on_waiting_friend_match_stringifies_missing_player2(
    client, auth_headers
):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()
    body = _status(client, auth_headers, created["match_id"], PLAYER_A).json()

    # Quirk: str(None) leaks out, and the nonexistent opponent counts as
    # "connected" because they were never seen.
    assert body["status"] == "waiting"
    assert body["player2_id"] == "None"
    assert body["opponent_connected"] is True


def test_presence_map_is_not_persisted_across_memory_eviction(
    client, auth_headers, monkeypatch
):
    # Quirk: mark_player_seen only mutates the in-memory doc.  If the match
    # is evicted (e.g. server restart) and reloaded from the DB, all heartbeat
    # history is gone and a long-gone opponent counts as connected again.
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_B, 60)

    db_doc = copy.deepcopy(main.in_memory_matches[match_id])
    db_doc.pop("player_last_seen")  # presence never reaches the DB
    del main.in_memory_matches[match_id]

    async def find_one(query, *args, **kwargs):
        if query.get("_id") == match_id:
            return copy.deepcopy(db_doc)
        return None

    monkeypatch.setattr(main.matches_collection, "find_one", find_one)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["opponent_connected"] is True  # B's absence was forgotten
    # The DB copy was cached back into memory for future polls.
    assert match_id in main.in_memory_matches


# ---------------------------------------------------------------------------
# give-up flows and stale-opponent auto-resolution
# ---------------------------------------------------------------------------


def test_give_up_with_connected_opponent_waits(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body == {"status": "gave_up", "waiting_for_opponent": True}
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    assert main.in_memory_rounds[round_id].get("winner_id") is None


def test_give_up_with_never_seen_opponent_also_waits(
    client, auth_headers, fixed_question
):
    # Quirk: an opponent who has NEVER polled counts as connected, so the
    # give-up hangs waiting for someone who may never have been there.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body["status"] == "gave_up"
    assert body["waiting_for_opponent"] is True


def test_give_up_with_stale_opponent_resolves_tie_friend(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_B, 13)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body["status"] == "both_gave_up"
    assert body["round_winner"] == "tie"


def test_give_up_with_stale_opponent_resolves_tie_ranked(
    client, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_B, 13)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body["status"] == "both_gave_up"
    assert body["round_winner"] == "tie"
    round_id = main.in_memory_matches[match_id]["current_round_id"]
    round_doc = main.in_memory_rounds[round_id]
    assert round_doc["player1_gave_up"] is True
    assert round_doc["player2_gave_up"] is True


def test_stale_opponent_tie_awards_no_points(client, auth_headers, fixed_question):
    # Quirk: the remaining player gets a TIE, not a win, when the opponent
    # walked away - scores stay frozen so the match can never be won this way.
    match_id = _ranked_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_B, 13)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body["player1_score"] == 0
    assert body["player2_score"] == 0
    match = main.in_memory_matches[match_id]
    assert match["status"] == "active"
    assert match["winner_id"] is None


def test_both_players_give_up_sequentially_resolves_tie(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    first = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert first["status"] == "gave_up"
    second = _give_up(client, auth_headers, match_id, PLAYER_B).json()
    assert second["status"] == "both_gave_up"
    assert second["round_winner"] == "tie"


def test_give_up_after_round_won_returns_already_ended(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    body = _give_up(client, auth_headers, match_id, PLAYER_B).json()
    assert body == {"status": "already_ended", "round_winner": PLAYER_A}


def test_give_up_without_active_round_404(client, auth_headers):
    match_id = _friend_match(client, auth_headers)
    response = _give_up(client, auth_headers, match_id, PLAYER_A)
    assert response.status_code == 404
    assert response.json()["detail"] == "No active round"


def test_give_up_403_for_outsider(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    response = _give_up(client, auth_headers, match_id, OUTSIDER)
    assert response.status_code == 403


def test_status_shows_opponent_gave_up_flag(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_A)  # keep A "connected"
    _give_up(client, auth_headers, match_id, PLAYER_B)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["player2_gave_up"] is True
    assert body["player1_gave_up"] is False
    assert body["round_winner"] is None


def test_after_stale_tie_next_question_starts_fresh_round(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A).json()
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_B, 13)
    _give_up(client, auth_headers, match_id, PLAYER_A)

    follow_up = _question(client, auth_headers, match_id, PLAYER_A).json()
    assert follow_up["round_id"] != first["round_id"]
    assert follow_up["round_id"] == f"round-{match_id}-2"


def test_rejoined_opponent_shares_the_new_round_and_can_win_it(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_B, 13)
    _give_up(client, auth_headers, match_id, PLAYER_A)

    # B comes back: their question poll re-registers presence and they get
    # the same round 2 that A sees.
    round_for_b = _question(client, auth_headers, match_id, PLAYER_B).json()
    round_for_a = _question(client, auth_headers, match_id, PLAYER_A).json()
    assert round_for_b["round_id"] == round_for_a["round_id"]

    body = _answer(client, auth_headers, match_id, PLAYER_B).json()
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER_B
    assert body["player2_score"] == 1


# ---------------------------------------------------------------------------
# lifecycle: get_active_match filtering
# ---------------------------------------------------------------------------


def test_abandoned_ranked_match_not_returned_by_get_active_match(
    client, auth_headers
):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"

    for player in (PLAYER_A, PLAYER_B):
        body = client.get(
            "/api/game/active", headers=auth_headers(player)
        ).json()
        assert body == {"has_active_match": False}


def test_abandoned_friend_match_not_returned_by_get_active_match(
    client, auth_headers
):
    match_id = _friend_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"

    body = client.get("/api/game/active", headers=auth_headers(PLAYER_A)).json()
    assert body == {"has_active_match": False}


def test_waiting_friend_match_not_returned_by_get_active_match(
    client, auth_headers
):
    client.post("/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A))
    body = client.get("/api/game/active", headers=auth_headers(PLAYER_A)).json()
    assert body == {"has_active_match": False}


def test_completed_match_not_returned_by_get_active_match(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)

    for player in (PLAYER_A, PLAYER_B):
        body = client.get(
            "/api/game/active", headers=auth_headers(player)
        ).json()
        assert body == {"has_active_match": False}


def test_active_friend_match_is_reported_with_type(client, auth_headers):
    match_id = _friend_match(client, auth_headers)
    body = client.get("/api/game/active", headers=auth_headers(PLAYER_B)).json()
    assert body["has_active_match"] is True
    assert body["match_id"] == match_id
    assert body["match_type"] == "friend"


# ---------------------------------------------------------------------------
# lifecycle: completed matches
# ---------------------------------------------------------------------------


def test_completed_match_lifecycle_status_visible_to_both(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)

    for player in (PLAYER_A, PLAYER_B):
        body = _status(client, auth_headers, match_id, player).json()
        assert body["status"] == "completed"
        assert body["winner_id"] == PLAYER_A
        assert body["player1_score"] == 3


def test_completed_match_rejects_new_questions(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)

    response = _question(client, auth_headers, match_id, PLAYER_B)
    assert response.status_code == 400
    assert response.json()["detail"] == "Match is already completed"


def test_completed_match_rejects_answers(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)

    response = _answer(client, auth_headers, match_id, PLAYER_B)
    assert response.status_code == 400
    assert response.json()["detail"] == "Match is already completed"


def test_give_up_still_allowed_on_completed_match(
    client, auth_headers, fixed_question
):
    # Quirk: give_up_round never checks match status; on a completed match it
    # reports the final round as already ended instead of rejecting.
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)

    body = _give_up(client, auth_headers, match_id, PLAYER_B)
    assert body.status_code == 200
    assert body.json() == {"status": "already_ended", "round_winner": PLAYER_A}


def test_presence_still_tracked_on_completed_match(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id, PLAYER_A)
    _backdate_seen(match_id, PLAYER_B, 60)

    body = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert body["status"] == "completed"
    assert body["opponent_connected"] is False


# ---------------------------------------------------------------------------
# lifecycle: abandoned matches and gameplay routes
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(zombie-abandoned-match): gameplay routes only reject status "
        "'completed'.  An abandoned match keeps serving fresh questions (and "
        "accepting answers), even though abandonment exists precisely to end "
        "it.  See the sibling test pinning current behavior."
    ),
)
def test_abandoned_match_should_not_serve_questions(
    client, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"

    response = _question(client, auth_headers, match_id, PLAYER_A)
    assert response.status_code == 400  # currently: 200 with a new round


def test_current_behavior_abandoned_match_still_serves_questions(
    client, auth_headers, fixed_question
):
    # BUG: pins the current (wrong) behavior of the xfail above.
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"

    response = _question(client, auth_headers, match_id, PLAYER_A)
    assert response.status_code == 200
    assert response.json()["round_id"] == f"round-{match_id}-1"
    # The match even keeps accepting answers while abandoned.
    body = _answer(client, auth_headers, match_id, PLAYER_A).json()
    assert body["correct"] is True
    assert main.in_memory_matches[match_id]["player1_score"] == 1


def test_status_endpoint_still_reports_abandoned_matches(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"

    body = _status(client, auth_headers, match_id, PLAYER_B).json()
    assert body["status"] == "abandoned"
    assert body["winner_id"] is None


# ---------------------------------------------------------------------------
# reconnect window vs presence
# ---------------------------------------------------------------------------


def test_reconnect_window_preserves_presence_data(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)

    # A re-polls /api/game/start within the <5s reconnect window.
    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == match_id
    # B's heartbeat survived the reconnect.
    assert PLAYER_B in main.in_memory_matches[match_id]["player_last_seen"]


def test_stale_opponent_presence_does_not_trigger_abandonment(
    client, auth_headers
):
    # Presence and the reconnect window are independent: a recent match is
    # reconnected even if the opponent's heartbeat says they are long gone.
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)
    _backdate_seen(match_id, PLAYER_B, 300)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == match_id
    assert main.in_memory_matches[match_id]["status"] == "active"


def test_abandonment_after_window_hides_match_from_both_players(
    client, auth_headers
):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow() - timedelta(
        seconds=6
    )

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "abandoned"

    for player in (PLAYER_A, PLAYER_B):
        active = client.get(
            "/api/game/active", headers=auth_headers(player)
        ).json()
        assert active == {"has_active_match": False}
    # The other player only finds out by polling status.
    status = _status(client, auth_headers, match_id, PLAYER_B).json()
    assert status["status"] == "abandoned"


def test_abandonment_keeps_presence_history(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_B)
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow() - timedelta(
        seconds=6
    )
    _start(client, auth_headers, PLAYER_A)

    match = main.in_memory_matches[match_id]
    assert match["status"] == "abandoned"
    assert PLAYER_B in match["player_last_seen"]


# ---------------------------------------------------------------------------
# cancel_challenge on waiting friend matches
# ---------------------------------------------------------------------------


@pytest.fixture
def db_backed_matches(mock_mongo, monkeypatch):
    """
    matches_collection.find_one/delete_one that mirror in_memory_matches so
    the DB-only challenge endpoints can see friend matches created in tests.
    Returns the list of delete_one filters for assertions.
    """
    deleted = []

    async def find_one(query, *args, **kwargs):
        match_id = query.get("_id")
        if match_id is not None:
            doc = main.in_memory_matches.get(match_id)
            return copy.deepcopy(doc) if doc else None
        code = query.get("match_code")
        if code is not None:
            for doc in main.in_memory_matches.values():
                if doc.get("match_code") == code:
                    return copy.deepcopy(doc)
        return None

    async def delete_one(query, *args, **kwargs):
        deleted.append(query)
        return None

    monkeypatch.setattr(main.matches_collection, "find_one", find_one)
    monkeypatch.setattr(main.matches_collection, "delete_one", delete_one)
    return deleted


def test_cancel_challenge_removes_waiting_friend_match(
    client, auth_headers, db_backed_matches
):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()
    match_id = created["match_id"]

    response = client.post(
        f"/api/challenges/cancel/{match_id}", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200
    assert response.json() == {"status": "cancelled"}
    assert match_id not in main.in_memory_matches
    assert db_backed_matches == [{"_id": match_id}]


def test_cancelled_waiting_match_code_is_dead(
    client, auth_headers, db_backed_matches
):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()
    client.post(
        f"/api/challenges/cancel/{created['match_id']}",
        headers=auth_headers(PLAYER_A),
    )

    poll = client.get(f"/api/game/friend/status/{created['match_code']}")
    assert poll.status_code == 404
    join = client.post(
        "/api/game/friend/join",
        json={"match_code": created["match_code"]},
        headers=auth_headers(PLAYER_B),
    )
    assert join.status_code == 404


def test_cancel_challenge_rejected_once_match_is_active(
    client, auth_headers, db_backed_matches
):
    match_id = _friend_match(client, auth_headers)
    response = client.post(
        f"/api/challenges/cancel/{match_id}", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Challenge already active or completed"
    assert main.in_memory_matches[match_id]["status"] == "active"


def test_cancel_challenge_403_for_non_creator(
    client, auth_headers, db_backed_matches
):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()

    response = client.post(
        f"/api/challenges/cancel/{created['match_id']}",
        headers=auth_headers(PLAYER_B),
    )
    assert response.status_code == 403
    assert created["match_id"] in main.in_memory_matches


# ---------------------------------------------------------------------------
# match detail endpoints
# ---------------------------------------------------------------------------


def test_match_by_code_perspectives_for_both_players(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    code = main.in_memory_matches[match_id]["match_code"]

    body_a = client.get(
        f"/api/game/match/{code}", headers=auth_headers(PLAYER_A)
    ).json()
    body_b = client.get(
        f"/api/game/match/{code}", headers=auth_headers(PLAYER_B)
    ).json()

    assert body_a["is_player1"] is True
    assert body_b["is_player1"] is False
    for body in (body_a, body_b):
        assert body["match_id"] == match_id
        assert body["status"] == "active"
        assert body["player1_id"] == PLAYER_A
        assert body["player2_id"] == PLAYER_B
        assert body["is_opponent_bot"] is False
        assert body["opponent_name"] == "Guest"  # guest ids contain "guest"
        assert body["current_round"] == 0


def test_match_details_endpoint_shape_for_friend_match(client, auth_headers):
    match_id = _friend_match(client, auth_headers)

    response = client.get(
        f"/match/{match_id}/details", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["match_id"] == match_id
    assert body["match_type"] == "friend"
    assert body["score"] == "0-0"
    assert body["status"] == "active"
    assert body["winner"] is None
    assert body["elo_change"] == 0
    assert body["rounds"] == []
    assert body["player1"]["id"] == PLAYER_A
    assert body["player2"]["id"] == PLAYER_B
    # Guests have no user doc, so the fallback labels are used.
    assert body["player1"]["username"] == "Player 1"
    assert body["player2"]["username"] == "Player 2"


def test_match_details_endpoint_404_for_unknown_match(client, auth_headers):
    response = client.get(
        "/match/match-nope/details", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 404


def test_matches_all_endpoint_returns_list(client, auth_headers):
    # With the DB mocked empty this endpoint returns [] even though matches
    # exist in memory - it reads exclusively from the database.
    _friend_match(client, auth_headers)
    response = client.get("/matches/all", headers=auth_headers(PLAYER_A))
    assert response.status_code == 200
    assert response.json() == []
