"""
Multiplayer stress / sequential-chaos edge cases for the people-match flows.

This suite simulates MANY people matching at once (or in rapid sequence) and
long chaotic session timelines, rather than single-interaction edge cases:

 1. 10 guests entering the ranked queue nearly simultaneously
 2. one user firing many parallel /api/game/start calls
 3. rapid cancel/start churn across 5 users
 4. friend-match create spam (20 waiting matches) + random joins
 5. a full first-to-3 friend match played end-to-end with alternating winners
 6. two ranked matches running in parallel without sharing rounds
 7. immediate requeue + rematch after a completed match
 8. challenge spam (12 pending challenges to one opponent, list capped at 10)
 9. give-up storms from both sides
10. status-polling storms while a round is being answered
11. friend + ranked matches overlapping for the same pair of players
12. a queue entry going stale for an hour before five new players arrive

Conventions (same as the other edge-case suites):
- Guest identities via "Bearer guest-xxx" tokens.
- "Simultaneous" arrivals are driven either by asyncio.gather over the route
  coroutines (single-worker semantics: guest-id paths have no await between
  queue check and pop, so the event loop serializes them - which is exactly
  what production sees on one uvicorn worker) or by rapid sequential HTTP
  calls through the TestClient.
- Time-based branches are exercised by backdating the naive utcnow
  timestamps the production code compares against.
- Known bugs surfaced by these scenarios are documented with strict xfail
  tests, each with a sibling test pinning the CURRENT behavior so regressions
  in either direction are visible.  See MATCH_EDGE_CASE_REPORT.md.
"""

import asyncio
import copy
import random as random_module
from datetime import datetime, timedelta

import pytest

import main


CORRECT_ANSWER = "2*x"  # fixed_question's stored answer is "2·x"
WRONG_ANSWER = "7"

CHALLENGER = "guest-stress-challenger"
INVITEE = "guest-stress-invitee"
INVITEE_USERNAME = "StressInvitee"

USER_REGISTRY = {
    INVITEE_USERNAME: {
        "_id": INVITEE,
        "username": INVITEE_USERNAME,
        "name": "Stress Invitee",
        "elo": 1000,
        "wins": 0,
        "losses": 0,
    },
}


def _guests(prefix, count):
    return [f"guest-stress-{prefix}-{i:02d}" for i in range(count)]


# ---------------------------------------------------------------------------
# helpers: HTTP
# ---------------------------------------------------------------------------


def _start(client, auth_headers, player, continue_existing=False):
    response = client.post(
        "/api/game/start",
        json={"mode": "random", "continue_existing": continue_existing},
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


def _question(client, auth_headers, match_id, player, expect=200):
    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert response.status_code == expect, response.text
    return response.json()


def _answer(client, auth_headers, match_id, player, answer, expect=200):
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )
    assert response.status_code == expect, response.text
    return response.json()


def _give_up(client, auth_headers, match_id, player):
    response = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _status(client, auth_headers, match_id, player):
    response = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_friend(client, auth_headers, creator, opponent_username=None):
    payload = {}
    if opponent_username is not None:
        payload["opponent_username"] = opponent_username
    response = client.post(
        "/api/game/friend/create", json=payload, headers=auth_headers(creator)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _join_friend(client, auth_headers, code, joiner, expect=200):
    response = client.post(
        "/api/game/friend/join",
        json={"match_code": code},
        headers=auth_headers(joiner),
    )
    assert response.status_code == expect, response.text
    return response.json()


def _friend_match(client, auth_headers, creator, joiner):
    """Create + join a friend match; returns match_id (creator is player1)."""
    created = _create_friend(client, auth_headers, creator)
    joined = _join_friend(client, auth_headers, created["match_code"], joiner)
    assert joined["status"] == "active"
    return created["match_id"]


def _ranked_pair(client, auth_headers, queued_player, joining_player):
    """Queue one player, let the other pair with them.

    Returns match_id. NOTE: the JOINING poller becomes player1 in the doc.
    """
    first = _start(client, auth_headers, queued_player)
    assert first["status"] == "searching"
    second = _start(client, auth_headers, joining_player)
    assert second["status"] == "matched", second
    return second["match_id"]


def _win_round(client, auth_headers, match_id, winner):
    """Fetch the current question as `winner` and answer it correctly."""
    _question(client, auth_headers, match_id, winner)
    body = _answer(client, auth_headers, match_id, winner, CORRECT_ANSWER)
    assert body["correct"] is True, body
    return body


def _players_of(match):
    return {str(match["player1_id"]), str(match["player2_id"])}


def _backdate_match(match_id, seconds):
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow() - timedelta(
        seconds=seconds
    )


# ---------------------------------------------------------------------------
# helpers: direct coroutine calls (concurrency)
# ---------------------------------------------------------------------------


def _gather_starts(users, elo=1000):
    """Fire start_match for every user in one event loop via asyncio.gather."""

    async def run():
        return await asyncio.gather(
            *[
                main.start_match(
                    main.MatchStart(mode="random"),
                    current_user={"_id": user_id, "elo": elo},
                )
                for user_id in users
            ]
        )

    return asyncio.run(run())


# ===========================================================================
# scenario 1: 10 guests enter the queue nearly simultaneously
# ===========================================================================


def test_ten_simultaneous_guests_form_five_matches(mock_mongo):
    users = _guests("wave", 10)
    results = _gather_starts(users)

    # Arrivals alternate: odd arrivals queue ("searching"), even arrivals
    # pair with the queued one ("matched").
    statuses = [r["status"] for r in results]
    assert statuses.count("matched") == 5
    assert statuses.count("searching") == 5
    assert "cancelled" not in statuses

    assert len(main.in_memory_matches) == 5
    # Nobody is left waiting in the queue.
    assert main.matchmaking_queue == {}


def test_ten_simultaneous_guests_no_user_in_two_matches(mock_mongo):
    users = _guests("wave", 10)
    _gather_starts(users)

    seen = []
    for match in main.in_memory_matches.values():
        players = _players_of(match)
        # Every match is a proper 1v1 between two distinct humans (no bot,
        # no self-match, no "triple match" where a player is shared).
        assert len(players) == 2
        assert "bot-opponent" not in players
        seen.extend(players)

    assert sorted(seen) == sorted(users)  # each user in exactly one match


def test_ten_simultaneous_guests_searching_users_reconnect_into_their_match(
    mock_mongo,
):
    users = _guests("wave", 10)
    results = _gather_starts(users)

    # The users that were answered "searching" were actually paired by a
    # later arrival; their next poll routes them into that match through the
    # <5s reconnect window instead of leaving them searching.
    searching_users = [
        user for user, r in zip(users, results) if r["status"] == "searching"
    ]
    assert len(searching_users) == 5

    followups = _gather_starts(searching_users)
    assert all(r["status"] == "matched" for r in followups)
    # And no duplicate matches were created by the reconnect polls.
    assert len(main.in_memory_matches) == 5


def test_nine_simultaneous_guests_leave_exactly_one_searching(mock_mongo):
    users = _guests("odd", 9)
    results = _gather_starts(users)

    statuses = [r["status"] for r in results]
    assert statuses.count("matched") == 4
    assert statuses.count("searching") == 5  # 4 later-paired + 1 leftover

    assert len(main.in_memory_matches) == 4
    # Exactly one user is genuinely still in the queue: the last odd arrival.
    assert list(main.matchmaking_queue.keys()) == [users[8]]
    matched_players = set()
    for match in main.in_memory_matches.values():
        matched_players |= _players_of(match)
    assert users[8] not in matched_players


def test_ten_rapid_sequential_guests_over_http_form_five_matches(
    client, auth_headers
):
    users = _guests("http", 10)
    statuses = [_start(client, auth_headers, user)["status"] for user in users]

    assert statuses == ["searching", "matched"] * 5
    assert len(main.in_memory_matches) == 5
    assert main.matchmaking_queue == {}

    all_players = set()
    for match in main.in_memory_matches.values():
        assert match["match_type"] == "ranked"
        all_players |= _players_of(match)
    assert all_players == set(users)


# ===========================================================================
# scenario 2: same user fires many parallel start_match calls
# ===========================================================================


def test_same_user_eight_parallel_starts_only_queue_once(mock_mongo):
    user = "guest-stress-spammer"
    results = _gather_starts([user] * 8)

    # Every call reports searching; the user never matches themselves.
    assert all(r["status"] == "searching" for r in results)
    assert list(main.matchmaking_queue.keys()) == [user]
    assert main.in_memory_matches == {}


def test_same_user_parallel_starts_do_not_reset_queue_timer(mock_mongo):
    user = "guest-stress-spammer"
    _gather_starts([user] * 3)
    joined_at = main.matchmaking_queue[user]["joined_at"]

    _gather_starts([user] * 3)
    # Re-polls hit the "already in queue" branch and never touch joined_at.
    assert main.matchmaking_queue[user]["joined_at"] == joined_at


def test_same_user_parallel_starts_after_matching_all_reconnect(mock_mongo):
    user_a = "guest-stress-par-a"
    user_b = "guest-stress-par-b"

    async def run():
        await main.start_match(
            main.MatchStart(mode="random"), current_user={"_id": user_a, "elo": 1000}
        )
        matched = await main.start_match(
            main.MatchStart(mode="random"), current_user={"_id": user_b, "elo": 1000}
        )
        spam = await asyncio.gather(
            *[
                main.start_match(
                    main.MatchStart(mode="random"),
                    current_user={"_id": user_a, "elo": 1000},
                )
                for _ in range(5)
            ]
        )
        return matched, spam

    matched, spam = asyncio.run(run())

    # All 5 parallel re-polls funnel into the one existing match.
    assert all(r["status"] == "matched" for r in spam)
    assert {r["match_id"] for r in spam} == {matched["match_id"]}
    assert len(main.in_memory_matches) == 1
    assert user_a not in main.matchmaking_queue


def test_same_user_rapid_http_poll_spam_stays_searching(client, auth_headers):
    user = "guest-stress-httpspam"
    for i in range(6):
        body = _start(client, auth_headers, user)
        assert body["status"] == "searching"
        # int() truncation of (10 - elapsed); rapid polls stay at 9-10.
        assert body["time_remaining"] in (9, 10)

    assert list(main.matchmaking_queue.keys()) == [user]
    assert main.in_memory_matches == {}


# ===========================================================================
# scenario 3: rapid cancel/start churn for 5 users
# ===========================================================================


CHURN_USERS = _guests("churn", 5)


def _churn(client, auth_headers, cycles=3):
    """Each user rapidly enters and leaves the queue `cycles` times."""
    for _ in range(cycles):
        for user in CHURN_USERS:
            assert _start(client, auth_headers, user)["status"] == "searching"
            assert _cancel(client, auth_headers, user) == {"status": "cancelled"}


def test_churn_leaves_stale_flags_but_empty_queue(client, auth_headers):
    _churn(client, auth_headers)

    assert main.matchmaking_queue == {}
    assert main.in_memory_matches == {}
    # BUG(stale-cancel-flag): every churned user leaves a permanent entry in
    # cancelled_users; nothing consumes it until a future pairing attempt.
    assert set(CHURN_USERS) <= main.cancelled_users


def test_current_behavior_first_mass_start_after_churn_pairs_nobody(
    client, auth_headers
):
    # BUG: pins the CURRENT behavior. The stale flags from the churn eat the
    # pairing attempts pairwise: users 2 and 4 get a spurious "cancelled"
    # response even though their *last* action was start, not cancel.
    _churn(client, auth_headers)

    statuses = [_start(client, auth_headers, user)["status"] for user in CHURN_USERS]
    assert statuses == ["searching", "cancelled", "searching", "cancelled", "searching"]
    assert main.in_memory_matches == {}
    # Users 1 and 3 were silently dropped from the queue by the aborted
    # pairings; only user 5 is still genuinely queued.
    assert list(main.matchmaking_queue.keys()) == [CHURN_USERS[4]]
    # The aborted pairings consumed four flags; user 5's flag still lingers.
    assert main.cancelled_users & set(CHURN_USERS) == {CHURN_USERS[4]}


def test_current_behavior_second_mass_start_finally_forms_two_matches(
    client, auth_headers
):
    # BUG: continues the pin above. On the second mass start user 1's pairing
    # with user 5 is eaten by user 5's leftover flag, and only then do the
    # remaining four users pair cleanly: (u3, u2) and (u5, u4).
    _churn(client, auth_headers)
    for user in CHURN_USERS:
        _start(client, auth_headers, user)

    statuses = [_start(client, auth_headers, user)["status"] for user in CHURN_USERS]
    assert statuses == ["cancelled", "searching", "matched", "searching", "matched"]

    assert len(main.in_memory_matches) == 2
    pairs = [_players_of(m) for m in main.in_memory_matches.values()]
    assert {CHURN_USERS[2], CHURN_USERS[1]} in pairs
    assert {CHURN_USERS[4], CHURN_USERS[3]} in pairs
    # All stale flags are finally gone...
    assert main.cancelled_users & set(CHURN_USERS) == set()
    # ...but user 1 is matchless and not even queued.
    assert CHURN_USERS[0] not in main.matchmaking_queue


@pytest.mark.xfail(
    reason=(
        "BUG(stale-cancel-flag): after rapid cancel/start churn the leftover "
        "cancelled_users entries poison the next pairing attempts. A single "
        "mass re-queue of 5 users should produce two matches and one searcher, "
        "but currently produces zero matches and two spurious 'cancelled' "
        "responses."
    ),
    strict=True,
)
def test_churned_users_should_all_be_matchable_on_first_mass_start(
    client, auth_headers
):
    _churn(client, auth_headers)

    statuses = [_start(client, auth_headers, user)["status"] for user in CHURN_USERS]
    assert "cancelled" not in statuses
    assert statuses.count("matched") == 2
    assert len(main.in_memory_matches) == 2


def test_churn_storm_never_creates_ghost_or_duplicate_matches(client, auth_headers):
    # However chaotic the churn, the surviving invariant is that no user ever
    # ends up in TWO active matches and no match contains a cancelled ghost.
    _churn(client, auth_headers)
    for _ in range(3):  # three mass start passes
        for user in CHURN_USERS:
            _start(client, auth_headers, user)

    membership = {}
    for match in main.in_memory_matches.values():
        assert match["status"] == "active"
        players = _players_of(match)
        assert len(players) == 2
        for player in players:
            membership[player] = membership.get(player, 0) + 1

    assert all(count == 1 for count in membership.values()), membership


# ===========================================================================
# scenario 4: friend-match create spam + random joins
# ===========================================================================


def test_twenty_waiting_friend_matches_have_unique_codes_and_ids(
    client, auth_headers
):
    creators = _guests("creator", 20)
    created = [_create_friend(client, auth_headers, c) for c in creators]

    codes = [c["match_code"] for c in created]
    ids = [c["match_id"] for c in created]
    assert len(set(codes)) == 20
    assert len(set(ids)) == 20
    assert all(c["status"] == "waiting" for c in created)
    assert len(main.in_memory_matches) == 20


def test_single_creator_can_stack_twenty_waiting_matches(client, auth_headers):
    # BUG/quirk: there is no per-user cap on open friend matches, so one
    # client can spam unbounded waiting matches into memory and the DB.
    creator = "guest-stress-hoarder"
    created = [_create_friend(client, auth_headers, creator) for _ in range(20)]

    assert len(main.in_memory_matches) == 20
    assert all(
        str(m["player1_id"]) == creator for m in main.in_memory_matches.values()
    )
    assert len({c["match_code"] for c in created}) == 20


def test_random_joins_activate_only_the_targeted_matches(client, auth_headers):
    creators = _guests("creator", 20)
    created = [_create_friend(client, auth_headers, c) for c in creators]

    rng = random_module.Random(20260720)
    picked = rng.sample(created, 8)
    joiners = _guests("joiner", 8)

    for joiner, target in zip(joiners, picked):
        body = _join_friend(client, auth_headers, target["match_code"], joiner)
        assert body == {"match_id": target["match_id"], "status": "active"}

    active_ids = {
        mid
        for mid, m in main.in_memory_matches.items()
        if m["status"] == "active"
    }
    assert active_ids == {t["match_id"] for t in picked}
    # Each activated match got exactly its own joiner as player2.
    for joiner, target in zip(joiners, picked):
        assert (
            str(main.in_memory_matches[target["match_id"]]["player2_id"]) == joiner
        )


def test_second_joiner_on_taken_code_is_rejected(client, auth_headers):
    created = _create_friend(client, auth_headers, "guest-stress-host")
    _join_friend(client, auth_headers, created["match_code"], "guest-stress-fast")

    body = _join_friend(
        client, auth_headers, created["match_code"], "guest-stress-slow", expect=400
    )
    assert body["detail"] == "Match already started"
    # The first joiner keeps the seat.
    assert (
        str(main.in_memory_matches[created["match_id"]]["player2_id"])
        == "guest-stress-fast"
    )


def test_unjoined_matches_stay_waiting_and_joinable_after_join_spam(
    client, auth_headers
):
    creators = _guests("creator", 20)
    created = [_create_friend(client, auth_headers, c) for c in creators]

    joiners = _guests("joiner", 10)
    for joiner, target in zip(joiners, created[:10]):
        _join_friend(client, auth_headers, target["match_code"], joiner)

    # The 10 untouched matches are still waiting and still joinable.
    for target in created[10:]:
        assert main.in_memory_matches[target["match_id"]]["status"] == "waiting"
    late = _join_friend(
        client, auth_headers, created[15]["match_code"], "guest-stress-late"
    )
    assert late["status"] == "active"


# ===========================================================================
# scenario 5: full first-to-3 friend match with alternating winners
# ===========================================================================


def test_full_friend_match_alternating_winners_ends_three_two(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-p1"
    joiner = "guest-stress-p2"
    match_id = _friend_match(client, auth_headers, creator, joiner)

    # Winners alternate A, B, A, B, A -> 3-2 for the creator (player1).
    script = [
        (creator, 1, 0, None),
        (joiner, 1, 1, None),
        (creator, 2, 1, None),
        (joiner, 2, 2, None),
        (creator, 3, 2, creator),
    ]
    for winner, p1_score, p2_score, match_winner in script:
        # Both players fetch the SAME round before anyone answers.
        q_creator = _question(client, auth_headers, match_id, creator)
        q_joiner = _question(client, auth_headers, match_id, joiner)
        assert q_creator["round_id"] == q_joiner["round_id"]

        body = _answer(client, auth_headers, match_id, winner, CORRECT_ANSWER)
        assert body["correct"] is True
        assert body["round_winner"] == winner
        assert (body["player1_score"], body["player2_score"]) == (p1_score, p2_score)
        assert body["match_winner"] == match_winner

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert str(match["winner_id"]) == creator
    assert (match["player1_score"], match["player2_score"]) == (3, 2)


def test_full_friend_match_round_ids_are_sequential_and_unique(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-p1"
    joiner = "guest-stress-p2"
    match_id = _friend_match(client, auth_headers, creator, joiner)

    round_ids = []
    for i, winner in enumerate([creator, joiner, creator, joiner, creator]):
        q = _question(client, auth_headers, match_id, winner)
        round_ids.append(q["round_id"])
        _answer(client, auth_headers, match_id, winner, CORRECT_ANSWER)

    assert round_ids == [f"round-{match_id}-{n}" for n in range(1, 6)]
    assert len(set(round_ids)) == 5


def test_wrong_answer_flurries_between_wins_do_not_advance_rounds(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-p1"
    joiner = "guest-stress-p2"
    match_id = _friend_match(client, auth_headers, creator, joiner)

    for winner, loser in [(creator, joiner), (joiner, creator)]:
        q_before = _question(client, auth_headers, match_id, winner)
        # The eventual loser hammers wrong answers first.
        for _ in range(3):
            body = _answer(client, auth_headers, match_id, loser, WRONG_ANSWER)
            assert body["correct"] is False
            assert body["round_winner"] is None
        # Round did not advance under the flurry.
        q_after = _question(client, auth_headers, match_id, loser)
        assert q_after["round_id"] == q_before["round_id"]

        win = _answer(client, auth_headers, match_id, winner, CORRECT_ANSWER)
        assert win["correct"] is True and win["round_winner"] == winner

    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (1, 1)


def test_completed_friend_match_rejects_further_play_and_pays_no_elo(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-p1"
    joiner = "guest-stress-p2"
    match_id = _friend_match(client, auth_headers, creator, joiner)

    final = None
    for winner in [creator, joiner, creator, joiner, creator]:
        final = _win_round(client, auth_headers, match_id, winner)

    # Friend matches are unranked: completion pays zero ELO.
    assert final["match_winner"] == creator
    assert final["elo_change"] == 0

    for player in (creator, joiner):
        status = _status(client, auth_headers, match_id, player)
        assert status["status"] == "completed"
        assert status["winner_id"] == creator
        assert status["elo_change"] == 0
        # No further questions or answers for either player.
        q = _question(client, auth_headers, match_id, player, expect=400)
        assert q["detail"] == "Match is already completed"
        a = _answer(
            client, auth_headers, match_id, player, CORRECT_ANSWER, expect=400
        )
        assert a["detail"] == "Match is already completed"


# ===========================================================================
# scenario 6: two ranked matches in parallel don't share rounds
# ===========================================================================


def test_parallel_ranked_matches_get_distinct_round_ids(
    client, auth_headers, fixed_question
):
    m1 = _ranked_pair(client, auth_headers, "guest-stress-a", "guest-stress-b")
    m2 = _ranked_pair(client, auth_headers, "guest-stress-c", "guest-stress-d")
    assert m1 != m2

    q1 = _question(client, auth_headers, m1, "guest-stress-a")
    q2 = _question(client, auth_headers, m2, "guest-stress-c")

    assert q1["round_id"] == f"round-{m1}-1"
    assert q2["round_id"] == f"round-{m2}-1"
    assert q1["round_id"] != q2["round_id"]
    assert main.in_memory_rounds[q1["round_id"]]["match_id"] == m1
    assert main.in_memory_rounds[q2["round_id"]]["match_id"] == m2


def test_win_in_one_parallel_match_moves_no_scores_in_the_other(
    client, auth_headers, fixed_question
):
    m1 = _ranked_pair(client, auth_headers, "guest-stress-a", "guest-stress-b")
    m2 = _ranked_pair(client, auth_headers, "guest-stress-c", "guest-stress-d")
    q1 = _question(client, auth_headers, m1, "guest-stress-a")
    q2 = _question(client, auth_headers, m2, "guest-stress-c")

    # guest-stress-a is player2 of m1 (the queued player).
    body = _answer(client, auth_headers, m1, "guest-stress-a", CORRECT_ANSWER)
    assert body["correct"] is True

    match2 = main.in_memory_matches[m2]
    assert (match2["player1_score"], match2["player2_score"]) == (0, 0)
    assert main.in_memory_rounds[q2["round_id"]].get("winner_id") is None
    # m1's round is resolved, m2's is untouched.
    assert main.in_memory_rounds[q1["round_id"]]["winner_id"] is not None


def test_parallel_matches_progress_rounds_independently(
    client, auth_headers, fixed_question
):
    m1 = _ranked_pair(client, auth_headers, "guest-stress-a", "guest-stress-b")
    m2 = _ranked_pair(client, auth_headers, "guest-stress-c", "guest-stress-d")

    # m1 races through two rounds; m2 stays parked on round 1.
    q2 = _question(client, auth_headers, m2, "guest-stress-c")
    _win_round(client, auth_headers, m1, "guest-stress-a")
    _win_round(client, auth_headers, m1, "guest-stress-b")
    q1_third = _question(client, auth_headers, m1, "guest-stress-a")

    assert q1_third["round_id"] == f"round-{m1}-3"
    q2_again = _question(client, auth_headers, m2, "guest-stress-d")
    assert q2_again["round_id"] == q2["round_id"] == f"round-{m2}-1"


def test_parallel_ranked_matches_complete_independently(
    client, auth_headers, fixed_question
):
    m1 = _ranked_pair(client, auth_headers, "guest-stress-a", "guest-stress-b")
    m2 = _ranked_pair(client, auth_headers, "guest-stress-c", "guest-stress-d")

    final1 = None
    final2 = None
    # Interleave the two matches round by round.
    for _ in range(3):
        final1 = _win_round(client, auth_headers, m1, "guest-stress-a")
        final2 = _win_round(client, auth_headers, m2, "guest-stress-d")

    assert final1["match_winner"] == "guest-stress-a"
    assert final2["match_winner"] == "guest-stress-d"
    # Even 1000-vs-1000 guests "pay" the standard 20 (phantom for guests).
    assert final1["elo_change"] == 20
    assert final2["elo_change"] == 20
    assert main.in_memory_matches[m1]["status"] == "completed"
    assert main.in_memory_matches[m2]["status"] == "completed"
    assert str(main.in_memory_matches[m1]["winner_id"]) == "guest-stress-a"
    assert str(main.in_memory_matches[m2]["winner_id"]) == "guest-stress-d"


# ===========================================================================
# scenario 7: immediate requeue + rematch after completion
# ===========================================================================


def _complete_ranked_match(client, auth_headers, queued, joiner):
    match_id = _ranked_pair(client, auth_headers, queued, joiner)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, joiner)
    assert main.in_memory_matches[match_id]["status"] == "completed"
    return match_id


def test_completed_match_is_not_reconnected_on_immediate_requeue(
    client, auth_headers, fixed_question
):
    player_a = "guest-stress-re-a"
    player_b = "guest-stress-re-b"
    _complete_ranked_match(client, auth_headers, player_a, player_b)

    # Seconds after completion the reconnect window must NOT resurrect the
    # completed match; the player re-enters the queue instead.
    body = _start(client, auth_headers, player_a)
    assert body["status"] == "searching"
    assert player_a in main.matchmaking_queue


def test_both_players_requeue_and_rematch_in_a_fresh_match(
    client, auth_headers, fixed_question
):
    player_a = "guest-stress-re-a"
    player_b = "guest-stress-re-b"
    first_id = _complete_ranked_match(client, auth_headers, player_a, player_b)
    first_code = main.in_memory_matches[first_id]["match_code"]

    assert _start(client, auth_headers, player_a)["status"] == "searching"
    rematch = _start(client, auth_headers, player_b)
    assert rematch["status"] == "matched"
    assert rematch["match_id"] != first_id
    assert rematch["match_code"] != first_code

    new_match = main.in_memory_matches[rematch["match_id"]]
    assert _players_of(new_match) == {player_a, player_b}
    assert new_match["status"] == "active"
    assert (new_match["player1_score"], new_match["player2_score"]) == (0, 0)


def test_rematch_is_playable_and_old_match_stays_frozen(
    client, auth_headers, fixed_question
):
    player_a = "guest-stress-re-a"
    player_b = "guest-stress-re-b"
    first_id = _complete_ranked_match(client, auth_headers, player_a, player_b)
    old_snapshot = copy.deepcopy(main.in_memory_matches[first_id])

    _start(client, auth_headers, player_a)
    rematch_id = _start(client, auth_headers, player_b)["match_id"]

    body = _win_round(client, auth_headers, rematch_id, player_a)
    assert body["round_winner"] == player_a

    old = main.in_memory_matches[first_id]
    assert old["status"] == "completed"
    assert old["player1_score"] == old_snapshot["player1_score"]
    assert old["player2_score"] == old_snapshot["player2_score"]
    assert str(old["winner_id"]) == str(old_snapshot["winner_id"])

    # /api/game/active points both players at the rematch only.
    for player in (player_a, player_b):
        active = _active(client, auth_headers, player)
        assert active["has_active_match"] is True
        assert active["match_id"] == rematch_id


# ===========================================================================
# scenario 8: challenge spam - 12 pending to the same opponent, list cap 10
# ===========================================================================


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self._docs)
        return list(self._docs)[:length]


class FakeMatchesDB:
    """Same minimal Mongo stand-in used by the challenge edge-case suite."""

    def __init__(self):
        self.docs = {}

    @staticmethod
    def _matches(doc, query):
        return all(doc.get(key) == value for key, value in (query or {}).items())

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = copy.deepcopy(doc)

    async def find_one(self, query, *args, **kwargs):
        for doc in self.docs.values():
            if self._matches(doc, query):
                return copy.deepcopy(doc)
        return None

    def find(self, query=None, *args, **kwargs):
        hits = [copy.deepcopy(d) for d in self.docs.values() if self._matches(d, query)]
        return _FakeCursor(hits)

    async def update_one(self, query, update, *args, **kwargs):
        for doc in self.docs.values():
            if self._matches(doc, query):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                break
        return type("R", (), {"modified_count": 1, "matched_count": 1, "upserted_id": None})()

    async def delete_one(self, query, *args, **kwargs):
        for match_id, doc in list(self.docs.items()):
            if self._matches(doc, query):
                del self.docs[match_id]
                break
        return None


@pytest.fixture
def challenge_db(mock_mongo, monkeypatch):
    db = FakeMatchesDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.matches_collection, method, getattr(db, method))

    async def users_find_one(query, *args, **kwargs):
        username = query.get("username")
        if username is not None:
            return copy.deepcopy(USER_REGISTRY.get(username))
        return None

    monkeypatch.setattr(main.users_collection, "find_one", users_find_one)
    return db


def _spam_challenges(client, auth_headers, count):
    created = []
    for _ in range(count):
        body = _create_friend(
            client, auth_headers, CHALLENGER, opponent_username=INVITEE_USERNAME
        )
        assert body["status"] == "pending"
        created.append(body)
    return created


def _pending(client, auth_headers):
    response = client.get("/api/challenges/pending", headers=auth_headers(INVITEE))
    assert response.status_code == 200, response.text
    return response.json()


def test_twelve_identical_challenges_all_stored_no_dedupe(
    client, auth_headers, challenge_db
):
    created = _spam_challenges(client, auth_headers, 12)

    # BUG/quirk: no dedupe and no cap - one challenger can stack unlimited
    # identical pending challenges against the same opponent.
    assert len(challenge_db.docs) == 12
    assert len({c["match_id"] for c in created}) == 12
    assert all(
        d["status"] == "pending"
        and d["player1_id"] == CHALLENGER
        and d["player2_id"] == INVITEE
        for d in challenge_db.docs.values()
    )


def test_pending_list_is_hard_capped_at_ten(client, auth_headers, challenge_db):
    created = _spam_challenges(client, auth_headers, 12)

    pending = _pending(client, auth_headers)
    # BUG/quirk: to_list(length=10) silently truncates - the invitee sees
    # exactly the 10 oldest challenges and has no idea two more exist.
    assert len(pending) == 10
    listed_ids = {p["match_id"] for p in pending}
    assert listed_ids == {c["match_id"] for c in created[:10]}
    assert created[10]["match_id"] not in listed_ids
    assert created[11]["match_id"] not in listed_ids


@pytest.mark.xfail(
    reason=(
        "BUG(pending-cap): get_pending_challenges truncates at to_list("
        "length=10) with no paging, count or dedupe, so challenges 11+ are "
        "silently invisible to the invitee while remaining fully live in "
        "the DB (and playable, per the challenge suite)."
    ),
    strict=True,
)
def test_pending_list_should_expose_all_twelve_spam_challenges(
    client, auth_headers, challenge_db
):
    _spam_challenges(client, auth_headers, 12)
    pending = _pending(client, auth_headers)
    assert len(pending) == 12


def test_accepting_one_spam_challenge_leaves_the_rest_pending(
    client, auth_headers, challenge_db
):
    created = _spam_challenges(client, auth_headers, 12)

    accepted = client.post(
        f"/api/challenges/accept/{created[0]['match_id']}",
        headers=auth_headers(INVITEE),
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "active"

    pending = _pending(client, auth_headers)
    # 11 still pending; the list cap keeps showing only 10 of them.
    assert len(pending) == 10
    assert created[0]["match_id"] not in {p["match_id"] for p in pending}
    still_pending = [
        d for d in challenge_db.docs.values() if d["status"] == "pending"
    ]
    assert len(still_pending) == 11


def test_cancelling_spam_challenges_uncovers_the_hidden_ones(
    client, auth_headers, challenge_db
):
    created = _spam_challenges(client, auth_headers, 12)

    # The challenger cleans up the two oldest challenges...
    for target in created[:2]:
        response = client.post(
            f"/api/challenges/cancel/{target['match_id']}",
            headers=auth_headers(CHALLENGER),
        )
        assert response.status_code == 200

    # ...and the two previously hidden ones scroll into view.
    pending = _pending(client, auth_headers)
    assert len(pending) == 10
    assert {p["match_id"] for p in pending} == {
        c["match_id"] for c in created[2:]
    }

    # Cancelling two more drops the list below the cap.
    for target in created[2:4]:
        client.post(
            f"/api/challenges/cancel/{target['match_id']}",
            headers=auth_headers(CHALLENGER),
        )
    assert len(_pending(client, auth_headers)) == 8


# ===========================================================================
# scenario 9: give-up storms from both sides
# ===========================================================================


def test_solo_give_up_spam_keeps_round_open(client, auth_headers, fixed_question):
    creator = "guest-stress-quitter"
    joiner = "guest-stress-stayer"
    match_id = _friend_match(client, auth_headers, creator, joiner)
    q = _question(client, auth_headers, match_id, creator)
    # Opponent heartbeats so their presence is fresh (no auto-tie shortcut).
    _status(client, auth_headers, match_id, joiner)

    for _ in range(4):
        body = _give_up(client, auth_headers, match_id, creator)
        assert body == {"status": "gave_up", "waiting_for_opponent": True}

    # The round is still the current, winnerless round.
    again = _question(client, auth_headers, match_id, joiner)
    assert again["round_id"] == q["round_id"]
    assert main.in_memory_rounds[q["round_id"]].get("winner_id") is None


def test_give_up_storm_from_both_sides_ties_without_scoring(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-quitter"
    joiner = "guest-stress-stayer"
    match_id = _friend_match(client, auth_headers, creator, joiner)
    _question(client, auth_headers, match_id, creator)
    _status(client, auth_headers, match_id, joiner)

    for _ in range(3):
        _give_up(client, auth_headers, match_id, creator)
    body = _give_up(client, auth_headers, match_id, joiner)

    assert body == {
        "status": "both_gave_up",
        "round_winner": "tie",
        "player1_score": 0,
        "player2_score": 0,
    }
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (0, 0)
    assert match["status"] == "active"


def test_give_up_spam_after_tie_returns_already_ended(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-quitter"
    joiner = "guest-stress-stayer"
    match_id = _friend_match(client, auth_headers, creator, joiner)
    _question(client, auth_headers, match_id, creator)
    _status(client, auth_headers, match_id, joiner)
    _give_up(client, auth_headers, match_id, creator)
    _give_up(client, auth_headers, match_id, joiner)

    for player in (creator, joiner, creator, joiner):
        body = _give_up(client, auth_headers, match_id, player)
        assert body == {"status": "already_ended", "round_winner": "tie"}


def test_three_rounds_of_give_up_storms_never_complete_the_match(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-quitter"
    joiner = "guest-stress-stayer"
    match_id = _friend_match(client, auth_headers, creator, joiner)

    round_ids = []
    for round_number in range(1, 4):
        q = _question(client, auth_headers, match_id, creator)
        _question(client, auth_headers, match_id, joiner)
        round_ids.append(q["round_id"])
        # Alternate who breaks first each round.
        first, second = (
            (creator, joiner) if round_number % 2 else (joiner, creator)
        )
        for _ in range(2):
            _give_up(client, auth_headers, match_id, first)
        body = _give_up(client, auth_headers, match_id, second)
        assert body["status"] == "both_gave_up"

    assert round_ids == [f"round-{match_id}-{n}" for n in range(1, 4)]
    match = main.in_memory_matches[match_id]
    assert match["status"] == "active"  # ties never finish a first-to-3
    assert (match["player1_score"], match["player2_score"]) == (0, 0)


def test_give_up_storm_then_correct_answer_still_wins_the_round(
    client, auth_headers, fixed_question
):
    # BUG/quirk (pinned in the answer suite too): giving up does not lock the
    # quitter out - after a give-up storm they can still snipe the round.
    creator = "guest-stress-quitter"
    joiner = "guest-stress-stayer"
    match_id = _friend_match(client, auth_headers, creator, joiner)
    _question(client, auth_headers, match_id, creator)
    _status(client, auth_headers, match_id, joiner)

    for _ in range(3):
        _give_up(client, auth_headers, match_id, creator)

    body = _answer(client, auth_headers, match_id, creator, CORRECT_ANSWER)
    assert body["correct"] is True
    assert body["round_winner"] == creator
    assert body["player1_score"] == 1


# ===========================================================================
# scenario 10: status-polling storm while answering
# ===========================================================================


def test_status_poll_storm_is_stable_while_round_is_open(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-poller1"
    joiner = "guest-stress-poller2"
    match_id = _friend_match(client, auth_headers, creator, joiner)
    q = _question(client, auth_headers, match_id, creator)
    _question(client, auth_headers, match_id, joiner)

    reference_start_time = None
    for i in range(15):
        player = creator if i % 2 == 0 else joiner
        status = _status(client, auth_headers, match_id, player)
        assert status["status"] == "active"
        assert (status["player1_score"], status["player2_score"]) == (0, 0)
        assert status["round_winner"] is None
        assert status["winner_id"] is None
        if reference_start_time is None:
            reference_start_time = status["round_start_time"]
        # The synchronized round anchor never drifts across polls.
        assert status["round_start_time"] == reference_start_time
        # Interleave wrong answers into the storm.
        if i % 3 == 0:
            wrong = _answer(client, auth_headers, match_id, player, WRONG_ANSWER)
            assert wrong["correct"] is False

    # The storm neither advanced nor forked the round.
    assert len(main.in_memory_rounds) == 1
    again = _question(client, auth_headers, match_id, creator)
    assert again["round_id"] == q["round_id"]


def test_poll_storm_keeps_both_players_marked_connected(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-poller1"
    joiner = "guest-stress-poller2"
    match_id = _friend_match(client, auth_headers, creator, joiner)
    _question(client, auth_headers, match_id, creator)

    for _ in range(10):
        assert (
            _status(client, auth_headers, match_id, creator)["opponent_connected"]
            is True
        )
        assert (
            _status(client, auth_headers, match_id, joiner)["opponent_connected"]
            is True
        )


def test_status_after_round_win_is_consistent_for_both_pollers(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-poller1"
    joiner = "guest-stress-poller2"
    match_id = _friend_match(client, auth_headers, creator, joiner)
    _question(client, auth_headers, match_id, creator)
    _question(client, auth_headers, match_id, joiner)

    win = _answer(client, auth_headers, match_id, joiner, CORRECT_ANSWER)
    assert win["correct"] is True

    for _ in range(5):
        for player in (creator, joiner):
            status = _status(client, auth_headers, match_id, player)
            assert status["round_winner"] == joiner
            assert (status["player1_score"], status["player2_score"]) == (0, 1)
            assert status["status"] == "active"


def test_poll_storm_between_rounds_never_mutates_scores(
    client, auth_headers, fixed_question
):
    creator = "guest-stress-poller1"
    joiner = "guest-stress-poller2"
    match_id = _friend_match(client, auth_headers, creator, joiner)

    _win_round(client, auth_headers, match_id, creator)
    for _ in range(20):
        status = _status(client, auth_headers, match_id, creator)
        assert (status["player1_score"], status["player2_score"]) == (1, 0)

    # After the storm, the next question advances to round 2 exactly once.
    q = _question(client, auth_headers, match_id, joiner)
    assert q["round_id"] == f"round-{match_id}-2"
    assert len(main.in_memory_rounds) == 2


# ===========================================================================
# scenario 11: friend + ranked overlap for the same pair
# ===========================================================================


def test_pair_can_hold_ranked_and_friend_match_when_ranked_comes_first(
    client, auth_headers, fixed_question
):
    player_a = "guest-stress-mix-a"
    player_b = "guest-stress-mix-b"
    ranked_id = _ranked_pair(client, auth_headers, player_a, player_b)
    friend_id = _friend_match(client, auth_headers, player_a, player_b)

    assert main.in_memory_matches[ranked_id]["status"] == "active"
    assert main.in_memory_matches[friend_id]["status"] == "active"

    # Scoring is fully isolated between the two overlapping matches.
    _win_round(client, auth_headers, ranked_id, player_a)  # A is player2 there
    _win_round(client, auth_headers, friend_id, player_b)  # B is player2 there

    ranked = main.in_memory_matches[ranked_id]
    friend = main.in_memory_matches[friend_id]
    assert (ranked["player1_score"], ranked["player2_score"]) == (0, 1)
    assert (friend["player1_score"], friend["player2_score"]) == (0, 1)

    # /api/game/active hides the friend match: insertion order wins.
    for player in (player_a, player_b):
        active = _active(client, auth_headers, player)
        assert active["match_id"] == ranked_id
        assert active["match_type"] == "ranked"


def test_overlapping_rounds_stay_in_their_own_match(
    client, auth_headers, fixed_question
):
    player_a = "guest-stress-mix-a"
    player_b = "guest-stress-mix-b"
    ranked_id = _ranked_pair(client, auth_headers, player_a, player_b)
    friend_id = _friend_match(client, auth_headers, player_a, player_b)

    q_ranked = _question(client, auth_headers, ranked_id, player_a)
    q_friend = _question(client, auth_headers, friend_id, player_a)
    assert q_ranked["round_id"] == f"round-{ranked_id}-1"
    assert q_friend["round_id"] == f"round-{friend_id}-1"

    # Answering in the friend match resolves only the friend round.
    _answer(client, auth_headers, friend_id, player_b, CORRECT_ANSWER)
    assert main.in_memory_rounds[q_friend["round_id"]]["winner_id"] is not None
    assert main.in_memory_rounds[q_ranked["round_id"]].get("winner_id") is None


def test_current_behavior_ranked_start_hijacks_fresh_friend_match_for_both(
    client, auth_headers
):
    # BUG: pins the CURRENT behavior of the match_type-blind reconnect scan.
    # Both players of a fresh friend match tap "play ranked" and both are
    # silently "reconnected" INTO the friend match; neither enters the queue
    # and no ranked match is ever created.
    player_a = "guest-stress-mix-a"
    player_b = "guest-stress-mix-b"
    friend_id = _friend_match(client, auth_headers, player_a, player_b)

    for player in (player_a, player_b):
        body = _start(client, auth_headers, player)
        assert body["status"] == "matched"
        assert body["match_id"] == friend_id

    assert main.matchmaking_queue == {}
    assert list(main.in_memory_matches.keys()) == [friend_id]


@pytest.mark.xfail(
    reason=(
        "BUG(match-type-blind reconnect): the stale-match scan in start_match "
        "ignores match_type, so a pair with a fresh active friend match cannot "
        "queue for ranked - both are hijacked back into the friend match "
        "instead of entering the queue."
    ),
    strict=True,
)
def test_overlapping_pair_should_be_able_to_queue_ranked_from_friend_match(
    client, auth_headers
):
    player_a = "guest-stress-mix-a"
    player_b = "guest-stress-mix-b"
    friend_id = _friend_match(client, auth_headers, player_a, player_b)

    body = _start(client, auth_headers, player_a)
    assert body["status"] == "searching"  # currently: hijacked into friend_id
    assert main.in_memory_matches[friend_id]["status"] == "active"


def test_current_behavior_ranked_start_abandons_older_friend_match_then_pairs(
    client, auth_headers
):
    # BUG: pins the >5s side of the same match_type-blind scan: queueing for
    # ranked destroys the pair's ongoing friend match as a side effect.
    player_a = "guest-stress-mix-a"
    player_b = "guest-stress-mix-b"
    friend_id = _friend_match(client, auth_headers, player_a, player_b)
    _backdate_match(friend_id, 6)

    body_a = _start(client, auth_headers, player_a)
    assert body_a["status"] == "searching"
    assert main.in_memory_matches[friend_id]["status"] == "abandoned"

    body_b = _start(client, auth_headers, player_b)
    assert body_b["status"] == "matched"
    assert body_b["match_id"] != friend_id
    ranked = main.in_memory_matches[body_b["match_id"]]
    assert ranked["match_type"] == "ranked"
    assert _players_of(ranked) == {player_a, player_b}
    # The friend opponent was never told; the friend match is just gone.
    assert main.in_memory_matches[friend_id]["status"] == "abandoned"


# ===========================================================================
# scenario 12: one user queued for an hour, then five arrive
# ===========================================================================


GHOST = "guest-stress-ghost"


def _queue_ghost(seconds=3600):
    main.matchmaking_queue[GHOST] = {
        "elo": 1000,
        "joined_at": datetime.utcnow() - timedelta(seconds=seconds),
    }


def test_hour_stale_waiter_is_paired_first_then_arrivals_pair_among_themselves(
    client, auth_headers
):
    _queue_ghost()
    arrivals = _guests("arrival", 5)

    statuses = [_start(client, auth_headers, user)["status"] for user in arrivals]
    # Arrival 1 is paired with the hour-gone ghost; the rest alternate.
    assert statuses == ["matched", "searching", "matched", "searching", "matched"]

    assert len(main.in_memory_matches) == 3
    assert main.matchmaking_queue == {}

    ghost_matches = [
        m for m in main.in_memory_matches.values() if GHOST in _players_of(m)
    ]
    assert len(ghost_matches) == 1
    assert ghost_matches[0]["match_type"] == "ranked"
    assert _players_of(ghost_matches[0]) == {GHOST, arrivals[0]}


@pytest.mark.xfail(
    reason=(
        "BUG(no-queue-expiry): queue entries never expire on their own - the "
        "10s bot deadline is only evaluated when the queued user themselves "
        "polls. An hour-gone user should no longer be matchable, but the "
        "first arrival is paired straight into a ghost match with them."
    ),
    strict=True,
)
def test_hour_stale_queue_entry_should_not_be_matchable(client, auth_headers):
    _queue_ghost()

    body = _start(client, auth_headers, "guest-stress-arrival-00")
    assert body["status"] == "searching"  # currently: matched with the ghost


def test_ghost_match_reports_never_polling_opponent_as_connected(
    client, auth_headers
):
    _queue_ghost()
    live = "guest-stress-arrival-00"
    body = _start(client, auth_headers, live)
    assert body["status"] == "matched"

    # BUG/quirk: the ghost never polled, so they have no player_last_seen
    # entry and the never-seen rule reports them connected forever.
    status = _status(client, auth_headers, body["match_id"], live)
    assert status["opponent_connected"] is True


def test_ghost_opponent_blocks_the_give_up_auto_tie(
    client, auth_headers, fixed_question
):
    _queue_ghost()
    live = "guest-stress-arrival-00"
    match_id = _start(client, auth_headers, live)["match_id"]
    _question(client, auth_headers, match_id, live)

    # BUG/quirk: because the never-seen ghost counts as connected, the live
    # player's give-up waits forever instead of auto-resolving to a tie.
    body = _give_up(client, auth_headers, match_id, live)
    assert body == {"status": "gave_up", "waiting_for_opponent": True}


def test_long_waiter_polling_right_after_ghost_creation_reconnects_into_it(
    client, auth_headers
):
    _queue_ghost()
    live = "guest-stress-arrival-00"
    matched = _start(client, auth_headers, live)
    assert matched["status"] == "matched"

    # If the "ghost" was merely slow and polls within 5s of the pairing, the
    # reconnect window routes them into the ghost match after all.
    body = _start(client, auth_headers, GHOST)
    assert body["status"] == "matched"
    assert body["match_id"] == matched["match_id"]
    assert GHOST not in main.matchmaking_queue
