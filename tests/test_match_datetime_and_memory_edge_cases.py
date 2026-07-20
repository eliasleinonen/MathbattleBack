"""
Edge-case tests for datetime handling and in-memory vs Mongo state in
people-vs-people matches.

Covers:
- ensure_utc / parse_round_start unit behavior (naive, aware, ISO strings,
  offsets, "Z" suffix, garbage, non-datetime input)
- round_start_time format guarantees (aware ISO string with +00:00, ~3s
  countdown, stability across resume polls and the status endpoint)
- the split timestamp regime: match docs use naive datetime.utcnow() while
  round docs / presence use aware utc_now(), and the reconnect-window
  TypeError when an aware created_at leaks into a match doc
- the 5-minute round timeout across created_at representations (aware,
  naive, ISO string, garbage)
- in-memory-only match visibility when the DB misses, memory eviction
  mid-match, and the find_one hydrate paths (status/question/answer/give-up)
- match_counter reuse after a simulated process restart
- player_last_seen loss after a memory wipe mid-match

Conventions (same as the sibling edge-case files):
- Guest identities via "Bearer guest-xxx" tokens.
- Known bugs are documented with strict xfail markers plus a sibling test
  that pins the CURRENT behavior, so regressions in either direction are
  visible.  See MATCH_EDGE_CASE_REPORT.md for the narrative summary.
"""

import copy
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main


PLAYER_A = "guest-dtm-aaa"
PLAYER_B = "guest-dtm-bbb"
PLAYER_C = "guest-dtm-ccc"
PLAYER_D = "guest-dtm-ddd"
OUTSIDER = "guest-dtm-outsider"

CORRECT = "2*x"  # matches fixed_question's answer "2·x"


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_no_reraise(mock_mongo):
    """Client that returns the handler's 500 instead of re-raising in-test."""
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze main.utc_now so round-timeout boundary math is exact."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(main, "utc_now", lambda: now)
    return now


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


@pytest.fixture
def fake_rounds_db(mock_mongo, monkeypatch):
    """rounds_collection.find_one backed by a dict keyed on _id."""
    docs = {}

    async def find_one(query, *args, **kwargs):
        doc = docs.get(query.get("_id"))
        return copy.deepcopy(doc) if doc is not None else None

    monkeypatch.setattr(main.rounds_collection, "find_one", find_one)
    return docs


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
    searching = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(first)
    )
    assert searching.json()["status"] == "searching"
    matched = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(second)
    )
    assert matched.json()["status"] == "matched", matched.text
    return matched.json()["match_id"]


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


def _give_up(client, auth_headers, match_id, player):
    return client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )


def _db_match_doc(match_id, p1=PLAYER_A, p2=PLAYER_B, **overrides):
    """A match document shaped like Motor would return it (naive datetimes,
    no player_last_seen — presence is never persisted)."""
    doc = {
        "_id": match_id,
        "match_code": "DBHYD1",
        "match_type": "friend",
        "player1_id": p1,
        "player1_username": None,
        "player2_id": p2,
        "player2_username": None,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": 1000,
        "player2_elo": 1000,
        "status": "active",
        "winner_id": None,
        "elo_change": 0,
        "created_at": datetime.utcnow(),
    }
    doc.update(overrides)
    return doc


def _db_round_doc(round_id, match_id, **overrides):
    doc = {
        "_id": round_id,
        "match_id": match_id,
        "round_number": 1,
        "question": "x^2",
        "answer": "2·x",
        "evaluate_at": 0,
        "ask_for_derivative_only": True,
        "difficulty": 1,
        "player1_answer": None,
        "player2_answer": None,
        "winner_id": None,
        "created_at": datetime.utcnow(),
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# ensure_utc unit tests
# ---------------------------------------------------------------------------


def test_ensure_utc_treats_naive_as_utc():
    naive = datetime(2024, 6, 1, 12, 30, 45)
    result = main.ensure_utc(naive)
    assert result.tzinfo == timezone.utc


def test_ensure_utc_preserves_wall_clock_of_naive_input():
    naive = datetime(2024, 6, 1, 12, 30, 45, 123456)
    result = main.ensure_utc(naive)
    assert (result.year, result.month, result.day) == (2024, 6, 1)
    assert (result.hour, result.minute, result.second) == (12, 30, 45)
    assert result.microsecond == 123456


def test_ensure_utc_returns_aware_utc_input_unchanged():
    aware = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    result = main.ensure_utc(aware)
    assert result is aware


def test_ensure_utc_does_not_convert_non_utc_offsets():
    # Quirk: despite the name, ensure_utc leaves an aware non-UTC datetime
    # completely untouched.  Comparisons stay correct because aware-aware
    # arithmetic normalizes offsets, but the returned tzinfo is NOT UTC.
    plus_five = timezone(timedelta(hours=5))
    aware = datetime(2024, 6, 1, 17, 0, tzinfo=plus_five)
    result = main.ensure_utc(aware)
    assert result is aware
    assert result.utcoffset() == timedelta(hours=5)


def test_ensure_utc_is_idempotent():
    naive = datetime(2024, 6, 1, 12, 0)
    once = main.ensure_utc(naive)
    twice = main.ensure_utc(once)
    assert twice == once
    assert twice.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# parse_round_start unit tests
# ---------------------------------------------------------------------------


def test_parse_round_start_none_is_none():
    assert main.parse_round_start(None) is None


def test_parse_round_start_naive_datetime_becomes_aware():
    naive = datetime(2024, 6, 1, 12, 0)
    result = main.parse_round_start(naive)
    assert result == datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_round_start_aware_datetime_passes_through():
    aware = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert main.parse_round_start(aware) is aware


def test_parse_round_start_naive_iso_string():
    result = main.parse_round_start("2024-06-01T12:00:00")
    assert result == datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_round_start_iso_string_with_utc_offset():
    result = main.parse_round_start("2024-06-01T12:00:00+00:00")
    assert result == datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_round_start_iso_string_preserves_non_utc_offset():
    # The offset is kept as-is (not normalized to UTC) but the instant is
    # correct: 17:00+05:00 == 12:00 UTC.
    result = main.parse_round_start("2024-06-01T17:00:00+05:00")
    assert result.utcoffset() == timedelta(hours=5)
    assert result == datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_round_start_z_suffix_depends_on_python_version():
    # datetime.fromisoformat only accepts a trailing "Z" from Python 3.11;
    # on older versions a JS-produced timestamp would silently become None
    # (and the round would never time out).
    result = main.parse_round_start("2024-06-01T12:00:00Z")
    if sys.version_info >= (3, 11):
        assert result == datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    else:
        assert result is None


def test_parse_round_start_garbage_string_is_none():
    assert main.parse_round_start("not-a-timestamp") is None


def test_parse_round_start_empty_string_is_none():
    assert main.parse_round_start("") is None


def test_parse_round_start_numeric_input_raises_attribute_error():
    # Pin: anything that is neither None, str nor datetime falls through to
    # ensure_utc, which assumes a .tzinfo attribute.  A unix timestamp float
    # therefore raises instead of parsing or returning None.  Not reachable
    # from current storage shapes, but worth knowing before feeding this
    # helper new inputs.
    with pytest.raises(AttributeError):
        main.parse_round_start(1717243200.0)


# ---------------------------------------------------------------------------
# round_start_time formats
# ---------------------------------------------------------------------------


def test_round_start_time_is_aware_iso_string_three_seconds_out(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    response = _question(client, auth_headers, match_id, PLAYER_A)
    assert response.status_code == 200

    stored = main.in_memory_matches[match_id]["round_start_time"]
    assert isinstance(stored, str)
    assert stored.endswith("+00:00")

    parsed = datetime.fromisoformat(stored)
    assert parsed.utcoffset() == timedelta(0)
    # Scheduled ~3s in the future at creation time; a little may have elapsed.
    lead = (parsed - datetime.now(timezone.utc)).total_seconds()
    assert 0 < lead <= 3.1


def test_round_start_time_identical_for_both_players_on_resume(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A).json()
    second = _question(client, auth_headers, match_id, PLAYER_B).json()

    # Same round, byte-identical countdown anchor for both clients.
    assert second["round_id"] == first["round_id"]
    assert second["round_start_time"] == first["round_start_time"]
    assert first["round_start_time"] == (
        main.in_memory_matches[match_id]["round_start_time"]
    )


def test_status_endpoint_echoes_round_start_time_verbatim(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    question = _question(client, auth_headers, match_id, PLAYER_A).json()

    status = _status(client, auth_headers, match_id, PLAYER_B).json()
    assert status["round_start_time"] == question["round_start_time"]


def test_new_round_gets_fresh_round_start_time(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    first = _question(client, auth_headers, match_id, PLAYER_A).json()
    assert _answer(client, auth_headers, match_id, PLAYER_A).json()["correct"] is True

    second = _question(client, auth_headers, match_id, PLAYER_A).json()
    assert second["round_id"] != first["round_id"]
    assert second["round_start_time"] != first["round_start_time"]
    assert datetime.fromisoformat(second["round_start_time"]) > (
        datetime.fromisoformat(first["round_start_time"])
    )


# ---------------------------------------------------------------------------
# naive utcnow() match timestamps vs aware utc_now() round timestamps
# ---------------------------------------------------------------------------


def test_ranked_match_created_at_is_naive(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    match = main.in_memory_matches[match_id]

    # Match docs still use datetime.utcnow(): naive timestamps.
    assert isinstance(match["created_at"], datetime)
    assert match["created_at"].tzinfo is None
    assert match["updated_at"].tzinfo is None


def test_friend_match_created_at_is_naive(client, auth_headers):
    match_id = _friend_match(client, auth_headers)
    assert main.in_memory_matches[match_id]["created_at"].tzinfo is None


def test_round_created_at_is_aware_unlike_match_created_at(
    client, auth_headers, fixed_question
):
    # Inconsistency: rounds were migrated to the aware utc_now() while match
    # docs stayed on naive utcnow().  The reconnect window only works because
    # both sides of its subtraction are naive; see the 500-tests below for
    # what happens when an aware value leaks into a match doc.
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()["round_id"]

    assert main.in_memory_rounds[round_id]["created_at"].tzinfo is not None
    assert main.in_memory_matches[match_id]["created_at"].tzinfo is None


def test_player_last_seen_entries_are_aware(client, auth_headers):
    match_id = _friend_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_A)

    last_seen = main.in_memory_matches[match_id]["player_last_seen"][PLAYER_A]
    assert last_seen.tzinfo is not None
    assert last_seen.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# reconnect window: aware / string created_at explode (naive-only math)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(naive-created-at): the reconnect window in start_match computes "
        "datetime.utcnow() - match['created_at'] with no ensure_utc, so an "
        "aware created_at (e.g. a doc migrated to utc_now(), or a Mongo "
        "client configured with tz_aware=True) raises TypeError and turns "
        "/api/game/start into a 500 for that player."
    ),
)
def test_aware_created_at_should_still_reconnect(client_no_reraise, auth_headers):
    match_id = _ranked_match(client_no_reraise, auth_headers)
    main.in_memory_matches[match_id]["created_at"] = datetime.now(timezone.utc)

    response = client_no_reraise.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_B)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "matched"


def test_current_behavior_aware_created_at_500s_the_start_endpoint(
    client_no_reraise, auth_headers
):
    # BUG pin for the xfail above: aware created_at -> TypeError -> generic 500.
    match_id = _ranked_match(client_no_reraise, auth_headers)
    main.in_memory_matches[match_id]["created_at"] = datetime.now(timezone.utc)

    response = client_no_reraise.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_B)
    )
    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."


def test_current_behavior_string_created_at_also_500s_the_start_endpoint(
    client_no_reraise, auth_headers
):
    # Same failure mode if created_at ever arrives as an ISO string (the
    # round docs already store ISO strings for started_at, so the shape is
    # not hypothetical): datetime - str raises TypeError.
    match_id = _ranked_match(client_no_reraise, auth_headers)
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow().isoformat()

    response = client_no_reraise.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_B)
    )
    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong. Please try again."


def test_missing_created_at_defaults_to_now_and_reconnects(client, auth_headers):
    # match.get("created_at", datetime.utcnow()) -> age ~0s -> reconnect.
    match_id = _ranked_match(client, auth_headers)
    del main.in_memory_matches[match_id]["created_at"]

    response = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_B)
    )
    body = response.json()
    assert body["status"] == "matched"
    assert body["match_id"] == match_id


# ---------------------------------------------------------------------------
# 5-minute round timeout across created_at representations
# ---------------------------------------------------------------------------


def test_round_exactly_at_300s_is_not_timed_out(
    client, auth_headers, fixed_question, frozen_now
):
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()["round_id"]

    # Strictly-greater-than comparison: exactly 300s still resumes.
    main.in_memory_rounds[round_id]["created_at"] = frozen_now - timedelta(seconds=300)
    resumed = _question(client, auth_headers, match_id, PLAYER_B).json()
    assert resumed["round_id"] == round_id
    assert main.in_memory_rounds[round_id]["winner_id"] is None


def test_round_past_300s_with_aware_created_at_ties_and_advances(
    client, auth_headers, fixed_question, frozen_now
):
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()["round_id"]

    main.in_memory_rounds[round_id]["created_at"] = frozen_now - timedelta(
        seconds=300, milliseconds=1
    )
    fresh = _question(client, auth_headers, match_id, PLAYER_B).json()
    assert fresh["round_id"] != round_id
    assert main.in_memory_rounds[round_id]["winner_id"] == "tie"


def test_round_past_300s_with_naive_created_at_ties_and_advances(
    client, auth_headers, fixed_question, frozen_now
):
    # A round hydrated from Mongo carries a naive created_at; ensure_utc in
    # parse_round_start keeps the timeout math working.
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()["round_id"]

    naive_old = frozen_now.replace(tzinfo=None) - timedelta(seconds=301)
    main.in_memory_rounds[round_id]["created_at"] = naive_old

    fresh = _question(client, auth_headers, match_id, PLAYER_B).json()
    assert fresh["round_id"] != round_id
    assert main.in_memory_rounds[round_id]["winner_id"] == "tie"


def test_round_past_300s_with_iso_string_created_at_ties_and_advances(
    client, auth_headers, fixed_question, frozen_now
):
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()["round_id"]

    main.in_memory_rounds[round_id]["created_at"] = (
        frozen_now - timedelta(seconds=301)
    ).isoformat()

    fresh = _question(client, auth_headers, match_id, PLAYER_B).json()
    assert fresh["round_id"] != round_id
    assert main.in_memory_rounds[round_id]["winner_id"] == "tie"


def test_unparseable_created_at_means_round_never_times_out(
    client, auth_headers, fixed_question, frozen_now
):
    # Quirk pin: parse_round_start returns None for garbage, and a None
    # round_start short-circuits the timeout to False — the round is resumed
    # forever instead of being tied.  A corrupted created_at therefore wedges
    # the match on one question until someone answers it.
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()["round_id"]

    main.in_memory_rounds[round_id]["created_at"] = "corrupted-timestamp"

    resumed = _question(client, auth_headers, match_id, PLAYER_B).json()
    assert resumed["round_id"] == round_id
    assert main.in_memory_rounds[round_id]["winner_id"] is None


# ---------------------------------------------------------------------------
# in-memory-only visibility when the DB misses
# ---------------------------------------------------------------------------


def test_full_match_playable_with_db_always_missing(
    client, auth_headers, fixed_question
):
    # conftest's mock_mongo returns None for every find_one: the whole match
    # lifecycle (create, join, 3 rounds, completion) runs from process memory.
    match_id = _friend_match(client, auth_headers)

    for expected_score in (1, 2, 3):
        assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 200
        body = _answer(client, auth_headers, match_id, PLAYER_A).json()
        assert body["correct"] is True
        assert body["player1_score"] == expected_score

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert str(match["winner_id"]) == PLAYER_A


def test_evicted_match_404s_on_every_gameplay_route(
    client, auth_headers, fixed_question
):
    # Simulates a memory wipe (deploy/restart) with the DB unavailable: the
    # match simply stops existing for both players mid-game.
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()["round_id"]
    code = main.in_memory_matches[match_id]["match_code"]

    del main.in_memory_matches[match_id]

    assert _status(client, auth_headers, match_id, PLAYER_A).status_code == 404
    assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 404
    assert _answer(client, auth_headers, match_id, PLAYER_A).status_code == 404
    assert _give_up(client, auth_headers, match_id, PLAYER_A).status_code == 404
    by_code = client.get(f"/api/game/match/{code}", headers=auth_headers(PLAYER_A))
    assert by_code.status_code == 404
    active = client.get("/api/game/active", headers=auth_headers(PLAYER_A)).json()
    assert active == {"has_active_match": False}

    # Quirk: the round doc and the match lock are orphaned in memory —
    # nothing cleans them up once the match itself is gone.
    assert round_id in main.in_memory_rounds
    assert match_id in main.match_locks


def test_db_only_listing_vs_memory_fallback_details(client, auth_headers):
    # /matches/all reads exclusively from the DB (invisible in-memory match),
    # while /match/{id}/details falls back to memory — the same match is
    # simultaneously "not in the history" and fully inspectable.
    match_id = _friend_match(client, auth_headers)

    listing = client.get("/matches/all", headers=auth_headers(PLAYER_A))
    assert listing.status_code == 200
    assert listing.json() == []

    details = client.get(f"/match/{match_id}/details", headers=auth_headers(PLAYER_A))
    assert details.status_code == 200
    assert details.json()["match_id"] == match_id


# ---------------------------------------------------------------------------
# hydrate paths: Mongo find_one returns a match that is not in memory
# ---------------------------------------------------------------------------


def test_status_hydrates_match_from_db_into_memory(
    client, fake_matches_db, auth_headers
):
    fake_matches_db["match-hyd-status"] = _db_match_doc("match-hyd-status")

    response = _status(client, auth_headers, "match-hyd-status", PLAYER_A)
    assert response.status_code == 200
    body = response.json()
    assert body["player1_id"] == PLAYER_A
    assert body["status"] == "active"

    # The doc is cached back so presence tracking survives subsequent polls.
    assert "match-hyd-status" in main.in_memory_matches
    assert PLAYER_A in main.in_memory_matches["match-hyd-status"]["player_last_seen"]


def test_question_hydrates_match_and_creates_first_round(
    client, fake_matches_db, auth_headers, fixed_question
):
    fake_matches_db["match-hyd-q"] = _db_match_doc("match-hyd-q")

    response = _question(client, auth_headers, "match-hyd-q", PLAYER_A)
    assert response.status_code == 200
    body = response.json()
    assert body["round_id"] == "round-match-hyd-q-1"
    assert "match-hyd-q" in main.in_memory_matches
    assert body["round_id"] in main.in_memory_rounds


def test_answer_hydrates_match_and_round_and_scores(
    client, fake_matches_db, fake_rounds_db, auth_headers
):
    fake_matches_db["match-hyd-a"] = _db_match_doc(
        "match-hyd-a", current_round_id="round-match-hyd-a-1"
    )
    fake_rounds_db["round-match-hyd-a-1"] = _db_round_doc(
        "round-match-hyd-a-1", "match-hyd-a"
    )

    body = _answer(client, auth_headers, "match-hyd-a", PLAYER_A).json()
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER_A
    assert body["player1_score"] == 1

    # Both docs were pulled into memory and mutated there.
    assert main.in_memory_matches["match-hyd-a"]["player1_score"] == 1
    assert str(main.in_memory_rounds["round-match-hyd-a-1"]["winner_id"]) == PLAYER_A


def test_give_up_hydrates_match_and_round(
    client, fake_matches_db, fake_rounds_db, auth_headers
):
    fake_matches_db["match-hyd-g"] = _db_match_doc(
        "match-hyd-g", current_round_id="round-match-hyd-g-1"
    )
    fake_rounds_db["round-match-hyd-g-1"] = _db_round_doc(
        "round-match-hyd-g-1", "match-hyd-g"
    )

    body = _give_up(client, auth_headers, "match-hyd-g", PLAYER_A).json()
    # The hydrated doc has no player_last_seen, so the opponent counts as
    # connected and the give-up waits instead of auto-tying.
    assert body == {"status": "gave_up", "waiting_for_opponent": True}
    assert main.in_memory_rounds["round-match-hyd-g-1"]["player1_gave_up"] is True


def test_membership_still_enforced_on_hydrated_match(
    client, fake_matches_db, auth_headers
):
    fake_matches_db["match-hyd-403"] = _db_match_doc("match-hyd-403")

    assert _status(client, auth_headers, "match-hyd-403", OUTSIDER).status_code == 403
    assert (
        _question(client, auth_headers, "match-hyd-403", OUTSIDER).status_code == 403
    )
    assert _answer(client, auth_headers, "match-hyd-403", OUTSIDER).status_code == 403
    assert _give_up(client, auth_headers, "match-hyd-403", OUTSIDER).status_code == 403


def test_current_behavior_memory_wipe_restarts_round_numbering(
    client, fake_matches_db, fake_rounds_db, auth_headers, fixed_question
):
    # BUG pin (see xfail below): after a memory wipe, get_question hydrates
    # the match doc but never hydrates its current round.  Round counting is
    # done over in_memory_rounds only, so the "next" round is numbered 1
    # again, reusing the historical round-…-1 id.  Because the id already
    # exists in the DB the insert is skipped, leaving memory (new question)
    # and Mongo (old question) permanently diverged for that round id.
    fake_matches_db["match-mw"] = _db_match_doc(
        "match-mw",
        player1_score=1,
        player2_score=1,
        current_round_id="round-match-mw-3",
    )
    fake_rounds_db["round-match-mw-1"] = _db_round_doc(
        "round-match-mw-1",
        "match-mw",
        question="OLD·x^9",
        answer="9·x^8",
        winner_id=PLAYER_B,
    )

    body = _question(client, auth_headers, "match-mw", PLAYER_A).json()
    assert body["round_id"] == "round-match-mw-1"
    # Memory got the freshly generated question…
    assert main.in_memory_rounds["round-match-mw-1"]["question"] == "x^2"
    # …while the DB still holds the historical round under the same id.
    assert fake_rounds_db["round-match-mw-1"]["question"] == "OLD·x^9"
    # Scores survive the wipe (they live on the match doc).
    match = main.in_memory_matches["match-mw"]
    assert (match["player1_score"], match["player2_score"]) == (1, 1)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(round-id-replay): after a memory wipe, _create_next_round counts "
        "rounds from in_memory_rounds only and ignores both the match doc's "
        "current_round_id and the rounds already persisted in Mongo, so it "
        "re-issues historical round ids (round-…-1) and the round history "
        "diverges between memory and the DB."
    ),
)
def test_rehydrated_match_should_not_reuse_historical_round_ids(
    client, fake_matches_db, fake_rounds_db, auth_headers, fixed_question
):
    fake_matches_db["match-mw2"] = _db_match_doc(
        "match-mw2",
        player1_score=1,
        player2_score=1,
        current_round_id="round-match-mw2-3",
    )
    fake_rounds_db["round-match-mw2-1"] = _db_round_doc(
        "round-match-mw2-1", "match-mw2", winner_id=PLAYER_B
    )

    body = _question(client, auth_headers, "match-mw2", PLAYER_A).json()
    assert body["round_id"] not in fake_rounds_db


# ---------------------------------------------------------------------------
# match_counter restart collision
# ---------------------------------------------------------------------------


def test_current_behavior_counter_restart_reuses_live_match_id(
    client, auth_headers
):
    # BUG pin (see xfail below): ranked ids are match-{counter} with a
    # process-local counter.  After a restart the counter resets and the next
    # ranked match reuses "match-1", silently overwriting the still-live
    # in-memory match (and, via the update-instead-of-insert branch, the
    # persisted doc too).  The original players are locked out of their own
    # match id with a 403.
    first_id = _ranked_match(client, auth_headers, PLAYER_A, PLAYER_B)
    assert first_id == "match-1"

    main.match_counter = 0  # simulated process restart

    second_id = _ranked_match(client, auth_headers, PLAYER_C, PLAYER_D)
    assert second_id == first_id  # same id, different match!

    match = main.in_memory_matches["match-1"]
    assert {str(match["player1_id"]), str(match["player2_id"])} == {
        PLAYER_C,
        PLAYER_D,
    }
    # Player A's own match id now answers 403 "Not your match".
    assert _status(client, auth_headers, "match-1", PLAYER_A).status_code == 403


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(counter-restart-collision): match ids are match-{process_counter}, "
        "so a restart (counter back to 0) re-issues ids of matches that still "
        "exist, overwriting them in memory and in Mongo (the insert path "
        "detects the duplicate and updates instead)."
    ),
)
def test_match_ids_should_survive_counter_restart(client, auth_headers):
    first_id = _ranked_match(client, auth_headers, PLAYER_A, PLAYER_B)
    main.match_counter = 0  # simulated process restart
    second_id = _ranked_match(client, auth_headers, PLAYER_C, PLAYER_D)
    assert second_id != first_id


# ---------------------------------------------------------------------------
# player_last_seen loss after a memory wipe mid-match
# ---------------------------------------------------------------------------


def _wipe_to_db(fake_matches_db, match_id):
    """Evict the match and register the Mongo-shaped doc (no presence map)."""
    doc = copy.deepcopy(main.in_memory_matches.pop(match_id))
    doc.pop("player_last_seen", None)  # presence is never persisted
    fake_matches_db[match_id] = doc


def test_presence_history_is_lost_after_memory_wipe(
    client, fake_matches_db, auth_headers
):
    match_id = _friend_match(client, auth_headers)
    _status(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)

    # B walks away long ago: A sees them disconnected…
    main.in_memory_matches[match_id]["player_last_seen"][PLAYER_B] = (
        main.utc_now() - timedelta(seconds=999)
    )
    before = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert before["opponent_connected"] is False

    # …until a memory wipe: the rehydrated doc has no player_last_seen, and
    # never-seen players count as connected, so the long-gone opponent flips
    # back to "connected".
    _wipe_to_db(fake_matches_db, match_id)
    after = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert after["opponent_connected"] is True


def test_stale_opponent_give_up_autotie_lost_after_memory_wipe(
    client, fake_matches_db, auth_headers, fixed_question
):
    # Before a wipe, giving up against a >12s-stale opponent auto-ties the
    # round (covered in the presence test file).  After the wipe the same
    # give-up waits forever, because the staleness evidence was memory-only.
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()["round_id"]
    main.in_memory_matches[match_id]["player_last_seen"][PLAYER_B] = (
        main.utc_now() - timedelta(seconds=999)
    )

    _wipe_to_db(fake_matches_db, match_id)

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body == {"status": "gave_up", "waiting_for_opponent": True}
    assert main.in_memory_rounds[round_id]["winner_id"] is None
