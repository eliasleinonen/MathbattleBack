"""
Edge-case tests for ranked/queue matchmaking between people.

Covers /api/game/start, /api/game/cancel and /api/game/active plus the
process-local state behind them (matchmaking_queue, cancelled_users,
in_memory_matches, bot fallback, the <5s reconnect window).

Conventions used here:
- Guest identities via "Bearer guest-xxx" tokens (see get_current_user).
- Time-based branches are exercised by backdating the naive utcnow
  timestamps stored in matchmaking_queue / in_memory_matches instead of
  monkeypatching datetime, which is what the production code compares
  against (datetime.utcnow()).
- Tests that document real bugs are marked xfail with the bug named in the
  reason, and each has a sibling test that pins the CURRENT behavior with a
  "BUG:" comment so regressions in either direction are visible.
"""

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

import main


PLAYER_A = "guest-mm-aaa"
PLAYER_B = "guest-mm-bbb"
PLAYER_C = "guest-mm-ccc"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _start(client, auth_headers, player, mode="random", continue_existing=False):
    response = client.post(
        "/api/game/start",
        json={"mode": mode, "continue_existing": continue_existing},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cancel(client, auth_headers, player):
    response = client.post("/api/game/cancel", headers=auth_headers(player))
    assert response.status_code == 200, response.text
    return response.json()


def _active(client, auth_headers, player):
    response = client.get("/api/game/active", headers=auth_headers(player))
    assert response.status_code == 200, response.text
    return response.json()


def _backdate_queue_entry(user_id, seconds):
    """Make a queued user look like they joined `seconds` ago."""
    main.matchmaking_queue[user_id]["joined_at"] = datetime.utcnow() - timedelta(
        seconds=seconds
    )


def _backdate_match(match_id, seconds):
    """Make a match look like it was created `seconds` ago."""
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow() - timedelta(
        seconds=seconds
    )


def _make_ranked_match(client, auth_headers, player1=PLAYER_A, player2=PLAYER_B):
    """Queue player1, then let player2 arrive and match. Returns match_id."""
    first = _start(client, auth_headers, player1)
    assert first["status"] == "searching"
    second = _start(client, auth_headers, player2)
    assert second["status"] == "matched", second
    return second["match_id"]


# ---------------------------------------------------------------------------
# response shapes (cases 17, 18)
# ---------------------------------------------------------------------------


def test_first_start_returns_searching_shape(client, auth_headers):
    body = _start(client, auth_headers, PLAYER_A)
    assert body == {"status": "searching", "time_remaining": 10}
    assert PLAYER_A in main.matchmaking_queue


def test_searching_poll_counts_down_time_remaining(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    _backdate_queue_entry(PLAYER_A, 4)

    poll = _start(client, auth_headers, PLAYER_A)
    assert poll["status"] == "searching"
    # int(10 - ~4.0s); allow for the truncation boundary
    assert poll["time_remaining"] in (5, 6)


def test_searching_response_has_exactly_expected_keys(client, auth_headers):
    body = _start(client, auth_headers, PLAYER_A)
    assert set(body.keys()) == {"status", "time_remaining"}


def test_matched_response_shape_for_joining_player(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    body = _start(client, auth_headers, PLAYER_B)

    assert set(body.keys()) == {"status", "match_id", "match_code", "opponent"}
    assert body["status"] == "matched"
    assert body["match_id"]
    assert body["match_code"]
    # Guest opponents have no username in the (mocked-empty) DB.
    assert body["opponent"] == "Player"


def test_matched_response_includes_same_match_id_for_both_players(
    client, auth_headers
):
    _start(client, auth_headers, PLAYER_A)
    matched_b = _start(client, auth_headers, PLAYER_B)

    # Player A polls start again and must be routed into the same match
    # through the <5s reconnect window.
    matched_a = _start(client, auth_headers, PLAYER_A)
    assert matched_a["status"] == "matched"
    assert matched_a["match_id"] == matched_b["match_id"]
    assert matched_a["match_code"] == matched_b["match_code"]
    # No duplicate match was created for the reconnect.
    assert len(main.in_memory_matches) == 1


# ---------------------------------------------------------------------------
# match document contents (cases 8, 13, 14)
# ---------------------------------------------------------------------------


def test_ranked_match_document_fields(client, auth_headers):
    match_id = _make_ranked_match(client, auth_headers)
    match = main.in_memory_matches[match_id]

    assert match["match_type"] == "ranked"
    assert match["status"] == "active"
    assert {str(match["player1_id"]), str(match["player2_id"])} == {
        PLAYER_A,
        PLAYER_B,
    }
    assert match["player1_score"] == 0 and match["player2_score"] == 0
    assert match["winner_id"] is None
    assert match["elo_change"] == 0
    assert match["rounds"] == []
    assert isinstance(match["created_at"], datetime)


def test_elo_is_snapshotted_into_match_at_creation(client, auth_headers):
    match_id = _make_ranked_match(client, auth_headers)
    match = main.in_memory_matches[match_id]

    # Guests always carry elo 1000. Note the queue entry stores the joining
    # user's elo, but for non-ObjectId (guest) opponents start_match ignores
    # the queue snapshot and uses a hardcoded {"elo": 1000} fallback.
    assert match["player1_elo"] == 1000
    assert match["player2_elo"] == 1000


def test_match_code_is_urlsafe_and_unique_across_matches(client, auth_headers):
    first_id = _make_ranked_match(client, auth_headers, PLAYER_A, PLAYER_B)
    first_code = main.in_memory_matches[first_id]["match_code"]

    # secrets.token_urlsafe(8) -> 11 chars from the urlsafe alphabet
    assert len(first_code) == 11
    assert all(c.isalnum() or c in "-_" for c in first_code)

    second_id = _make_ranked_match(client, auth_headers, PLAYER_C, "guest-mm-ddd")
    second_code = main.in_memory_matches[second_id]["match_code"]
    assert second_code != first_code
    assert second_id != first_id


def test_match_by_code_labels_guest_opponent_and_not_bot(client, auth_headers):
    match_id = _make_ranked_match(client, auth_headers)
    code = main.in_memory_matches[match_id]["match_code"]

    response = client.get(f"/api/game/match/{code}", headers=auth_headers(PLAYER_A))
    assert response.status_code == 200
    body = response.json()
    assert body["match_id"] == match_id
    assert body["is_opponent_bot"] is False
    # Guest ids contain "guest" so the by-code endpoint labels them "Guest".
    assert body["opponent_name"] == "Guest"


# ---------------------------------------------------------------------------
# queue bookkeeping (cases 3, 4, 10)
# ---------------------------------------------------------------------------


def test_queue_entries_removed_after_successful_match(client, auth_headers):
    _make_ranked_match(client, auth_headers)
    assert PLAYER_A not in main.matchmaking_queue
    assert PLAYER_B not in main.matchmaking_queue
    assert main.matchmaking_queue == {}


def test_user_cannot_match_with_self_by_double_polling(client, auth_headers):
    first = _start(client, auth_headers, PLAYER_A)
    second = _start(client, auth_headers, PLAYER_A)

    assert first["status"] == "searching"
    assert second["status"] == "searching"
    assert main.in_memory_matches == {}
    # Still exactly one queue entry for the user.
    assert list(main.matchmaking_queue.keys()) == [PLAYER_A]


def test_three_users_first_two_match_third_keeps_searching(client, auth_headers):
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    matched = _start(client, auth_headers, PLAYER_B)
    assert matched["status"] == "matched"

    third = _start(client, auth_headers, PLAYER_C)
    assert third["status"] == "searching"
    assert list(main.matchmaking_queue.keys()) == [PLAYER_C]
    assert len(main.in_memory_matches) == 1
    match = main.in_memory_matches[matched["match_id"]]
    assert PLAYER_C not in (str(match["player1_id"]), str(match["player2_id"]))


def test_two_users_both_queued_manually_pair_on_next_poll(client, auth_headers):
    # Simulates the state after two concurrent first polls: both searching.
    now = datetime.utcnow()
    main.matchmaking_queue[PLAYER_A] = {"elo": 1000, "joined_at": now}
    main.matchmaking_queue[PLAYER_B] = {"elo": 1000, "joined_at": now}

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    match = main.in_memory_matches[body["match_id"]]
    assert {str(match["player1_id"]), str(match["player2_id"])} == {
        PLAYER_A,
        PLAYER_B,
    }
    assert main.matchmaking_queue == {}


# ---------------------------------------------------------------------------
# cancel flows (cases 1, 2, 9, 16)
# ---------------------------------------------------------------------------


def test_cancel_while_searching_removes_queue_entry(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    body = _cancel(client, auth_headers, PLAYER_A)

    assert body == {"status": "cancelled"}
    assert PLAYER_A not in main.matchmaking_queue
    assert PLAYER_A in main.cancelled_users


def test_opponent_arriving_after_cancel_does_not_ghost_match(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    _cancel(client, auth_headers, PLAYER_A)

    # B arrives after A cancelled: B must not be paired with A.
    body = _start(client, auth_headers, PLAYER_B)
    assert body["status"] == "searching"
    assert main.in_memory_matches == {}
    assert list(main.matchmaking_queue.keys()) == [PLAYER_B]


def test_cancel_then_requeue_returns_searching(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    _cancel(client, auth_headers, PLAYER_A)

    requeued = _start(client, auth_headers, PLAYER_A)
    assert requeued["status"] == "searching"
    assert PLAYER_A in main.matchmaking_queue
    # BUG: re-queueing does NOT clear the cancelled flag, which poisons the
    # next pairing attempt (see the xfail test below).
    assert PLAYER_A in main.cancelled_users


@pytest.mark.xfail(
    reason=(
        "BUG(stale-cancel-flag): /api/game/start does not remove the user from "
        "cancelled_users when they re-queue after cancelling. The next opponent "
        "that would pair with them gets a bogus {'status': 'cancelled'} response "
        "and the re-queued user is silently dropped from the queue."
    )
)
def test_requeued_user_after_cancel_is_matchable_again(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    _cancel(client, auth_headers, PLAYER_A)
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"

    body = _start(client, auth_headers, PLAYER_B)
    assert body["status"] == "matched"  # currently: "cancelled"


def test_current_behavior_stale_cancel_flag_aborts_pairing(client, auth_headers):
    # BUG: pins the CURRENT (wrong) behavior described in the xfail above so
    # any change in either direction is caught.
    _start(client, auth_headers, PLAYER_A)
    _cancel(client, auth_headers, PLAYER_A)
    _start(client, auth_headers, PLAYER_A)  # re-queue; stale flag remains

    body = _start(client, auth_headers, PLAYER_B)
    assert body == {"status": "cancelled"}  # B never cancelled!
    assert main.in_memory_matches == {}
    # Both were popped from the queue: A is silently dropped, B never joined.
    assert main.matchmaking_queue == {}
    # The stale flag was at least consumed.
    assert PLAYER_A not in main.cancelled_users
    assert PLAYER_B not in main.cancelled_users


def test_cancelled_user_stuck_in_queue_is_not_paired(client, auth_headers):
    # Weird state: cancel raced so the user is BOTH queued and cancelled.
    main.matchmaking_queue[PLAYER_A] = {
        "elo": 1000,
        "joined_at": datetime.utcnow(),
    }
    main.cancelled_users.add(PLAYER_A)

    body = _start(client, auth_headers, PLAYER_B)
    assert body["status"] == "cancelled"
    assert main.in_memory_matches == {}
    assert PLAYER_A not in main.matchmaking_queue
    assert PLAYER_A not in main.cancelled_users


def test_cancel_after_match_creation_does_not_cancel_the_match(
    client, auth_headers
):
    # Race (case 2): B pairs with A while A's cancel request is in flight.
    _start(client, auth_headers, PLAYER_A)
    matched = _start(client, auth_headers, PLAYER_B)
    assert matched["status"] == "matched"

    _cancel(client, auth_headers, PLAYER_A)

    # A's next poll reconnects them to the already-created match; the match
    # is not torn down by a late cancel.
    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == matched["match_id"]
    assert main.in_memory_matches[matched["match_id"]]["status"] == "active"
    # BUG: the cancelled flag is never consumed on this path, so it lingers
    # and can abort A's next pairing after this match ends (stale-cancel-flag).
    assert PLAYER_A in main.cancelled_users


# ---------------------------------------------------------------------------
# reconnect window and abandonment (cases 5, 6, 11)
# ---------------------------------------------------------------------------


def test_reconnect_to_match_created_less_than_five_seconds_ago(
    client, auth_headers
):
    match_id = _make_ranked_match(client, auth_headers)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == match_id
    assert body["match_code"] == main.in_memory_matches[match_id]["match_code"]
    # Reconnecting must not re-enter the queue.
    assert PLAYER_A not in main.matchmaking_queue


def test_active_match_older_than_five_seconds_is_abandoned_on_new_search(
    client, auth_headers
):
    match_id = _make_ranked_match(client, auth_headers)
    _backdate_match(match_id, 10)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "abandoned"
    assert PLAYER_A in main.matchmaking_queue


def test_match_exactly_at_boundary_still_within_reconnect_window(
    client, auth_headers
):
    match_id = _make_ranked_match(client, auth_headers)
    _backdate_match(match_id, 4)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == match_id


def test_continue_existing_true_skips_abandonment_but_still_searches(
    client, auth_headers
):
    match_id = _make_ranked_match(client, auth_headers)
    _backdate_match(match_id, 10)

    body = _start(client, auth_headers, PLAYER_A, continue_existing=True)
    # BUG/quirk: continue_existing=True does NOT return the existing match.
    # It only skips marking it abandoned and then falls through to normal
    # matchmaking, so the caller is told "searching" while their old match
    # silently stays active.
    assert body["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "active"
    assert PLAYER_A in main.matchmaking_queue


def test_continue_existing_with_queued_opponent_creates_second_active_match(
    client, auth_headers
):
    old_match_id = _make_ranked_match(client, auth_headers)
    _backdate_match(old_match_id, 10)
    main.matchmaking_queue[PLAYER_C] = {
        "elo": 1000,
        "joined_at": datetime.utcnow(),
    }

    body = _start(client, auth_headers, PLAYER_A, continue_existing=True)
    # BUG/quirk: player A now has TWO simultaneously active matches.
    assert body["status"] == "matched"
    assert body["match_id"] != old_match_id
    assert main.in_memory_matches[old_match_id]["status"] == "active"
    assert main.in_memory_matches[body["match_id"]]["status"] == "active"

    # get_active_match returns the first active match in insertion order,
    # i.e. the OLD match, not the one just created.
    active = _active(client, auth_headers, PLAYER_A)
    assert active["has_active_match"] is True
    assert active["match_id"] == old_match_id


# ---------------------------------------------------------------------------
# bot fallback (cases 7, 8)
# ---------------------------------------------------------------------------


def test_still_searching_before_ten_seconds_no_bot(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    _backdate_queue_entry(PLAYER_A, 9)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert main.in_memory_matches == {}


def test_bot_fallback_after_ten_seconds_in_queue(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    _backdate_queue_entry(PLAYER_A, 11)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["opponent"].endswith("(bot)")
    assert PLAYER_A not in main.matchmaking_queue


def test_bot_match_fields(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    _backdate_queue_entry(PLAYER_A, 11)
    body = _start(client, auth_headers, PLAYER_A)

    match = main.in_memory_matches[body["match_id"]]
    assert match["match_type"] == "random"  # bot matches are "random"
    assert match["player2_id"] == "bot-opponent"
    assert str(match["player1_id"]) == PLAYER_A
    assert match["player1_elo"] == 1000
    # Bot is 50-150 elo below the player.
    assert 850 <= match["player2_elo"] <= 950
    assert match["status"] == "active"


def test_bot_fallback_respects_cancelled_flag(client, auth_headers):
    # User is queued past the bot deadline but cancelled in the meantime
    # (flag set without the queue pop, simulating the race).
    _start(client, auth_headers, PLAYER_A)
    _backdate_queue_entry(PLAYER_A, 11)
    main.cancelled_users.add(PLAYER_A)

    body = _start(client, auth_headers, PLAYER_A)
    assert body == {"status": "cancelled"}
    assert main.in_memory_matches == {}
    assert PLAYER_A not in main.matchmaking_queue
    assert PLAYER_A not in main.cancelled_users


def test_human_opponent_preferred_over_bot_even_after_timeout(
    client, auth_headers
):
    # Both users have waited past the 10s bot deadline; the opponent scan
    # runs before the timeout check, so they pair with each other.
    stale = datetime.utcnow() - timedelta(seconds=12)
    main.matchmaking_queue[PLAYER_A] = {"elo": 1000, "joined_at": stale}
    main.matchmaking_queue[PLAYER_B] = {"elo": 1000, "joined_at": stale}

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    match = main.in_memory_matches[body["match_id"]]
    assert match["match_type"] == "ranked"
    assert match["player2_id"] != "bot-opponent"


# ---------------------------------------------------------------------------
# /api/game/active (case 12)
# ---------------------------------------------------------------------------


def test_get_active_match_while_searching_is_false(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)
    body = _active(client, auth_headers, PLAYER_A)
    assert body == {"has_active_match": False}


def test_get_active_match_after_matching_is_true_for_both(client, auth_headers):
    match_id = _make_ranked_match(client, auth_headers)

    for player in (PLAYER_A, PLAYER_B):
        body = _active(client, auth_headers, player)
        assert body["has_active_match"] is True
        assert body["match_id"] == match_id
        assert body["match_type"] == "ranked"


def test_abandoned_match_not_reported_as_active(client, auth_headers):
    match_id = _make_ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"

    body = _active(client, auth_headers, PLAYER_A)
    assert body == {"has_active_match": False}


# ---------------------------------------------------------------------------
# concurrency (case 15)
# ---------------------------------------------------------------------------


def test_concurrent_start_from_two_guests_creates_single_match(mock_mongo):
    async def run():
        return await asyncio.gather(
            main.start_match(
                main.MatchStart(mode="random"),
                current_user={"_id": PLAYER_A, "elo": 1000},
            ),
            main.start_match(
                main.MatchStart(mode="random"),
                current_user={"_id": PLAYER_B, "elo": 1000},
            ),
        )

    first, second = asyncio.run(run())

    # For guest ids the queue check-and-pop runs without await points, so
    # the two coroutines serialize: one queues, the other pairs with it.
    statuses = sorted([first["status"], second["status"]])
    assert statuses == ["matched", "searching"]
    assert len(main.in_memory_matches) == 1


def test_concurrent_joiners_with_guest_queued_player_no_double_pairing(mock_mongo):
    main.matchmaking_queue[PLAYER_A] = {
        "elo": 1000,
        "joined_at": datetime.utcnow(),
    }

    async def run():
        return await asyncio.gather(
            main.start_match(
                main.MatchStart(mode="random"),
                current_user={"_id": PLAYER_B, "elo": 1000},
            ),
            main.start_match(
                main.MatchStart(mode="random"),
                current_user={"_id": PLAYER_C, "elo": 1000},
            ),
        )

    first, second = asyncio.run(run())
    statuses = sorted([first["status"], second["status"]])
    assert statuses == ["matched", "searching"]

    matches_with_a = [
        m
        for m in main.in_memory_matches.values()
        if PLAYER_A in (str(m["player1_id"]), str(m["player2_id"]))
    ]
    assert len(matches_with_a) == 1


@pytest.mark.xfail(
    reason=(
        "BUG(pairing-race): start_match awaits users_collection.find_one for "
        "ObjectId opponents BETWEEN selecting the opponent from the queue and "
        "popping them, so two concurrent callers can both pair with the same "
        "queued player, creating two active matches for that player."
    )
)
def test_concurrent_joiners_with_objectid_queued_player_no_double_pairing(
    mock_mongo, monkeypatch
):
    async def yielding_find_one(*args, **kwargs):
        await asyncio.sleep(0)  # force interleaving at the await point
        return None

    monkeypatch.setattr(main.users_collection, "find_one", yielding_find_one)
    monkeypatch.setattr(main.matches_collection, "find_one", yielding_find_one)

    queued_id = str(ObjectId())
    main.matchmaking_queue[queued_id] = {
        "elo": 1000,
        "joined_at": datetime.utcnow(),
    }

    async def run():
        return await asyncio.gather(
            main.start_match(
                main.MatchStart(mode="random"),
                current_user={"_id": ObjectId(), "elo": 1000},
            ),
            main.start_match(
                main.MatchStart(mode="random"),
                current_user={"_id": ObjectId(), "elo": 1000},
            ),
        )

    asyncio.run(run())

    matches_with_queued = [
        m
        for m in main.in_memory_matches.values()
        if queued_id in (str(m["player1_id"]), str(m["player2_id"]))
    ]
    assert len(matches_with_queued) == 1  # currently: 2


# ---------------------------------------------------------------------------
# sequential matches (case 19)
# ---------------------------------------------------------------------------


def test_same_user_can_match_again_after_completing_prior_match(
    client, auth_headers
):
    first_id = _make_ranked_match(client, auth_headers)
    # Simulate the first match finishing.
    main.in_memory_matches[first_id]["status"] = "completed"

    # A re-queues; completed match is neither reconnected nor abandoned.
    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert main.in_memory_matches[first_id]["status"] == "completed"

    # B re-queues too and pairs with A into a fresh match.
    second = _start(client, auth_headers, PLAYER_B)
    assert second["status"] == "matched"
    assert second["match_id"] != first_id
    assert (
        main.in_memory_matches[second["match_id"]]["match_code"]
        != main.in_memory_matches[first_id]["match_code"]
    )


def test_completed_match_does_not_trigger_reconnect_window(client, auth_headers):
    first_id = _make_ranked_match(client, auth_headers)
    main.in_memory_matches[first_id]["status"] = "completed"
    # Even a brand-new completed match (age < 5s) must not be "reconnected".
    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"


# ---------------------------------------------------------------------------
# auth edge cases (case 20)
# ---------------------------------------------------------------------------


def test_missing_auth_falls_back_to_shared_guest_identity(client, auth_headers):
    # BUG/quirk: requests without any Authorization header all map to the
    # single shared identity "guest-user-id" (demo mode), so two anonymous
    # browsers share one queue slot and can never match each other.
    response = client.post("/api/game/start", json={"mode": "random"})
    assert response.status_code == 200
    assert response.json()["status"] == "searching"
    assert "guest-user-id" in main.matchmaking_queue

    again = client.post("/api/game/start", json={"mode": "random"})
    assert again.json()["status"] == "searching"  # self-match is impossible
    assert len(main.matchmaking_queue) == 1


def test_invalid_jwt_token_falls_back_to_default_guest(client, auth_headers):
    response = client.post(
        "/api/game/start",
        json={"mode": "random"},
        headers={"Authorization": "Bearer definitely-not-a-valid-jwt"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "searching"
    assert "guest-user-id" in main.matchmaking_queue


def test_anonymous_user_can_match_with_explicit_guest(client, auth_headers):
    response = client.post("/api/game/start", json={"mode": "random"})
    assert response.json()["status"] == "searching"

    body = _start(client, auth_headers, PLAYER_B)
    assert body["status"] == "matched"
    match = main.in_memory_matches[body["match_id"]]
    assert {str(match["player1_id"]), str(match["player2_id"])} == {
        "guest-user-id",
        PLAYER_B,
    }


def test_explicit_guest_user_id_token_is_same_identity_as_no_auth(
    client, auth_headers
):
    # "guest-user-id" itself starts with "guest-" so the explicit token and
    # the no-auth fallback collide into one identity.
    client.post("/api/game/start", json={"mode": "random"})
    body = _start(client, auth_headers, "guest-user-id")
    assert body["status"] == "searching"  # same user -> no self-match
    assert len(main.matchmaking_queue) == 1


# ---------------------------------------------------------------------------
# misc quirks
# ---------------------------------------------------------------------------


def test_mode_field_is_ignored_by_start_match(client, auth_headers):
    # Quirk: MatchStart.mode is never read by start_match; "friend" (or any
    # string) still enters the ranked queue.
    body = _start(client, auth_headers, PLAYER_A, mode="friend")
    assert body["status"] == "searching"

    matched = _start(client, auth_headers, PLAYER_B, mode="anything-goes")
    assert matched["status"] == "matched"
    assert main.in_memory_matches[matched["match_id"]]["match_type"] == "ranked"


def test_requeue_after_abandonment_can_rematch_same_opponent(client, auth_headers):
    old_id = _make_ranked_match(client, auth_headers)
    _backdate_match(old_id, 10)

    # Both players search again: A's poll abandons the old match, B's poll
    # then pairs with A into a fresh match.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    body = _start(client, auth_headers, PLAYER_B)
    assert body["status"] == "matched"
    assert body["match_id"] != old_id
    assert main.in_memory_matches[old_id]["status"] == "abandoned"
    assert main.in_memory_matches[body["match_id"]]["status"] == "active"
