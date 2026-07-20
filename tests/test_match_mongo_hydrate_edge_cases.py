"""
Mongo hydrate / fallback edge cases for people-vs-people matches.

conftest's default mocks answer None to every find_one, so most suites only
exercise the pure in-memory paths.  This suite backs matches_collection and
rounds_collection with small stateful fakes (FakeMatchesDB / FakeRoundsDB,
same pattern as the challenge and history suites) so the DB can actually
RETURN documents, and targets the hydrate branches:

1.  /api/game/question hydrates the match doc from the DB on a memory miss
2.  /api/game/answer hydrates the round doc from the DB
3.  /api/game/give-up hydrates the round doc from the DB
4.  /api/game/status/{id} does NOT hydrate the round (xfail re-pin of
    BUG(status-no-round-hydration))
5.  /api/game/friend/join hydrates the match from the DB (but never caches)
6.  /api/game/friend/status/{code} hydrates from the DB (never caches)
7.  /api/game/match/{code} does NOT hydrate at all (xfail re-pin of
    BUG(by-code-no-db-fallback))
8.  /api/game/active scans memory only, zero DB reads
9.  corrupted DB docs missing load-bearing fields -> 500s (and what state
    they leave behind)
10. DB docs with extra unexpected fields are served and preserved
11. once hydrated, memory takes precedence and the DB is never re-read
12. update_one write-backs after a hydrate land in the DB doc

Conventions (same as the sibling edge-case files):
- Guest identities via "Bearer guest-xxx" tokens.
- Known bugs are documented with strict xfail markers plus sibling tests
  that pin the CURRENT behavior.  See MATCH_EDGE_CASE_REPORT.md.
"""

import copy
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import main


PLAYER_A = "guest-hyd-aaa"
PLAYER_B = "guest-hyd-bbb"
OUTSIDER = "guest-hyd-outsider"

CORRECT = "2*x"  # matches fixed_question's answer "2·x"
WRONG = "999"

STATUS_RESPONSE_KEYS = {
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
# FakeMatchesDB / FakeRoundsDB: stateful Mongo stand-ins
# ---------------------------------------------------------------------------


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


class _UpdateResult:
    def __init__(self, matched):
        self.matched_count = matched
        self.modified_count = matched
        self.upserted_id = None


class FakeMatchesDB:
    """
    Stateful stand-in for the Mongo matches collection.  Supports the query
    shapes main.py issues against it: flat equality filters plus the
    positional "rounds.round_number" filter, and $set (including the
    "rounds.$." positional operator) / $push updates.  Documents are
    deep-copied at the driver boundary, like Motor.  find_one calls are
    counted so tests can assert whether a route touched the DB at all.
    """

    def __init__(self):
        self.docs = {}
        self.find_one_calls = 0

    @staticmethod
    def _matches(doc, query):
        """Mongo-style filter check.  Returns (matched, positional_index)."""
        pos = None
        for key, expected in (query or {}).items():
            if key == "rounds.round_number":
                idx = next(
                    (
                        i
                        for i, r in enumerate(doc.get("rounds", []))
                        if r.get("round_number") == expected
                    ),
                    None,
                )
                if idx is None:
                    return False, None
                pos = idx
            elif doc.get(key) != expected:
                return False, None
        return True, pos

    async def insert_one(self, doc, *args, **kwargs):
        self.docs[doc["_id"]] = copy.deepcopy(doc)

    async def find_one(self, query, *args, **kwargs):
        self.find_one_calls += 1
        for doc in self.docs.values():
            matched, _ = self._matches(doc, query)
            if matched:
                return copy.deepcopy(doc)
        return None

    def find(self, query=None, *args, **kwargs):
        hits = [
            copy.deepcopy(d)
            for d in self.docs.values()
            if self._matches(d, query)[0]
        ]
        return _FakeCursor(hits)

    async def update_one(self, query, update, *args, **kwargs):
        for doc in self.docs.values():
            matched, pos = self._matches(doc, query)
            if not matched:
                continue
            for op, fields in update.items():
                if op == "$set":
                    for key, value in fields.items():
                        if key.startswith("rounds.$."):
                            field = key[len("rounds.$."):]
                            doc["rounds"][pos][field] = copy.deepcopy(value)
                        else:
                            doc[key] = copy.deepcopy(value)
                elif op == "$push":
                    for key, value in fields.items():
                        doc.setdefault(key, []).append(copy.deepcopy(value))
            return _UpdateResult(1)
        return _UpdateResult(0)

    async def delete_one(self, query, *args, **kwargs):
        for doc_id, doc in list(self.docs.items()):
            if self._matches(doc, query)[0]:
                del self.docs[doc_id]
                break
        return None


class FakeRoundsDB(FakeMatchesDB):
    """Same semantics; round docs only ever see flat {"_id": ...} queries."""


@pytest.fixture
def fake_matches_db(mock_mongo, monkeypatch):
    db = FakeMatchesDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.matches_collection, method, getattr(db, method))
    return db


@pytest.fixture
def fake_rounds_db(mock_mongo, monkeypatch):
    db = FakeRoundsDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.rounds_collection, method, getattr(db, method))
    return db


@pytest.fixture
def client_no_reraise(mock_mongo):
    """Client that returns the handler's 500 instead of re-raising in-test."""
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Mongo-shaped document builders (naive datetimes, no player_last_seen)
# ---------------------------------------------------------------------------


def _db_match_doc(match_id, p1=PLAYER_A, p2=PLAYER_B, **overrides):
    doc = {
        "_id": match_id,
        "match_code": f"HYD{match_id[-3:].upper()}",
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


def _memory_match(match_id, **overrides):
    """Seed a match straight into process memory (not in any DB fake)."""
    doc = _db_match_doc(match_id, **overrides)
    main.in_memory_matches[match_id] = doc
    return doc


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


# ---------------------------------------------------------------------------
# 1. question hydrates the match from the DB
# ---------------------------------------------------------------------------


def test_question_hydrates_match_from_db_when_not_in_memory(
    client, fake_matches_db, fake_rounds_db, auth_headers, fixed_question
):
    fake_matches_db.docs["match-hq1"] = _db_match_doc("match-hq1")
    assert "match-hq1" not in main.in_memory_matches

    response = _question(client, auth_headers, "match-hq1", PLAYER_A)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["round_id"] == "round-match-hq1-1"
    assert body["expression"] == "x^2"

    # Match cached back into memory, first round created in memory + rounds DB.
    assert "match-hq1" in main.in_memory_matches
    assert "round-match-hq1-1" in main.in_memory_rounds
    assert "round-match-hq1-1" in fake_rounds_db.docs

    # The new round is also written back onto the persisted match doc.
    db_match = fake_matches_db.docs["match-hq1"]
    assert db_match["current_round_id"] == "round-match-hq1-1"
    assert len(db_match["rounds"]) == 1
    assert db_match["rounds"][0]["round_number"] == 1


def test_question_on_hydrated_completed_match_is_rejected_but_cached(
    client, fake_matches_db, fake_rounds_db, auth_headers, fixed_question
):
    # Hydration happens BEFORE the status gate: the dead match is pulled into
    # memory just to be rejected, and stays cached there.
    fake_matches_db.docs["match-hq2"] = _db_match_doc(
        "match-hq2", status="completed", winner_id=PLAYER_B
    )

    response = _question(client, auth_headers, "match-hq2", PLAYER_A)
    assert response.status_code == 400
    assert response.json()["detail"] == "Match is already completed"
    assert "match-hq2" in main.in_memory_matches

    answer = _answer(client, auth_headers, "match-hq2", PLAYER_A)
    assert answer.status_code == 400


# ---------------------------------------------------------------------------
# 2. answer hydrates the round from the DB
# ---------------------------------------------------------------------------


def test_answer_hydrates_round_from_db_and_scores(
    client, fake_rounds_db, auth_headers, fixed_question
):
    _memory_match("match-ha1", current_round_id="round-match-ha1-1")
    fake_rounds_db.docs["round-match-ha1-1"] = _db_round_doc(
        "round-match-ha1-1", "match-ha1"
    )
    assert "round-match-ha1-1" not in main.in_memory_rounds

    body = _answer(client, auth_headers, "match-ha1", PLAYER_A).json()
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER_A
    assert body["player1_score"] == 1

    # The round was pulled into memory and resolved there.
    assert main.in_memory_rounds["round-match-ha1-1"]["winner_id"] == PLAYER_A
    assert main.in_memory_matches["match-ha1"]["player1_score"] == 1


# ---------------------------------------------------------------------------
# 3. give-up hydrates the round from the DB
# ---------------------------------------------------------------------------


def test_give_up_hydrates_round_from_db_and_writes_flag_back(
    client, fake_rounds_db, auth_headers
):
    _memory_match("match-hg1", current_round_id="round-match-hg1-1")
    fake_rounds_db.docs["round-match-hg1-1"] = _db_round_doc(
        "round-match-hg1-1", "match-hg1"
    )

    body = _give_up(client, auth_headers, "match-hg1", PLAYER_A).json()
    # The hydrated doc carries no player_last_seen, so the opponent counts
    # as connected and the give-up waits instead of auto-tying.
    assert body == {"status": "gave_up", "waiting_for_opponent": True}

    # Hydrated into memory AND the flag written back to the DB doc, so a
    # different worker (or a post-restart process) can see it.
    assert main.in_memory_rounds["round-match-hg1-1"]["player1_gave_up"] is True
    assert fake_rounds_db.docs["round-match-hg1-1"]["player1_gave_up"] is True


def test_give_up_on_hydrated_already_resolved_round_short_circuits(
    client, fake_rounds_db, auth_headers
):
    # A round that was resolved before the memory wipe: the hydrate pulls
    # winner_id back and the give-up answers already_ended without mutating.
    _memory_match("match-hg2", current_round_id="round-match-hg2-1")
    fake_rounds_db.docs["round-match-hg2-1"] = _db_round_doc(
        "round-match-hg2-1", "match-hg2", winner_id=PLAYER_B
    )

    body = _give_up(client, auth_headers, "match-hg2", PLAYER_A).json()
    assert body == {"status": "already_ended", "round_winner": PLAYER_B}
    assert main.in_memory_rounds["round-match-hg2-1"]["winner_id"] == PLAYER_B
    assert "player1_gave_up" not in fake_rounds_db.docs["round-match-hg2-1"]


# ---------------------------------------------------------------------------
# 4. status does NOT hydrate the round (known bug, re-pin)
# ---------------------------------------------------------------------------


def _seed_resolved_round_in_db_only(fake_matches_db, fake_rounds_db):
    fake_matches_db.docs["match-hs1"] = _db_match_doc(
        "match-hs1", current_round_id="round-match-hs1-1", player1_score=1
    )
    fake_rounds_db.docs["round-match-hs1-1"] = _db_round_doc(
        "round-match-hs1-1", "match-hs1", winner_id=PLAYER_A
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(status-no-round-hydration): get_game_status hydrates the match "
        "doc but reads the current round only via `in in_memory_rounds`, "
        "with no rounds_collection fallback.  With both docs in Mongo and "
        "neither in memory, the poll reports the match fine but the resolved "
        "round's winner as None, so a polling client never advances."
    ),
)
def test_status_should_hydrate_current_round_from_db(
    client, fake_matches_db, fake_rounds_db, auth_headers
):
    _seed_resolved_round_in_db_only(fake_matches_db, fake_rounds_db)

    body = _status(client, auth_headers, "match-hs1", PLAYER_B).json()
    assert body["round_winner"] == PLAYER_A


def test_current_behavior_status_is_blind_to_db_only_round(
    client, fake_matches_db, fake_rounds_db, auth_headers
):
    # BUG pin for the xfail above: the match half hydrates, the round half
    # is never even queried.
    _seed_resolved_round_in_db_only(fake_matches_db, fake_rounds_db)

    body = _status(client, auth_headers, "match-hs1", PLAYER_B).json()
    assert body["player1_score"] == 1  # match doc hydrated fine
    assert body["round_winner"] is None  # round result invisible
    assert body["player1_gave_up"] is False
    assert body["player2_gave_up"] is False

    assert "round-match-hs1-1" not in main.in_memory_rounds
    assert fake_rounds_db.find_one_calls == 0  # rounds DB never touched


# ---------------------------------------------------------------------------
# 5. friend join hydrates the match from the DB
# ---------------------------------------------------------------------------


def test_friend_join_hydrates_waiting_match_from_db(
    client, fake_matches_db, auth_headers
):
    fake_matches_db.docs["match-hj1"] = _db_match_doc(
        "match-hj1", p2=None, status="waiting", match_code="HYDJN1"
    )

    response = client.post(
        "/api/game/friend/join",
        json={"match_code": "hydjn1"},  # lowercase input is uppercased
        headers=auth_headers(PLAYER_B),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"match_id": "match-hj1", "status": "active"}

    # The join is written back to the DB doc...
    db_doc = fake_matches_db.docs["match-hj1"]
    assert db_doc["player2_id"] == PLAYER_B
    assert db_doc["status"] == "active"
    # ...but the match is NOT cached into memory: join only updates memory
    # when the match was already there.
    assert "match-hj1" not in main.in_memory_matches


def test_join_then_question_completes_a_db_only_friend_flow(
    client, fake_matches_db, fake_rounds_db, auth_headers, fixed_question
):
    # End to end with the match living only in Mongo: join hydrates and
    # writes back, then question hydrates the now-active doc and serves
    # round 1 to both players.
    fake_matches_db.docs["match-hj2"] = _db_match_doc(
        "match-hj2", p2=None, status="waiting", match_code="HYDJN2"
    )

    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": "HYDJN2"},
        headers=auth_headers(PLAYER_B),
    )
    assert joined.status_code == 200

    first = _question(client, auth_headers, "match-hj2", PLAYER_A).json()
    second = _question(client, auth_headers, "match-hj2", PLAYER_B).json()
    assert first["round_id"] == second["round_id"] == "round-match-hj2-1"

    body = _answer(client, auth_headers, "match-hj2", PLAYER_B).json()
    assert body["correct"] is True
    assert body["player2_score"] == 1


# ---------------------------------------------------------------------------
# 6. friend status hydrates the match from the DB
# ---------------------------------------------------------------------------


def test_friend_status_hydrates_match_from_db(client, fake_matches_db):
    fake_matches_db.docs["match-hf1"] = _db_match_doc(
        "match-hf1", p2=None, status="waiting", match_code="HYDFS1"
    )

    # No auth required on this route.
    response = client.get("/api/game/friend/status/HYDFS1")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "match_id": "match-hf1",
        "status": "waiting",
        "player1_ready": True,
        "player2_ready": False,
    }
    # Read-only: the DB doc is served but never cached into memory.
    assert "match-hf1" not in main.in_memory_matches


# ---------------------------------------------------------------------------
# 7. by-code does NOT hydrate the match (known bug, re-pin)
# ---------------------------------------------------------------------------


def test_by_code_should_hydrate_match_from_db(
    client, fake_matches_db, auth_headers
):
    fake_matches_db.docs["match-hc1"] = _db_match_doc(
        "match-hc1", match_code="HYDBC1"
    )

    response = client.get("/api/game/match/HYDBC1", headers=auth_headers(PLAYER_A))
    assert response.status_code == 200
    assert response.json()["match_id"] == "match-hc1"


def test_current_behavior_by_code_never_queries_the_db(
    client, fake_matches_db, auth_headers
):
    # BUG pin for the xfail above: not only does the lookup 404, the route
    # performs ZERO reads against matches_collection.
    fake_matches_db.docs["match-hc2"] = _db_match_doc(
        "match-hc2", match_code="HYDBC2"
    )

    response = client.get("/api/game/match/HYDBC2", headers=auth_headers(PLAYER_A))
    assert response.status_code == 404
    assert fake_matches_db.find_one_calls == 0


# ---------------------------------------------------------------------------
# 8. active-match scan is memory only
# ---------------------------------------------------------------------------


def test_active_match_scan_is_memory_only_until_another_route_hydrates(
    client, fake_matches_db, auth_headers
):
    fake_matches_db.docs["match-hm1"] = _db_match_doc("match-hm1")

    # The DB holds an active match for A, but /api/game/active scans
    # in_memory_matches only and never issues a DB read.
    before = client.get("/api/game/active", headers=auth_headers(PLAYER_A))
    assert before.json() == {"has_active_match": False}
    assert fake_matches_db.find_one_calls == 0

    # One status poll hydrates the match, and the active scan flips.
    assert _status(client, auth_headers, "match-hm1", PLAYER_A).status_code == 200
    after = client.get("/api/game/active", headers=auth_headers(PLAYER_A)).json()
    assert after["has_active_match"] is True
    assert after["match_id"] == "match-hm1"


# ---------------------------------------------------------------------------
# 9. corrupted DB docs missing fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_field", ["player2_id", "player1_elo"])
def test_question_500s_when_hydrated_match_doc_is_corrupted(
    client_no_reraise,
    fake_matches_db,
    fake_rounds_db,
    auth_headers,
    fixed_question,
    missing_field,
):
    doc = _db_match_doc("match-hx1")
    del doc[missing_field]
    fake_matches_db.docs["match-hx1"] = doc

    response = _question(client_no_reraise, auth_headers, "match-hx1", PLAYER_A)
    assert response.status_code == 500

    # The corrupted doc was cached into memory BEFORE the crash, so every
    # later request keeps hitting the same broken doc without re-reading
    # the DB.
    assert "match-hx1" in main.in_memory_matches
    assert missing_field not in main.in_memory_matches["match-hx1"]


def test_question_500_on_missing_scores_leaves_half_created_round_behind(
    client_no_reraise, fake_matches_db, fake_rounds_db, auth_headers, fixed_question
):
    # player1_score is only read AFTER the round doc was already stored in
    # memory and inserted into the rounds collection, so the 500 strands a
    # fully created round whose match doc never learned about it.
    doc = _db_match_doc("match-hx2")
    del doc["player1_score"]
    fake_matches_db.docs["match-hx2"] = doc

    response = _question(client_no_reraise, auth_headers, "match-hx2", PLAYER_A)
    assert response.status_code == 500

    assert "round-match-hx2-1" in main.in_memory_rounds  # orphan in memory
    assert "round-match-hx2-1" in fake_rounds_db.docs  # orphan persisted
    # The write-back that records the round on the match doc never ran.
    assert "rounds" not in fake_matches_db.docs["match-hx2"]


@pytest.mark.parametrize("missing_field", ["player1_score", "status"])
def test_status_500s_and_still_caches_the_corrupted_match_doc(
    client_no_reraise, fake_matches_db, auth_headers, missing_field
):
    doc = _db_match_doc("match-hx3")
    del doc[missing_field]
    fake_matches_db.docs["match-hx3"] = doc

    response = _status(client_no_reraise, auth_headers, "match-hx3", PLAYER_A)
    assert response.status_code == 500
    assert "match-hx3" in main.in_memory_matches


def test_answer_500s_when_hydrated_round_doc_is_missing_the_answer(
    client_no_reraise, fake_rounds_db, auth_headers
):
    _memory_match("match-hx4", current_round_id="round-match-hx4-1")
    round_doc = _db_round_doc("round-match-hx4-1", "match-hx4")
    del round_doc["answer"]
    fake_rounds_db.docs["round-match-hx4-1"] = round_doc

    response = _answer(client_no_reraise, auth_headers, "match-hx4", PLAYER_A)
    assert response.status_code == 500
    # The broken round doc is cached too.
    assert "round-match-hx4-1" in main.in_memory_rounds


def test_status_tolerates_missing_optional_match_fields(
    client, fake_matches_db, auth_headers
):
    # Status only truly needs identity, scores and status; everything else
    # is read with .get and defaults cleanly.
    fake_matches_db.docs["match-hx5"] = {
        "_id": "match-hx5",
        "player1_id": PLAYER_A,
        "player2_id": PLAYER_B,
        "player1_score": 0,
        "player2_score": 0,
        "status": "active",
    }

    body = _status(client, auth_headers, "match-hx5", PLAYER_A).json()
    assert body["status"] == "active"
    assert body["winner_id"] is None
    assert body["elo_change"] == 0
    assert body["round_winner"] is None
    assert body["round_start_time"] is None


# ---------------------------------------------------------------------------
# 10. DB docs with extra unexpected fields
# ---------------------------------------------------------------------------


def test_hydrated_match_doc_with_extra_fields_is_served_and_preserved(
    client, fake_matches_db, auth_headers
):
    fake_matches_db.docs["match-he1"] = _db_match_doc(
        "match-he1",
        legacy_blob={"schema": 1, "flags": ["beta"]},
        region="eu-west",
        player3_id="guest-hyd-ghost",
    )

    response = _status(client, auth_headers, "match-he1", PLAYER_A)
    assert response.status_code == 200
    # The response shape is unaffected: no extra keys leak out.
    assert set(response.json()) == STATUS_RESPONSE_KEYS

    # The extras ride along into the memory cache untouched.
    cached = main.in_memory_matches["match-he1"]
    assert cached["legacy_blob"] == {"schema": 1, "flags": ["beta"]}
    assert cached["region"] == "eu-west"
    assert cached["player3_id"] == "guest-hyd-ghost"


def test_hydrated_round_doc_with_extra_fields_scores_normally(
    client, fake_rounds_db, auth_headers
):
    _memory_match("match-he2", current_round_id="round-match-he2-1")
    fake_rounds_db.docs["round-match-he2-1"] = _db_round_doc(
        "round-match-he2-1",
        "match-he2",
        replay_blob=[1, 2, 3],
        shard=7,
    )

    body = _answer(client, auth_headers, "match-he2", PLAYER_A).json()
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER_A

    cached = main.in_memory_rounds["round-match-he2-1"]
    assert cached["replay_blob"] == [1, 2, 3]
    assert cached["shard"] == 7


# ---------------------------------------------------------------------------
# 11. once hydrated, memory takes precedence over the DB
# ---------------------------------------------------------------------------


def test_memory_takes_precedence_over_db_after_hydrate(
    client, fake_matches_db, auth_headers
):
    fake_matches_db.docs["match-hp1"] = _db_match_doc("match-hp1")

    first = _status(client, auth_headers, "match-hp1", PLAYER_A).json()
    assert first["player1_score"] == 0
    assert fake_matches_db.find_one_calls == 1  # the hydrate read

    # Memory and DB now diverge: the poll serves memory and never re-reads.
    main.in_memory_matches["match-hp1"]["player1_score"] = 2
    fake_matches_db.docs["match-hp1"]["player1_score"] = 5

    second = _status(client, auth_headers, "match-hp1", PLAYER_A).json()
    assert second["player1_score"] == 2  # memory wins
    assert fake_matches_db.find_one_calls == 1  # no second DB read


# ---------------------------------------------------------------------------
# 12. update_one after a hydrate writes back to the DB
# ---------------------------------------------------------------------------


def test_correct_answer_after_full_hydrate_writes_back_to_both_collections(
    client, fake_matches_db, fake_rounds_db, auth_headers
):
    # Neither doc in memory; the match doc carries the rounds array shape a
    # real match accumulates, so the positional rounds.$ update applies.
    fake_matches_db.docs["match-hw1"] = _db_match_doc(
        "match-hw1",
        current_round_id="round-match-hw1-1",
        rounds=[
            {
                "round_number": 1,
                "question": "x^2",
                "winner": None,
                "player1_answer": None,
                "player2_answer": None,
            }
        ],
    )
    fake_rounds_db.docs["round-match-hw1-1"] = _db_round_doc(
        "round-match-hw1-1", "match-hw1"
    )

    body = _answer(client, auth_headers, "match-hw1", PLAYER_A).json()
    assert body["correct"] is True
    assert body["player1_score"] == 1

    # Rounds collection: winner and answer written back.
    db_round = fake_rounds_db.docs["round-match-hw1-1"]
    assert db_round["winner_id"] == PLAYER_A
    assert db_round["player1_answer"] == CORRECT

    # Matches collection: score and the positional rounds-array entry too.
    db_match = fake_matches_db.docs["match-hw1"]
    assert db_match["player1_score"] == 1
    assert db_match["rounds"][0]["winner"] == "player1"
    assert db_match["rounds"][0]["player1_answer"] == CORRECT


def test_wrong_answer_after_hydrate_writes_attempt_back_to_db(
    client, fake_rounds_db, auth_headers
):
    _memory_match("match-hw2", current_round_id="round-match-hw2-1")
    fake_rounds_db.docs["round-match-hw2-1"] = _db_round_doc(
        "round-match-hw2-1", "match-hw2"
    )

    body = _answer(client, auth_headers, "match-hw2", PLAYER_A, answer=WRONG).json()
    assert body["correct"] is False
    assert body["round_winner"] is None

    # The failed attempt is durably recorded; the round stays open.
    db_round = fake_rounds_db.docs["round-match-hw2-1"]
    assert db_round["player1_answer"] == WRONG
    assert db_round["winner_id"] is None


def test_membership_is_enforced_before_any_write_back(
    client, fake_matches_db, fake_rounds_db, auth_headers
):
    # An outsider probing a DB-only match gets 403 on every hydrate route
    # and nothing is written back to either collection.
    fake_matches_db.docs["match-hw3"] = _db_match_doc(
        "match-hw3", current_round_id="round-match-hw3-1"
    )
    fake_rounds_db.docs["round-match-hw3-1"] = _db_round_doc(
        "round-match-hw3-1", "match-hw3"
    )
    before_round = copy.deepcopy(fake_rounds_db.docs["round-match-hw3-1"])

    assert _status(client, auth_headers, "match-hw3", OUTSIDER).status_code == 403
    assert _question(client, auth_headers, "match-hw3", OUTSIDER).status_code == 403
    assert _answer(client, auth_headers, "match-hw3", OUTSIDER).status_code == 403
    assert _give_up(client, auth_headers, "match-hw3", OUTSIDER).status_code == 403

    assert fake_rounds_db.docs["round-match-hw3-1"] == before_round
    assert "rounds" not in fake_matches_db.docs["match-hw3"]
