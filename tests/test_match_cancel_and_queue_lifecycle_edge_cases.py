"""
Edge-case tests for matchmaking cancellation and queue lifecycle
(main.py: cancel_matchmaking ~1263, start_match ~1087, cancel_challenge
~1061, join_friend_match ~924) plus the process-local state behind them
(matchmaking_queue, cancelled_users, in_memory_matches/rounds, match_locks).

Scope (complements tests/test_ranked_matchmaking_edge_cases.py and
tests/test_challenge_match_edge_cases.py):
- cancel when not queued / double cancel / cancel as pure set-add
- the cancelled_users flag leaking across match COMPLETION and poisoning a
  later pairing (strict xfail), and the "cancel before ever queueing"
  manifestation of the same bug (strict xfail + pins)
- unbounded cancelled_users growth: entries are only ever consumed by a
  pairing or bot-creation attempt, never expired
- queue entries never expire without polling: an hour-stale entry is still
  matchable, producing a ghost match whose absent player is reported
  connected forever and blocks give-up auto-ties
- abandon-then-cancel interactions (cancel after re-queueing from a stale
  match, cancel not resurrecting abandoned matches)
- cancel_challenge on a waiting (code-only) friend match: full memory+DB
  deletion, the orphaned round/lock it leaves behind, and the missing
  in-memory fallback (strict xfail)
- racing cancel vs pairing on the queue (spurious "cancelled" to the
  joiner, but no ghost match), and racing cancel_challenge vs join with DB
  latency (both succeed; acknowledged joiner's match is deleted -- strict
  xfail)

Known bugs are documented with strict xfail plus companion tests pinning
current behavior.  See MATCH_EDGE_CASE_REPORT.md for the summary.
"""

import asyncio
import copy
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

import main


PLAYER_A = "guest-cq-aaa"
PLAYER_B = "guest-cq-bbb"
PLAYER_C = "guest-cq-ccc"

CORRECT = "2*x"  # matches fixed_question's answer "2·x"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _start(client, auth_headers, player):
    response = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _cancel(client, auth_headers, player):
    response = client.post("/api/game/cancel", headers=auth_headers(player))
    assert response.status_code == 200, response.text
    return response.json()


def _ranked_match(client, auth_headers, first=PLAYER_A, second=PLAYER_B):
    assert _start(client, auth_headers, first)["status"] == "searching"
    matched = _start(client, auth_headers, second)
    assert matched["status"] == "matched", matched
    return matched["match_id"]


def _backdate_queue(player, seconds):
    main.matchmaking_queue[player]["joined_at"] = datetime.utcnow() - timedelta(
        seconds=seconds
    )


def _backdate_match(match_id, seconds):
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow() - timedelta(
        seconds=seconds
    )


def _win_three_rounds(client, auth_headers, match_id, player):
    """Drive a human match to completion with `player` winning 3-0."""
    for expected in (1, 2, 3):
        question = client.get(
            "/api/game/question",
            params={"match_id": match_id},
            headers=auth_headers(player),
        )
        assert question.status_code == 200, question.text
        answer = client.post(
            "/api/game/answer",
            json={"match_id": match_id, "answer": CORRECT},
            headers=auth_headers(player),
        )
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["correct"] is True
        assert expected in (body["player1_score"], body["player2_score"])
    assert main.in_memory_matches[match_id]["status"] == "completed"


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
    """Minimal Mongo matches stand-in for the flat queries main.py issues."""

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
        hits = [
            copy.deepcopy(d) for d in self.docs.values() if self._matches(d, query)
        ]
        return _FakeCursor(hits)

    async def update_one(self, query, update, *args, **kwargs):
        for doc in self.docs.values():
            if self._matches(doc, query):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                break
        return type(
            "R", (), {"modified_count": 1, "matched_count": 1, "upserted_id": None}
        )()

    async def delete_one(self, query, *args, **kwargs):
        for match_id, doc in list(self.docs.items()):
            if self._matches(doc, query):
                del self.docs[match_id]
                break
        return None


class LaggyMatchesDB(FakeMatchesDB):
    """Reads snapshot the document, then yield to the event loop before
    returning -- the classic 'response already in flight' DB race."""

    async def find_one(self, query, *args, **kwargs):
        snapshot = await super().find_one(query, *args, **kwargs)
        await asyncio.sleep(0)
        return snapshot


@pytest.fixture
def fake_matches_db(mock_mongo, monkeypatch):
    db = FakeMatchesDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.matches_collection, method, getattr(db, method))
    return db


@pytest.fixture
def laggy_matches_db(mock_mongo, monkeypatch):
    db = LaggyMatchesDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.matches_collection, method, getattr(db, method))
    return db


def _waiting_friend_match(client, auth_headers, creator=PLAYER_A):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(creator)
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "waiting"
    return body


# ---------------------------------------------------------------------------
# cancel when not in queue / double cancel
# ---------------------------------------------------------------------------


def test_cancel_when_not_in_queue_still_succeeds_and_flags_user(
    client, auth_headers
):
    # cancel_matchmaking never checks whether the user was queued: it is a
    # blind pop + set-add that always reports success.
    body = _cancel(client, auth_headers, PLAYER_A)
    assert body == {"status": "cancelled"}
    assert PLAYER_A not in main.matchmaking_queue
    assert PLAYER_A in main.cancelled_users


def test_cancel_twice_is_idempotent_on_state(client, auth_headers):
    _start(client, auth_headers, PLAYER_A)

    first = _cancel(client, auth_headers, PLAYER_A)
    second = _cancel(client, auth_headers, PLAYER_A)

    assert first == second == {"status": "cancelled"}
    assert PLAYER_A not in main.matchmaking_queue
    # A set can't double-count: exactly one flag entry.
    assert list(main.cancelled_users) == [PLAYER_A]


def test_cancel_does_not_touch_other_queued_users(client, auth_headers):
    # Two users searching concurrently (state as after two first polls).
    now = datetime.utcnow()
    main.matchmaking_queue[PLAYER_A] = {"elo": 1000, "joined_at": now}
    main.matchmaking_queue[PLAYER_B] = {"elo": 1000, "joined_at": now}

    _cancel(client, auth_headers, PLAYER_A)

    assert PLAYER_A not in main.matchmaking_queue
    assert PLAYER_B in main.matchmaking_queue
    assert PLAYER_B not in main.cancelled_users


# ---------------------------------------------------------------------------
# cancel before ever queueing poisons the first pairing (flag leak)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(stale-cancel-flag, never-queued): cancel_matchmaking flags a "
        "user who was never in the queue, and start_match never clears the "
        "flag when they queue for the first time afterwards.  Their first "
        "pairing is aborted with a bogus {'status': 'cancelled'} handed to "
        "the OPPONENT."
    ),
)
def test_cancel_before_ever_queueing_should_not_poison_first_pairing(
    client, auth_headers
):
    _cancel(client, auth_headers, PLAYER_A)  # stray cancel, e.g. UI misfire

    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    body = _start(client, auth_headers, PLAYER_B)
    assert body["status"] == "matched"  # currently: "cancelled"


def test_current_behavior_stray_cancel_aborts_first_pairing(
    client, auth_headers
):
    # BUG pin for the xfail above.
    _cancel(client, auth_headers, PLAYER_A)
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"

    body = _start(client, auth_headers, PLAYER_B)
    assert body == {"status": "cancelled"}
    assert main.in_memory_matches == {}
    # Collateral damage: BOTH users were popped from the queue, so A is
    # silently unqueued without ever being told.
    assert main.matchmaking_queue == {}
    assert PLAYER_A not in main.cancelled_users  # flag consumed here


# ---------------------------------------------------------------------------
# cancel after matched: flag leaks across a COMPLETED match
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(stale-cancel-flag, across-completion): a cancel that lands "
        "just after a match was created is never consumed (the reconnect "
        "path ignores cancelled_users).  The flag survives the entire "
        "match, and after completion it aborts the user's NEXT pairing."
    ),
)
def test_late_cancel_flag_should_not_survive_a_completed_match(
    client, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)
    _cancel(client, auth_headers, PLAYER_A)  # too late: match already exists

    # A keeps playing (reconnect path) and the match runs to completion.
    reconnect = _start(client, auth_headers, PLAYER_A)
    assert reconnect["status"] == "matched"
    _win_three_rounds(client, auth_headers, match_id, PLAYER_A)

    # New session: A queues again, C arrives.  This pairing must work.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    body = _start(client, auth_headers, PLAYER_C)
    assert body["status"] == "matched"  # currently: "cancelled"


def test_current_behavior_late_cancel_flag_survives_completed_match(
    client, auth_headers, fixed_question
):
    # BUG pin for the xfail above, including the intermediate states.
    match_id = _ranked_match(client, auth_headers)
    _cancel(client, auth_headers, PLAYER_A)
    assert PLAYER_A in main.cancelled_users

    assert _start(client, auth_headers, PLAYER_A)["status"] == "matched"
    assert PLAYER_A in main.cancelled_users  # reconnect did not consume it

    _win_three_rounds(client, auth_headers, match_id, PLAYER_A)
    assert PLAYER_A in main.cancelled_users  # completion did not consume it

    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    body = _start(client, auth_headers, PLAYER_C)
    assert body == {"status": "cancelled"}  # C never cancelled anything
    assert PLAYER_A not in main.cancelled_users


# ---------------------------------------------------------------------------
# cancelled_users growth / leak
# ---------------------------------------------------------------------------


def test_cancelled_users_grows_without_bound_and_never_expires(
    client, auth_headers
):
    # LEAK (documented): every cancel from a distinct user adds a set entry
    # that is only ever removed by a later pairing or bot-creation attempt
    # involving that user.  Users who cancel and walk away accumulate
    # forever (no TTL, no cap, survives any amount of unrelated traffic).
    for i in range(40):
        _cancel(client, auth_headers, f"guest-cq-leak-{i:03d}")

    assert len(main.cancelled_users) == 40

    # Unrelated users playing full matchmaking cycles shrink nothing.
    _ranked_match(client, auth_headers, PLAYER_A, PLAYER_B)
    assert len(main.cancelled_users) == 40
    assert all(f"guest-cq-leak-{i:03d}" in main.cancelled_users for i in range(40))


def test_cancelled_flag_is_consumed_only_pairwise_never_globally(
    client, auth_headers
):
    # Two flagged users; a pairing between OTHER users consumes neither.
    _cancel(client, auth_headers, PLAYER_A)
    _cancel(client, auth_headers, PLAYER_B)

    _ranked_match(client, auth_headers, "guest-cq-x1", "guest-cq-x2")
    assert main.cancelled_users == {PLAYER_A, PLAYER_B}

    # A pairing involving ONE flagged user consumes only the pair involved.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    aborted = _start(client, auth_headers, PLAYER_C)
    assert aborted == {"status": "cancelled"}
    assert main.cancelled_users == {PLAYER_B}


# ---------------------------------------------------------------------------
# queue entries never expire without polling; ghost matches
# ---------------------------------------------------------------------------


def test_queue_entry_survives_an_hour_without_polling(client, auth_headers):
    # The 10s bot deadline is only evaluated when the queued user POLLS.
    # A user who joins and never polls again sits in the queue forever.
    _start(client, auth_headers, PLAYER_A)
    _backdate_queue(PLAYER_A, 3600)

    assert PLAYER_A in main.matchmaking_queue
    joined_at = main.matchmaking_queue[PLAYER_A]["joined_at"]
    assert (datetime.utcnow() - joined_at).total_seconds() >= 3600
    # Nothing in the codebase sweeps the queue: still there, still matchable.


def test_hour_stale_queued_user_is_paired_into_a_ghost_match(
    client, auth_headers
):
    # Someone who joins later is matched against the hour-gone user: a
    # "ghost match" the absent player will never know about.
    _start(client, auth_headers, PLAYER_A)
    _backdate_queue(PLAYER_A, 3600)

    body = _start(client, auth_headers, PLAYER_B)
    assert body["status"] == "matched"
    match = main.in_memory_matches[body["match_id"]]
    assert {str(match["player1_id"]), str(match["player2_id"])} == {
        PLAYER_A,
        PLAYER_B,
    }
    assert match["status"] == "active"
    assert main.matchmaking_queue == {}


def test_ghost_opponent_is_reported_connected_and_blocks_give_up_auto_tie(
    client, auth_headers, fixed_question
):
    # QUIRK (documented in the presence suite as "never-seen counts as
    # connected"): the ghost never polls, so they have NO player_last_seen
    # entry, which is_player_connected treats as connected.  The live
    # player therefore sees a connected opponent forever, and give-up
    # cannot auto-tie -- they are stuck until the 5-minute round timeout.
    _start(client, auth_headers, PLAYER_A)
    _backdate_queue(PLAYER_A, 3600)
    body = _start(client, auth_headers, PLAYER_B)
    match_id = body["match_id"]

    status = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_B)
    ).json()
    assert status["opponent_connected"] is True  # A left an hour ago

    question = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_B),
    )
    assert question.status_code == 200

    gave_up = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_B),
    )
    assert gave_up.status_code == 200
    assert gave_up.json() == {"status": "gave_up", "waiting_for_opponent": True}


def test_ghost_opponent_reads_disconnected_once_they_polled_once(
    client, auth_headers
):
    # Contrast case: if the ghost polled even ONCE after matching, their
    # heartbeat goes stale 12s later and presence works as designed.
    _start(client, auth_headers, PLAYER_A)
    _backdate_queue(PLAYER_A, 3600)
    body = _start(client, auth_headers, PLAYER_B)
    match_id = body["match_id"]

    client.get(f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_A))
    main.in_memory_matches[match_id]["player_last_seen"][PLAYER_A] = (
        main.utc_now() - timedelta(seconds=13)
    )

    status = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_B)
    ).json()
    assert status["opponent_connected"] is False


# ---------------------------------------------------------------------------
# abandon-then-cancel interactions
# ---------------------------------------------------------------------------


def test_abandon_then_cancel_leaves_clean_queue_and_abandoned_match(
    client, auth_headers
):
    match_id = _ranked_match(client, auth_headers)
    _backdate_match(match_id, 10)

    # A re-searches: the stale match is abandoned and A re-enters the queue.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "abandoned"

    # Then A cancels the new search.
    _cancel(client, auth_headers, PLAYER_A)
    assert main.matchmaking_queue == {}
    assert PLAYER_A in main.cancelled_users
    # The abandonment is not undone by the cancel.
    assert main.in_memory_matches[match_id]["status"] == "abandoned"


def test_cancel_does_not_resurrect_or_complete_an_abandoned_match(
    client, auth_headers
):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"

    _cancel(client, auth_headers, PLAYER_A)
    assert main.in_memory_matches[match_id]["status"] == "abandoned"

    # /api/game/active agrees for both players.
    for player in (PLAYER_A, PLAYER_B):
        active = client.get(
            "/api/game/active", headers=auth_headers(player)
        ).json()
        assert active == {"has_active_match": False}


def test_abandoned_players_can_repair_after_mutual_cancel_round_trip(
    client, auth_headers
):
    # Full lifecycle: match -> stale -> abandon -> both cancel -> both
    # re-queue.  The pairing succeeds only because the two stale flags are
    # consumed as a PAIR by the same aborted attempt, and both users then
    # retry clean.
    match_id = _ranked_match(client, auth_headers)
    _backdate_match(match_id, 10)
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"

    _cancel(client, auth_headers, PLAYER_A)
    _cancel(client, auth_headers, PLAYER_B)

    # Both re-queue; the first pairing attempt is eaten by the stale flags.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    assert _start(client, auth_headers, PLAYER_B) == {"status": "cancelled"}

    # Second attempt: clean flags, pairing works again.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    rematch = _start(client, auth_headers, PLAYER_B)
    assert rematch["status"] == "matched"
    assert rematch["match_id"] != match_id


# ---------------------------------------------------------------------------
# cancel_challenge on waiting friend matches
# ---------------------------------------------------------------------------


def test_cancel_challenge_wipes_waiting_match_from_db_and_memory(
    client, auth_headers, fake_matches_db
):
    created = _waiting_friend_match(client, auth_headers)
    match_id = created["match_id"]
    assert match_id in fake_matches_db.docs
    assert match_id in main.in_memory_matches

    response = client.post(
        f"/api/challenges/cancel/{match_id}", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200
    assert response.json() == {"status": "cancelled"}

    assert match_id not in fake_matches_db.docs
    assert match_id not in main.in_memory_matches

    # The code is dead on every surface afterwards.
    assert (
        client.get(f"/api/game/friend/status/{created['match_code']}").status_code
        == 404
    )
    join = client.post(
        "/api/game/friend/join",
        json={"match_code": created["match_code"]},
        headers=auth_headers(PLAYER_B),
    )
    assert join.status_code == 404


def test_cancel_challenge_orphans_round_and_lock_created_before_cancel(
    client, auth_headers, fake_matches_db, fixed_question
):
    # LEAK (documented): gameplay routes happily serve a WAITING match, so
    # the creator can fetch a question (creating a round + match lock)
    # before anyone joins.  cancel_challenge deletes only the match doc --
    # the round and the lock stay behind forever.
    created = _waiting_friend_match(client, auth_headers)
    match_id = created["match_id"]

    question = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    assert question.status_code == 200
    round_id = question.json()["round_id"]
    assert round_id in main.in_memory_rounds
    assert match_id in main.match_locks

    response = client.post(
        f"/api/challenges/cancel/{match_id}", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200

    assert match_id not in main.in_memory_matches
    # Orphans: nobody will ever clean these up.
    assert round_id in main.in_memory_rounds
    assert match_id in main.match_locks


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(no-memory-fallback): cancel_challenge reads only from "
        "matches_collection.  A waiting match that exists only in "
        "in_memory_matches (DB down/empty) is fully joinable via its code "
        "but 404s on cancel -- the creator cannot kill their own match."
    ),
)
def test_memory_only_waiting_match_should_be_cancellable(client, auth_headers):
    # Default conftest mocks: DB always misses; the match lives in memory.
    created = _waiting_friend_match(client, auth_headers)
    match_id = created["match_id"]
    assert match_id in main.in_memory_matches

    response = client.post(
        f"/api/challenges/cancel/{match_id}", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200  # currently: 404


def test_current_behavior_memory_only_match_uncancellable_but_joinable(
    client, auth_headers
):
    # BUG pin for the xfail above: cancel 404s while join succeeds on the
    # very same memory-only match.
    created = _waiting_friend_match(client, auth_headers)
    match_id = created["match_id"]

    cancel = client.post(
        f"/api/challenges/cancel/{match_id}", headers=auth_headers(PLAYER_A)
    )
    assert cancel.status_code == 404

    join = client.post(
        "/api/game/friend/join",
        json={"match_code": created["match_code"]},
        headers=auth_headers(PLAYER_B),
    )
    assert join.status_code == 200
    assert main.in_memory_matches[match_id]["status"] == "active"


# ---------------------------------------------------------------------------
# racing cancel vs pair (queue) and cancel_challenge vs join (friend)
# ---------------------------------------------------------------------------


def test_racing_queue_cancel_vs_pairing_yields_no_ghost_match(
    mock_mongo, monkeypatch
):
    # A is queued with an ObjectId identity, so the pairing path awaits
    # users_collection.find_one BETWEEN selecting A and popping A -- the
    # exact window where A's cancel can land.
    queued_id = str(ObjectId())

    async def yielding_find_one(*args, **kwargs):
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(main.users_collection, "find_one", yielding_find_one)
    main.matchmaking_queue[queued_id] = {
        "elo": 1000,
        "joined_at": datetime.utcnow(),
    }

    async def run():
        return await asyncio.gather(
            main.start_match(
                main.MatchStart(mode="random"),
                current_user={"_id": PLAYER_B, "elo": 1000},
            ),
            main.cancel_matchmaking(current_user={"_id": queued_id}),
        )

    pair_result, cancel_result = asyncio.run(run())

    # The cancel ran inside the pairing's await window: the joiner is told
    # "cancelled" (spurious for THEM, see the stale-flag bugs) but no ghost
    # match is created for the canceller, and all flags are consumed.
    assert cancel_result == {"status": "cancelled"}
    assert pair_result == {"status": "cancelled"}
    assert main.in_memory_matches == {}
    assert queued_id not in main.matchmaking_queue
    assert queued_id not in main.cancelled_users
    assert PLAYER_B not in main.matchmaking_queue  # silently dropped too


def test_cancel_landing_before_pairing_scan_just_requeues_the_joiner(
    mock_mongo,
):
    queued_id = str(ObjectId())
    main.matchmaking_queue[queued_id] = {
        "elo": 1000,
        "joined_at": datetime.utcnow(),
    }

    async def run():
        cancel_result = await main.cancel_matchmaking(
            current_user={"_id": queued_id}
        )
        pair_result = await main.start_match(
            main.MatchStart(mode="random"),
            current_user={"_id": PLAYER_B, "elo": 1000},
        )
        return cancel_result, pair_result

    cancel_result, pair_result = asyncio.run(run())

    assert cancel_result == {"status": "cancelled"}
    # Queue was already empty when B scanned it: B simply starts searching.
    assert pair_result == {"status": "searching", "time_remaining": 10}
    assert list(main.matchmaking_queue.keys()) == [PLAYER_B]
    assert main.in_memory_matches == {}
    # The canceller's flag lingers, waiting to poison their next pairing.
    assert queued_id in main.cancelled_users


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(cancel-vs-join race): neither join_friend_match nor "
        "cancel_challenge takes the per-match lock, and both do "
        "check-then-act around awaited DB reads.  With any DB read latency "
        "a join and a cancel can BOTH succeed: the joiner gets a 200 for a "
        "match that the cancel then deletes from memory and DB."
    ),
)
def test_racing_cancel_challenge_vs_join_must_not_both_succeed(
    laggy_matches_db, client, auth_headers
):
    created = _waiting_friend_match(client, auth_headers)
    match_id = created["match_id"]

    async def run():
        return await asyncio.gather(
            main.join_friend_match(
                main.FriendMatchJoin(match_code=created["match_code"]),
                current_user={"_id": PLAYER_B, "elo": 1000},
            ),
            main.cancel_challenge(match_id, current_user={"_id": PLAYER_A}),
            return_exceptions=True,
        )

    join_result, cancel_result = asyncio.run(run())

    join_ok = isinstance(join_result, dict) and join_result.get("status") == "active"
    cancel_ok = (
        isinstance(cancel_result, dict)
        and cancel_result.get("status") == "cancelled"
    )
    # Exactly one of the two conflicting operations may win.
    assert not (join_ok and cancel_ok)  # currently: both True


def test_current_behavior_cancel_challenge_vs_join_race_deletes_joined_match(
    laggy_matches_db, client, auth_headers
):
    # BUG pin for the xfail above: both calls succeed and the acknowledged
    # joiner is left holding a match id that no longer exists anywhere.
    created = _waiting_friend_match(client, auth_headers)
    match_id = created["match_id"]

    async def run():
        return await asyncio.gather(
            main.join_friend_match(
                main.FriendMatchJoin(match_code=created["match_code"]),
                current_user={"_id": PLAYER_B, "elo": 1000},
            ),
            main.cancel_challenge(match_id, current_user={"_id": PLAYER_A}),
        )

    join_result, cancel_result = asyncio.run(run())

    assert join_result["status"] == "active"  # B was told they joined
    assert cancel_result == {"status": "cancelled"}  # A was told it's gone
    assert match_id not in main.in_memory_matches
    assert match_id not in laggy_matches_db.docs

    # B's "active" match 404s on every subsequent request.
    question = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_B),
    )
    assert question.status_code == 404


def test_sequential_join_then_cancel_challenge_rejects_the_cancel(
    fake_matches_db, client, auth_headers
):
    # Without the race window the guard works: once joined, cancel is 400.
    created = _waiting_friend_match(client, auth_headers)
    join = client.post(
        "/api/game/friend/join",
        json={"match_code": created["match_code"]},
        headers=auth_headers(PLAYER_B),
    )
    assert join.status_code == 200

    cancel = client.post(
        f"/api/challenges/cancel/{created['match_id']}",
        headers=auth_headers(PLAYER_A),
    )
    assert cancel.status_code == 400
    assert cancel.json()["detail"] == "Challenge already active or completed"
    assert created["match_id"] in fake_matches_db.docs
