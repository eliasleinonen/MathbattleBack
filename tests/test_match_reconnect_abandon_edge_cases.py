"""
Deep edge-case tests for the 5-second reconnect window in /api/game/start
and its abandonment side effects on people-vs-people matches.

Covers:
- exact window boundaries (0s / 4.999s / 5.000s / 5.001s ages, plus a
  future created_at) under a frozen clock, pinning the strict `< 5`
- which match wins the reconnect when one user has several active matches
  (insertion order, the scan's early return, one call abandoning one match
  while reconnecting into another, mass abandonment)
- the continue_existing True/False matrix crossed with friend vs ranked
  matches, for both recent (<5s) and stale (>5s) matches
- abandonment being a memory-only mutation: no matches_collection write,
  Mongo keeps "active", and an evicted abandoned match resurrects
- reconnecting while the opponent is mid-round (round state untouched,
  both the opponent's and the reconnector's answers still land)
- completed matches: never reconnected, never abandoned, re-queue pairs a
  fresh match
- abandoned matches: never reconnected, re-queue pairs a fresh match
- both players of one match reconnecting into the same match (no
  duplicates, empty queue), including at the 4.999s boundary
- ISO-string created_at: the TypeError 500 only hits participants of the
  corrupted ACTIVE match (outsiders keep matchmaking), continue_existing
  cannot dodge it, and non-active corrupted matches are harmless.  The
  direct participant 500 itself is already pinned in
  test_match_datetime_and_memory_edge_cases.py.
- missing created_at: defaults to "now" on every scan, so the match is
  permanently inside the reconnect window and can never be abandoned
  (the caller is trapped until the match completes)

Conventions (same as the sibling edge-case files):
- Guest identities via "Bearer guest-xxx" tokens.
- Known bugs are documented with strict xfail markers plus sibling tests
  that pin the CURRENT behavior.  See MATCH_EDGE_CASE_REPORT.md.
"""

import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main


PLAYER_A = "guest-rca-aaa"
PLAYER_B = "guest-rca-bbb"
PLAYER_C = "guest-rca-ccc"
OUTSIDER = "guest-rca-outsider"

CORRECT = "2*x"  # matches fixed_question's answer "2·x"


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_no_reraise(mock_mongo):
    """Client that returns the handler's 500 instead of re-raising in-test."""
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


class _FrozenDatetime(datetime):
    """datetime subclass whose utcnow()/now() return a fixed instant.

    Subclassing keeps fromisoformat / arithmetic / isinstance checks intact
    while making the reconnect-window age math exact to the microsecond.
    """

    frozen_naive = None

    @classmethod
    def utcnow(cls):
        return cls.frozen_naive

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.frozen_naive
        return cls.frozen_naive.replace(tzinfo=timezone.utc).astimezone(tz)


@pytest.fixture
def frozen_clock(monkeypatch):
    """Freeze main's `datetime` name so utcnow() is exact and steady."""

    class _Frozen(_FrozenDatetime):
        pass

    _Frozen.frozen_naive = datetime.utcnow()
    monkeypatch.setattr(main, "datetime", _Frozen)
    return _Frozen.frozen_naive


@pytest.fixture
def match_update_spy(mock_mongo, monkeypatch):
    """Record every matches_collection.update_one call."""
    calls = []

    class _Result:
        modified_count = 1
        matched_count = 1
        upserted_id = None

    async def update_one(query, update, *args, **kwargs):
        calls.append((query, update))
        return _Result()

    monkeypatch.setattr(main.matches_collection, "update_one", update_one)
    return calls


@pytest.fixture
def fake_matches_db(mock_mongo, monkeypatch):
    """matches_collection.find_one backed by a dict keyed on _id.

    Returns a deepcopy per call, like Motor materializing a fresh document
    for every query.
    """
    docs = {}

    async def find_one(query, *args, **kwargs):
        doc = docs.get(query.get("_id"))
        return copy.deepcopy(doc) if doc is not None else None

    monkeypatch.setattr(main.matches_collection, "find_one", find_one)
    return docs


def _start(client, auth_headers, player, continue_existing=False):
    response = client.post(
        "/api/game/start",
        json={"mode": "random", "continue_existing": continue_existing},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _friend_match(client, auth_headers, p1=PLAYER_A, p2=PLAYER_B):
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


def _ranked_match(client, auth_headers, first=PLAYER_A, second=PLAYER_B):
    """Queue `first`, then `second` arrives and pairs (second is player1)."""
    searching = _start(client, auth_headers, first)
    assert searching["status"] == "searching"
    matched = _start(client, auth_headers, second)
    assert matched["status"] == "matched", matched
    return matched["match_id"]


def _make_match(client, auth_headers, kind):
    if kind == "ranked":
        return _ranked_match(client, auth_headers)
    return _friend_match(client, auth_headers)


def _backdate(match_id, seconds):
    main.in_memory_matches[match_id]["created_at"] = (
        datetime.utcnow() - timedelta(seconds=seconds)
    )


def _question(client, auth_headers, match_id, player):
    return client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )


def _answer(client, auth_headers, match_id, player, answer=CORRECT):
    return client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )


def _status(client, auth_headers, match_id, player):
    return client.get(f"/api/game/status/{match_id}", headers=auth_headers(player))


# ---------------------------------------------------------------------------
# 1. exact window boundaries under a frozen clock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        pytest.param(timedelta(seconds=0), "matched", id="0.000s"),
        pytest.param(
            timedelta(seconds=4, milliseconds=999), "matched", id="4.999s"
        ),
        pytest.param(timedelta(seconds=5), "searching", id="5.000s"),
        pytest.param(
            timedelta(seconds=5, milliseconds=1), "searching", id="5.001s"
        ),
    ],
)
def test_reconnect_window_boundary_is_strictly_less_than_five_seconds(
    client, auth_headers, frozen_clock, age, expected
):
    # The window is `match_age < 5`: 4.999s reconnects, exactly 5.000s does
    # not — the boundary instant itself already abandons the match.
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["created_at"] = frozen_clock - age

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == expected
    if expected == "matched":
        assert body["match_id"] == match_id
        assert main.in_memory_matches[match_id]["status"] == "active"
        assert PLAYER_A not in main.matchmaking_queue
    else:
        assert main.in_memory_matches[match_id]["status"] == "abandoned"
        assert PLAYER_A in main.matchmaking_queue


def test_future_created_at_counts_as_recent_and_reconnects(
    client, auth_headers, frozen_clock
):
    # Clock skew pin: a created_at from the future gives a NEGATIVE age,
    # which is `< 5`, so the match is treated as brand new.
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["created_at"] = frozen_clock + timedelta(
        hours=1
    )

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == match_id


# ---------------------------------------------------------------------------
# 2. multiple active matches for one user: which one reconnects
# ---------------------------------------------------------------------------


def _two_friend_matches(client, auth_headers):
    """PLAYER_A active in two friend matches (vs B, then vs C)."""
    first = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    second = _friend_match(client, auth_headers, PLAYER_A, PLAYER_C)
    return first, second


def test_first_inserted_recent_match_wins_the_reconnect(client, auth_headers):
    # The scan iterates in_memory_matches in insertion order and returns on
    # the first active <5s match involving the caller — the OLDER entry in
    # the dict wins, the second match is never even looked at.
    first, second = _two_friend_matches(client, auth_headers)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == first
    assert main.in_memory_matches[second]["status"] == "active"


def test_stale_first_match_is_abandoned_and_recent_second_reconnects_same_call(
    client, auth_headers
):
    # One /start call does both: abandons the stale first match as it walks
    # past it, then reconnects into the still-recent second one.
    first, second = _two_friend_matches(client, auth_headers)
    _backdate(first, 60)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == second
    assert main.in_memory_matches[first]["status"] == "abandoned"
    assert main.in_memory_matches[second]["status"] == "active"


def test_scan_early_return_leaves_later_stale_match_active(client, auth_headers):
    # Quirk pin: the early return on the first recent match means a LATER
    # stale match is never scanned, so it silently stays active instead of
    # being abandoned — the user keeps two live matches.
    first, second = _two_friend_matches(client, auth_headers)
    _backdate(second, 60)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == first
    assert main.in_memory_matches[second]["status"] == "active"


def test_all_stale_matches_are_abandoned_in_one_search(client, auth_headers):
    # With no recent match to return into, the scan walks the whole dict
    # and abandons every stale active match the caller is part of.
    first, second = _two_friend_matches(client, auth_headers)
    _backdate(first, 60)
    _backdate(second, 60)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert main.in_memory_matches[first]["status"] == "abandoned"
    assert main.in_memory_matches[second]["status"] == "abandoned"
    assert PLAYER_A in main.matchmaking_queue


# ---------------------------------------------------------------------------
# 3. continue_existing True/False x friend/ranked matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["ranked", "friend"])
@pytest.mark.parametrize("continue_existing", [True, False])
def test_stale_match_matrix_continue_existing_only_toggles_abandonment(
    client, auth_headers, kind, continue_existing
):
    # For a >5s-old active match, /start ALWAYS answers "searching":
    # continue_existing=True never returns the old match (the documented
    # quirk from the ranked suite), it only decides whether the old match
    # is marked abandoned (False) or silently left active (True).  The
    # friend rows additionally re-pin the match_type-blind scan (bug 7):
    # queueing for ranked abandons a live FRIEND match too.
    match_id = _make_match(client, auth_headers, kind)
    _backdate(match_id, 10)

    body = _start(client, auth_headers, PLAYER_A, continue_existing=continue_existing)
    assert body["status"] == "searching"
    assert body.get("match_id") is None  # never "continued" into
    expected = "active" if continue_existing else "abandoned"
    assert main.in_memory_matches[match_id]["status"] == expected
    assert PLAYER_A in main.matchmaking_queue


@pytest.mark.parametrize("kind", ["ranked", "friend"])
@pytest.mark.parametrize("continue_existing", [True, False])
def test_recent_match_matrix_flag_is_irrelevant_inside_the_window(
    client, auth_headers, kind, continue_existing
):
    # For a <5s-old active match the reconnect branch fires before
    # continue_existing is ever consulted: both flag values reconnect.
    # The friend rows pin the hijack flavor of bug 7 (a ranked /start
    # "reconnects" into a fresh friend match).
    match_id = _make_match(client, auth_headers, kind)

    body = _start(client, auth_headers, PLAYER_A, continue_existing=continue_existing)
    assert body["status"] == "matched"
    assert body["match_id"] == match_id
    assert main.in_memory_matches[match_id]["status"] == "active"
    assert PLAYER_A not in main.matchmaking_queue


# ---------------------------------------------------------------------------
# 4. abandonment is a memory-only mutation (never persisted to Mongo)
# ---------------------------------------------------------------------------


def test_current_behavior_abandonment_never_written_to_db(
    client, auth_headers, match_update_spy, fake_matches_db
):
    # BUG pin (see xfail below): `match["status"] = "abandoned"` mutates the
    # in-memory doc only.  No matches_collection.update_one is issued, so
    # Mongo keeps status "active" forever.
    match_id = _ranked_match(client, auth_headers)
    fake_matches_db[match_id] = copy.deepcopy(main.in_memory_matches[match_id])
    _backdate(match_id, 10)
    match_update_spy.clear()

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "abandoned"

    touched = [q for q, _ in match_update_spy if q.get("_id") == match_id]
    assert touched == []  # not a single write for the abandoned match
    assert fake_matches_db[match_id]["status"] == "active"


def test_current_behavior_evicted_abandoned_match_resurrects_as_active(
    client, auth_headers, fake_matches_db
):
    # BUG pin (see xfail below): because the abandonment never reached the
    # DB, a memory eviction (restart / worker switch) resurrects the match:
    # the status poll hydrates the stale "active" doc and /api/game/active
    # starts advertising the supposedly-dead match again.
    match_id = _ranked_match(client, auth_headers)
    fake_matches_db[match_id] = copy.deepcopy(main.in_memory_matches[match_id])
    _backdate(match_id, 10)

    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "abandoned"

    del main.in_memory_matches[match_id]  # simulated eviction

    status = _status(client, auth_headers, match_id, PLAYER_A)
    assert status.status_code == 200
    assert status.json()["status"] == "active"  # resurrected!
    active = client.get("/api/game/active", headers=auth_headers(PLAYER_A)).json()
    assert active["has_active_match"] is True
    assert active["match_id"] == match_id


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(memory-only-abandonment): start_match marks a stale match "
        "abandoned by mutating the in-memory doc only — no "
        "matches_collection.update_one follows, so Mongo keeps status "
        "'active'.  After an eviction/restart, the hydrate paths reload the "
        "stale doc and the abandoned match resurrects as active for both "
        "players (and reappears in /api/game/active)."
    ),
)
def test_abandonment_should_survive_memory_eviction(
    client, auth_headers, fake_matches_db
):
    match_id = _ranked_match(client, auth_headers)
    fake_matches_db[match_id] = copy.deepcopy(main.in_memory_matches[match_id])
    _backdate(match_id, 10)

    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    del main.in_memory_matches[match_id]  # simulated eviction

    status = _status(client, auth_headers, match_id, PLAYER_A)
    assert status.json()["status"] == "abandoned"


# ---------------------------------------------------------------------------
# 5. reconnect while the opponent is mid-round
# ---------------------------------------------------------------------------


def test_reconnect_mid_round_preserves_round_state_and_opponent_answer_lands(
    client, auth_headers, fixed_question
):
    # B (player1) is mid-round when A's /start poll reconnects: the round
    # doc, current_round_id and countdown anchor must all survive, and B's
    # in-flight answer still wins the round afterwards.
    match_id = _ranked_match(client, auth_headers)  # B is player1
    question = _question(client, auth_headers, match_id, PLAYER_B).json()

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == match_id

    match = main.in_memory_matches[match_id]
    assert match["current_round_id"] == question["round_id"]
    assert match["round_start_time"] == question["round_start_time"]
    assert main.in_memory_rounds[question["round_id"]]["question"] == "x^2"
    assert main.in_memory_rounds[question["round_id"]]["winner_id"] is None

    answered = _answer(client, auth_headers, match_id, PLAYER_B).json()
    assert answered["correct"] is True
    assert str(answered["round_winner"]) == PLAYER_B
    assert answered["player1_score"] == 1


def test_reconnector_can_steal_the_open_round_after_reconnecting(
    client, auth_headers, fixed_question
):
    # The reconnect does not reset or forfeit the open round: the returning
    # player answers the very round their opponent was working on and wins it.
    match_id = _ranked_match(client, auth_headers)  # A is player2
    question = _question(client, auth_headers, match_id, PLAYER_B).json()

    assert _start(client, auth_headers, PLAYER_A)["status"] == "matched"

    answered = _answer(client, auth_headers, match_id, PLAYER_A).json()
    assert answered["correct"] is True
    assert str(answered["round_winner"]) == PLAYER_A
    assert answered["player2_score"] == 1
    # Still the same round the opponent was answering.
    assert main.in_memory_matches[match_id]["current_round_id"] == (
        question["round_id"]
    )


# ---------------------------------------------------------------------------
# 6. completed matches never reconnect (and are never abandoned)
# ---------------------------------------------------------------------------


def test_fresh_completed_match_is_not_reconnected(client, auth_headers):
    # Even inside the <5s window a completed match must not resurrect.
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "completed"

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "completed"
    assert PLAYER_A in main.matchmaking_queue


def test_stale_completed_match_is_not_marked_abandoned(client, auth_headers):
    # The abandonment branch only runs for status "active": a >5s-old
    # completed match keeps its terminal status when its player re-queues.
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "completed"
    _backdate(match_id, 60)

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "completed"


def test_requeue_after_completion_pairs_a_fresh_match(client, auth_headers):
    old_id = _ranked_match(client, auth_headers)
    old_code = main.in_memory_matches[old_id]["match_code"]
    main.in_memory_matches[old_id]["status"] = "completed"

    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    body = _start(client, auth_headers, PLAYER_B)
    assert body["status"] == "matched"
    assert body["match_id"] != old_id
    assert body["match_code"] != old_code
    assert main.in_memory_matches[old_id]["status"] == "completed"
    assert main.in_memory_matches[body["match_id"]]["status"] == "active"


# ---------------------------------------------------------------------------
# 7. abandoned matches never reconnect; the search goes on
# ---------------------------------------------------------------------------


def test_fresh_abandoned_match_is_not_reconnected(client, auth_headers):
    # Even inside the <5s window an already-abandoned match is skipped and
    # the caller goes back to searching.
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "abandoned"
    assert PLAYER_A in main.matchmaking_queue


def test_requeue_after_abandonment_pairs_a_fresh_match_old_stays_dead(
    client, auth_headers
):
    old_id = _ranked_match(client, auth_headers)
    _backdate(old_id, 10)

    # A's poll abandons the stale match and queues A; B's poll pairs them.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    assert main.in_memory_matches[old_id]["status"] == "abandoned"

    body = _start(client, auth_headers, PLAYER_B)
    assert body["status"] == "matched"
    assert body["match_id"] != old_id
    assert main.in_memory_matches[old_id]["status"] == "abandoned"
    assert main.in_memory_matches[body["match_id"]]["status"] == "active"
    assert main.matchmaking_queue == {}


# ---------------------------------------------------------------------------
# 8. both players reconnecting into the same new match
# ---------------------------------------------------------------------------


def test_both_players_reconnect_into_the_same_match(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)

    reconnect_a = _start(client, auth_headers, PLAYER_A)
    reconnect_b = _start(client, auth_headers, PLAYER_B)

    assert reconnect_a["status"] == reconnect_b["status"] == "matched"
    assert reconnect_a["match_id"] == reconnect_b["match_id"] == match_id
    assert reconnect_a["match_code"] == reconnect_b["match_code"]
    assert len(main.in_memory_matches) == 1
    assert main.matchmaking_queue == {}


def test_interleaved_reconnect_polls_never_fork_the_match(client, auth_headers):
    # Frontends poll /start repeatedly; alternate A/B polls must keep
    # landing in the one match without ever re-queueing either player.
    match_id = _ranked_match(client, auth_headers)

    for player in (PLAYER_A, PLAYER_B, PLAYER_A, PLAYER_B, PLAYER_A):
        body = _start(client, auth_headers, player)
        assert body["status"] == "matched"
        assert body["match_id"] == match_id

    assert len(main.in_memory_matches) == 1
    assert main.matchmaking_queue == {}


def test_both_players_reconnect_at_the_4999ms_boundary(
    client, auth_headers, frozen_clock
):
    # Both polls happen at the last representable instant inside the
    # window: both players still route into the same match.
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["created_at"] = frozen_clock - timedelta(
        seconds=4, milliseconds=999
    )

    reconnect_a = _start(client, auth_headers, PLAYER_A)
    reconnect_b = _start(client, auth_headers, PLAYER_B)
    assert reconnect_a["match_id"] == reconnect_b["match_id"] == match_id
    assert len(main.in_memory_matches) == 1


# ---------------------------------------------------------------------------
# 9. ISO-string created_at: blast radius of the TypeError 500
#    (the direct participant 500 is already pinned in
#    test_match_datetime_and_memory_edge_cases.py)
# ---------------------------------------------------------------------------


def test_iso_created_at_500s_participants_but_not_outsiders(
    client_no_reraise, auth_headers
):
    # The age subtraction only runs for ACTIVE matches the caller is part
    # of, so a corrupted created_at breaks matchmaking for that match's two
    # players while every other user keeps matchmaking normally.
    match_id = _ranked_match(client_no_reraise, auth_headers)
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow().isoformat()

    outsider = client_no_reraise.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(OUTSIDER)
    )
    assert outsider.status_code == 200
    assert outsider.json()["status"] == "searching"

    for participant in (PLAYER_A, PLAYER_B):
        broken = client_no_reraise.post(
            "/api/game/start",
            json={"mode": "random"},
            headers=auth_headers(participant),
        )
        assert broken.status_code == 500


def test_continue_existing_cannot_dodge_the_iso_created_at_500(
    client_no_reraise, auth_headers
):
    # The subtraction happens BEFORE continue_existing is consulted, so the
    # flag offers no escape hatch from the corrupted match.
    match_id = _ranked_match(client_no_reraise, auth_headers)
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow().isoformat()

    response = client_no_reraise.post(
        "/api/game/start",
        json={"mode": "random", "continue_existing": True},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 500


def test_iso_created_at_on_non_active_match_is_harmless(
    client_no_reraise, auth_headers
):
    # Non-active matches are skipped before the age math, so the same
    # corrupted timestamp on an abandoned match doesn't break anyone.
    match_id = _ranked_match(client_no_reraise, auth_headers)
    main.in_memory_matches[match_id]["status"] = "abandoned"
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow().isoformat()

    response = client_no_reraise.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "searching"


# ---------------------------------------------------------------------------
# 10. missing created_at defaults to "now" on every scan
#     (the basic reconnect flavor is already pinned in
#     test_match_datetime_and_memory_edge_cases.py)
# ---------------------------------------------------------------------------


def test_missing_created_at_match_can_never_be_abandoned(client, auth_headers):
    # Quirk pin: match.get("created_at", datetime.utcnow()) re-defaults to
    # the CURRENT time on every scan, so the match is permanently "0s old".
    # However often the player re-searches (continue_existing=False), the
    # abandonment branch can never fire — they are trapped in the match and
    # can never re-enter the queue.
    match_id = _ranked_match(client, auth_headers)
    del main.in_memory_matches[match_id]["created_at"]

    for _ in range(3):
        body = _start(client, auth_headers, PLAYER_A)
        assert body["status"] == "matched"
        assert body["match_id"] == match_id

    assert main.in_memory_matches[match_id]["status"] == "active"
    assert PLAYER_A not in main.matchmaking_queue


def test_missing_created_at_trap_is_released_only_by_leaving_active_status(
    client, auth_headers
):
    # Once the match reaches a non-active status the scan skips it and the
    # trapped player can finally search again.
    match_id = _ranked_match(client, auth_headers)
    del main.in_memory_matches[match_id]["created_at"]
    assert _start(client, auth_headers, PLAYER_A)["status"] == "matched"

    main.in_memory_matches[match_id]["status"] = "completed"
    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"
    assert PLAYER_A in main.matchmaking_queue
