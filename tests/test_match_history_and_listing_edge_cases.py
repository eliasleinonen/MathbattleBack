"""
Match listing & history edge cases for people matches.

Targets the read-side endpoints that expose match history:
- /matches/all            (the only match-listing/"history" endpoint)
- /match/{id}/details     (per-match history: winner, elo_change, rounds)
- /api/leaderboard        (only where it brushes against match results)

Covers:
1.  /matches/all without auth returning everyone's matches (xfail + pin)
2.  empty history (empty DB, DB-only reads ignoring memory)
3.  limit (50) / sort (created_at desc) behavior
4.  details of completed matches: winner / elo_change / score
5.  details of an active match leaking the open round's answer (oracle re-pin)
6.  details for nonexistent matches + the memory-fallback rounds blackout
7.  listings after many sequential matches
8.  friend vs ranked (vs bot) matches in listings/details
9.  abandoned matches in listings (abandonment is never persisted)
10. the match-doc rounds array numbering after tie rounds

Unlike the sibling suites, matches_collection is backed here by a small
Mongo-semantics emulator (insert_one/find_one/find with real sort+limit,
update_one incl. the positional `rounds.$` operator, deepcopies at the
driver boundary).  The listing endpoints read ONLY from the DB, so they see
exactly what real Mongo would hold after the $push/$set/positional updates
ran - which is what surfaces the post-tie persistence bugs below.

Conventions (same as the sibling edge-case files):
- Guest identities via "Bearer guest-xxx" tokens.
- Known bugs are documented with strict xfail markers plus sibling tests
  that pin the CURRENT behavior.  See MATCH_EDGE_CASE_REPORT.md.
"""

import copy
from datetime import datetime, timedelta

import pytest

import main


PLAYER_A = "guest-hist-aaa"
PLAYER_B = "guest-hist-bbb"
PLAYER_C = "guest-hist-ccc"
PLAYER_D = "guest-hist-ddd"
OUTSIDER = "guest-hist-outsider"

LISTING_KEYS = {
    "match_id",
    "player1",
    "player2",
    "score",
    "status",
    "rounds_count",
    "created_at",
}

DETAIL_KEYS = {
    "match_id",
    "match_code",
    "match_type",
    "player1",
    "player2",
    "score",
    "status",
    "winner",
    "elo_change",
    "rounds",
    "created_at",
    "updated_at",
}


# ---------------------------------------------------------------------------
# Mongo-semantics emulator for matches_collection
# ---------------------------------------------------------------------------


class _EmulatedCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, field, direction=1):
        self._docs = sorted(
            self._docs, key=lambda d: d.get(field), reverse=(direction == -1)
        )
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        docs = [copy.deepcopy(d) for d in self._docs]
        return docs if length is None else docs[:length]


def _doc_matches(doc, query):
    """Mongo-style filter check. Returns (matched, positional_index)."""
    pos = None
    for key, expected in query.items():
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


def _apply_update(doc, update, pos):
    for op, fields in update.items():
        if op == "$set":
            for key, value in fields.items():
                if key.startswith("rounds.$."):
                    doc["rounds"][pos][key[len("rounds.$."):]] = copy.deepcopy(value)
                elif "." in key:
                    raise NotImplementedError(f"dotted $set key {key!r}")
                else:
                    doc[key] = copy.deepcopy(value)
        elif op == "$push":
            for key, value in fields.items():
                doc.setdefault(key, []).append(copy.deepcopy(value))
        else:
            raise NotImplementedError(f"update operator {op!r}")


class _UpdateResult:
    def __init__(self, matched):
        self.matched_count = matched
        self.modified_count = matched
        self.upserted_id = None


@pytest.fixture
def mongo_matches(mock_mongo, monkeypatch):
    """Back matches_collection with an in-process Mongo emulation.

    Deepcopies at every boundary, like a real driver: the in-memory match
    dicts and the "database" docs share no state.
    """
    docs = {}

    async def insert_one(doc, *args, **kwargs):
        docs[doc["_id"]] = copy.deepcopy(doc)

    async def find_one(query, *args, **kwargs):
        for doc in docs.values():
            matched, _ = _doc_matches(doc, query)
            if matched:
                return copy.deepcopy(doc)
        return None

    def find(query=None, *args, **kwargs):
        selected = [d for d in docs.values() if _doc_matches(d, query or {})[0]]
        return _EmulatedCursor(selected)

    async def update_one(query, update, *args, **kwargs):
        for doc in docs.values():
            matched, pos = _doc_matches(doc, query)
            if matched:
                _apply_update(doc, update, pos)
                return _UpdateResult(1)
        return _UpdateResult(0)

    monkeypatch.setattr(main.matches_collection, "insert_one", insert_one)
    monkeypatch.setattr(main.matches_collection, "find_one", find_one)
    monkeypatch.setattr(main.matches_collection, "find", find)
    monkeypatch.setattr(main.matches_collection, "update_one", update_one)
    return docs


def _seed_doc(mongo_matches, match_id, created_at, status="completed",
              p1=PLAYER_A, p2=PLAYER_B, score=(1, 0), rounds=None,
              match_type="friend"):
    doc = {
        "_id": match_id,
        "match_code": f"CODE-{match_id}",
        "player1_id": p1,
        "player2_id": p2,
        "player1_score": score[0],
        "player2_score": score[1],
        "player1_elo": 1000,
        "player2_elo": 1000,
        "status": status,
        "winner_id": None,
        "elo_change": 0,
        "created_at": created_at,
        "rounds": rounds or [],
    }
    if match_type is not None:
        doc["match_type"] = match_type
    mongo_matches[match_id] = doc
    return doc


def _space_created_at(mongo_matches, ordered_ids):
    """Give the listed matches strictly increasing created_at (oldest first)
    so the endpoint's created_at sort is deterministic to assert on."""
    base = datetime.utcnow() - timedelta(hours=1)
    for i, match_id in enumerate(ordered_ids):
        mongo_matches[match_id]["created_at"] = base + timedelta(minutes=i)


# ---------------------------------------------------------------------------
# gameplay helpers (same shapes as the sibling suites)
# ---------------------------------------------------------------------------


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
    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": created.json()["match_code"]},
        headers=auth_headers(player2),
    )
    assert joined.status_code == 200, joined.text
    return created.json()["match_id"]


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


def _status(client, auth_headers, match_id, player):
    return client.get(f"/api/game/status/{match_id}", headers=auth_headers(player))


def _win_round(client, auth_headers, match_id, player):
    q = _question(client, auth_headers, match_id, player)
    assert q.status_code == 200, q.text
    body = _answer(client, auth_headers, match_id, player).json()
    assert body["correct"] is True, body
    return body


def _tie_current_round(client, auth_headers, match_id, p1=PLAYER_A, p2=PLAYER_B):
    first = _give_up(client, auth_headers, match_id, p1).json()
    assert first["status"] == "gave_up", first
    second = _give_up(client, auth_headers, match_id, p2).json()
    assert second["status"] == "both_gave_up", second


def _listing(client, headers=None):
    response = client.get("/matches/all", headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def _details(client, match_id, headers=None):
    return client.get(f"/match/{match_id}/details", headers=headers or {})


# ---------------------------------------------------------------------------
# 1. /matches/all without auth returns everyone's matches
# ---------------------------------------------------------------------------


def test_current_behavior_matches_all_serves_full_history_with_no_auth(
    client, mongo_matches, auth_headers, fixed_question
):
    # BUG pin for the xfail below (bug 21/26 family): no Authorization header
    # at all still gets the last 50 matches of ALL players, scores included.
    friend_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, friend_id, PLAYER_A)
    ranked_id = _ranked_match(client, auth_headers, PLAYER_C, PLAYER_D)

    listing = {entry["match_id"]: entry for entry in _listing(client)}
    assert set(listing) == {friend_id, ranked_id}
    assert listing[friend_id]["score"] == "1-0"
    assert listing[ranked_id]["status"] == "active"


def test_current_behavior_any_guest_reads_other_pairs_history(
    client, mongo_matches, auth_headers, fixed_question
):
    friend_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, friend_id, PLAYER_B)

    listing = _listing(client, auth_headers(OUTSIDER))
    assert [entry["match_id"] for entry in listing] == [friend_id]
    assert listing[0]["score"] == "0-1"  # a stranger's live score, readable


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(matches-all-open-history): /matches/all is a debugging endpoint "
        "left in production with no authentication and no ownership filter - "
        "get_current_user admits headerless callers as the shared guest, so "
        "anyone on the internet can enumerate the last 50 matches of all "
        "players.  Same open-access family as bugs 21/26."
    ),
)
def test_matches_all_should_require_credentials(client, mongo_matches, auth_headers):
    _friend_match(client, auth_headers)
    assert client.get("/matches/all").status_code == 401


# ---------------------------------------------------------------------------
# 2. empty history
# ---------------------------------------------------------------------------


def test_matches_all_empty_history_returns_empty_list(
    client, mongo_matches, auth_headers
):
    for headers in ({}, auth_headers(PLAYER_A)):
        response = client.get("/matches/all", headers=headers)
        assert response.status_code == 200
        assert response.json() == []


def test_matches_all_is_db_only_so_memory_matches_are_invisible(
    client, auth_headers
):
    # Quirk pin: the listing never falls back to in_memory_matches.  With the
    # DB down (default no-op mocks) a live, playable match simply isn't in
    # anyone's history - the exact inverse of the details endpoint, which
    # DOES fall back to memory.
    match_id = _friend_match(client, auth_headers)
    assert match_id in main.in_memory_matches
    assert _listing(client, auth_headers(PLAYER_A)) == []


def test_empty_leaderboard_is_empty_list_not_error(client):
    response = client.get("/api/leaderboard")
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# 3. limit / sort behavior
# ---------------------------------------------------------------------------


def test_matches_all_caps_at_50_newest_and_drops_the_oldest(
    client, mongo_matches, auth_headers
):
    base = datetime.utcnow() - timedelta(days=1)
    for i in range(55):
        _seed_doc(
            mongo_matches, f"seed-{i:02d}", created_at=base + timedelta(minutes=i)
        )

    listing = _listing(client, auth_headers(PLAYER_A))
    assert len(listing) == 50
    # Newest first; the five oldest fell off the end with no paging.
    assert [e["match_id"] for e in listing] == [
        f"seed-{i:02d}" for i in range(54, 4, -1)
    ]


def test_matches_all_orders_newest_first_across_match_types(
    client, mongo_matches, auth_headers
):
    first = _friend_match(client, auth_headers)
    second = _ranked_match(client, auth_headers, PLAYER_C, PLAYER_D)
    third = _friend_match(client, auth_headers, PLAYER_A, PLAYER_C)
    _space_created_at(mongo_matches, [first, second, third])

    ids = [e["match_id"] for e in _listing(client, auth_headers(PLAYER_A))]
    assert ids == [third, second, first]


def test_matches_all_passes_string_created_at_through_verbatim(
    client, mongo_matches, auth_headers
):
    # Datetimes are isoformat()ed; a legacy string created_at is passed as-is.
    _seed_doc(mongo_matches, "seed-str", created_at="2024-01-01T00:00:00")
    entry = _listing(client, auth_headers(PLAYER_A))[0]
    assert entry["created_at"] == "2024-01-01T00:00:00"


def test_matches_all_serializes_datetime_created_at_to_isoformat(
    client, mongo_matches, auth_headers
):
    created = datetime(2024, 6, 1, 12, 30, 45)
    _seed_doc(mongo_matches, "seed-dt", created_at=created)
    entry = _listing(client, auth_headers(PLAYER_A))[0]
    assert entry["created_at"] == created.isoformat()


# ---------------------------------------------------------------------------
# 4. details for completed matches: winner / elo_change / score
# ---------------------------------------------------------------------------


def test_details_completed_ranked_match_reports_winner_score_and_elo(
    client, mongo_matches, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, PLAYER_A)

    body = _details(client, match_id, auth_headers(PLAYER_A)).json()
    assert set(body.keys()) == DETAIL_KEYS
    assert body["status"] == "completed"
    assert body["winner"] == PLAYER_A
    assert body["elo_change"] == main.calculate_elo_change(1000, 1000) == 20
    assert body["score"] == "3-0"
    assert body["match_type"] == "ranked"
    assert [r["round_number"] for r in body["rounds"]] == [1, 2, 3]
    assert [r["winner"] for r in body["rounds"]] == ["player1"] * 3
    assert body["rounds"][0]["player1_answer"] == "2*x"


def test_details_completed_friend_match_has_winner_but_zero_elo_change(
    client, mongo_matches, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, PLAYER_B)

    body = _details(client, match_id, auth_headers(PLAYER_B)).json()
    assert body["status"] == "completed"
    assert body["winner"] == PLAYER_B
    assert body["elo_change"] == 0  # friend matches never pay ELO
    assert body["score"] == "0-3"
    assert body["match_type"] == "friend"


def test_matches_all_shows_final_score_and_completed_status(
    client, mongo_matches, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, PLAYER_A)

    entry = _listing(client, auth_headers(PLAYER_A))[0]
    assert entry["match_id"] == match_id
    assert entry["status"] == "completed"
    assert entry["score"] == "3-0"
    assert entry["rounds_count"] == 3


# ---------------------------------------------------------------------------
# 5. details for an active match leaks the open round's answer
# ---------------------------------------------------------------------------


def test_current_behavior_details_expose_open_round_answer_to_players(
    client, mongo_matches, auth_headers, fixed_question
):
    # BUG pin (bug 1 rediscovered from the participant angle): mid-round the
    # persisted rounds array already carries the correct answer, so either
    # player can open the details "history" in a second tab and cheat.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _details(client, match_id, auth_headers(PLAYER_B)).json()
    open_round = body["rounds"][-1]
    assert open_round["winner"] is None  # round is still in play...
    assert open_round["answer"] == "2·x"  # ...yet the answer is served
    assert open_round["derivative"] == "2·x"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(details-answer-oracle): /match/{id}/details embeds the "
        "persisted rounds array verbatim, including the correct answer of "
        "the still-unresolved current round.  Same root cause as "
        "BUG(details-no-authz) in the isolation suite - the history payload "
        "needs the unresolved round's answer/derivative stripped even for "
        "participants."
    ),
)
def test_details_should_not_reveal_unresolved_round_answer(
    client, mongo_matches, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    open_round = _details(client, match_id, auth_headers(PLAYER_A)).json()["rounds"][-1]
    assert "answer" not in open_round


# ---------------------------------------------------------------------------
# 6. details for nonexistent matches + memory-fallback rounds blackout
# ---------------------------------------------------------------------------


def test_details_nonexistent_match_is_404(client, mongo_matches, auth_headers):
    response = _details(client, "match-ghost", auth_headers(PLAYER_A))
    assert response.status_code == 404
    assert response.json()["detail"] == "Match not found"


def test_current_behavior_details_memory_fallback_blanks_round_history(
    client, mongo_matches, auth_headers, fixed_question
):
    # Quirk pin: rounds summaries are $push'ed to Mongo ONLY - the in-memory
    # match doc never accumulates them.  When the DB doc is gone (outage,
    # lost write) the details fallback serves the live score from memory but
    # an empty rounds history for a match that demonstrably played a round.
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    assert len(_details(client, match_id).json()["rounds"]) == 1

    mongo_matches.clear()  # simulate the DB losing the doc
    body = _details(client, match_id, auth_headers(PLAYER_A)).json()
    assert body["score"] == "1-0"  # memory knows the score...
    assert body["rounds"] == []  # ...but the round history vanished


# ---------------------------------------------------------------------------
# 7. listing after many sequential matches
# ---------------------------------------------------------------------------


def test_matches_all_after_many_sequential_matches(
    client, mongo_matches, auth_headers, fixed_question
):
    played = []
    for i in range(5):
        winner = PLAYER_A if i % 2 == 0 else PLAYER_B
        match_id = _friend_match(client, auth_headers)
        for _ in range(3):
            _win_round(client, auth_headers, match_id, winner)
        played.append((match_id, winner))

    ranked_id = _ranked_match(client, auth_headers)
    for _ in range(3):
        _win_round(client, auth_headers, ranked_id, PLAYER_B)
    played.append((ranked_id, PLAYER_B))

    live_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, live_id, PLAYER_A)

    _space_created_at(mongo_matches, [mid for mid, _ in played] + [live_id])
    listing = _listing(client, auth_headers(OUTSIDER))

    assert [e["match_id"] for e in listing] == [live_id] + [
        mid for mid, _ in reversed(played)
    ]
    by_id = {e["match_id"]: e for e in listing}
    for match_id, winner in played:
        assert by_id[match_id]["status"] == "completed"
        assert by_id[match_id]["score"] == (
            "3-0" if winner == PLAYER_A else "0-3"
        )
        assert by_id[match_id]["rounds_count"] == 3
    assert by_id[live_id]["status"] == "active"
    assert by_id[live_id]["rounds_count"] == 1


# ---------------------------------------------------------------------------
# 8. friend vs ranked (vs bot) in listings and details
# ---------------------------------------------------------------------------


def test_matches_all_entries_have_identical_shape_without_match_type(
    client, mongo_matches, auth_headers
):
    # The listing exposes no match_type: friend and ranked matches are
    # byte-shape identical and indistinguishable without a details call.
    _friend_match(client, auth_headers)
    _ranked_match(client, auth_headers, PLAYER_C, PLAYER_D)

    listing = _listing(client, auth_headers(PLAYER_A))
    assert len(listing) == 2
    for entry in listing:
        assert set(entry.keys()) == LISTING_KEYS
        assert entry["score"] == "0-0"
        assert entry["status"] == "active"


def test_details_distinguish_friend_from_ranked_via_match_type(
    client, mongo_matches, auth_headers
):
    friend_id = _friend_match(client, auth_headers)
    ranked_id = _ranked_match(client, auth_headers, PLAYER_C, PLAYER_D)

    assert _details(client, friend_id).json()["match_type"] == "friend"
    assert _details(client, ranked_id).json()["match_type"] == "ranked"


def test_details_report_unknown_match_type_for_legacy_docs(
    client, mongo_matches, auth_headers
):
    _seed_doc(
        mongo_matches, "seed-legacy", created_at=datetime.utcnow(), match_type=None
    )
    body = _details(client, "seed-legacy", auth_headers(PLAYER_A)).json()
    assert body["match_type"] == "unknown"


def test_matches_all_lists_waiting_friend_match_with_placeholder_opponent(
    client, mongo_matches, auth_headers
):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()

    entry = _listing(client, auth_headers(PLAYER_A))[0]
    assert entry["match_id"] == created["match_id"]
    assert entry["status"] == "waiting"
    assert entry["score"] == "0-0"
    assert entry["player2"] == "Player 2"  # nobody joined yet
    assert entry["rounds_count"] == 0


def test_details_waiting_friend_match_stringifies_missing_opponent(
    client, mongo_matches, auth_headers
):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()

    body = _details(client, created["match_id"], auth_headers(PLAYER_A)).json()
    assert body["status"] == "waiting"
    assert body["player2"]["id"] == "None"  # known str(None) quirk
    assert body["player2"]["username"] == "Player 2"


def test_matches_all_labels_bot_opponent_but_hides_bot_match_type(
    client, mongo_matches, auth_headers
):
    # Bot matches share the people-history listing.  The player2 label is the
    # only tell; the entry shape is identical to friend/ranked entries.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    main.matchmaking_queue[PLAYER_A]["joined_at"] -= timedelta(seconds=11)
    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"

    entry = _listing(client, auth_headers(PLAYER_A))[0]
    assert set(entry.keys()) == LISTING_KEYS
    assert entry["player2"] == "AI Opponent"
    assert _details(client, body["match_id"]).json()["match_type"] == "random"


# ---------------------------------------------------------------------------
# 9. abandoned matches in listings
# ---------------------------------------------------------------------------


def test_matches_all_lists_persisted_abandoned_matches(
    client, mongo_matches, auth_headers
):
    # Abandoned matches are not filtered out of the listing (only the 50-cap
    # applies) - IF the status ever reaches the DB (see the xfail below).
    _seed_doc(
        mongo_matches, "seed-abandoned", created_at=datetime.utcnow(),
        status="abandoned", score=(1, 1),
    )
    entry = _listing(client, auth_headers(PLAYER_A))[0]
    assert entry["status"] == "abandoned"
    assert entry["score"] == "1-1"


def test_current_behavior_http_abandonment_invisible_to_listing_and_details(
    client, mongo_matches, auth_headers
):
    # BUG pin for the xfail below: the reconnect scan in start_match flips
    # the stale match to "abandoned" in memory only - no matches_collection
    # write.  The DB-first listing AND details keep reporting "active",
    # contradicting /api/game/status, and a restart resurrects the match.
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["created_at"] -= timedelta(seconds=10)
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"

    assert main.in_memory_matches[match_id]["status"] == "abandoned"
    assert _status(client, auth_headers, match_id, PLAYER_B).json()["status"] == (
        "abandoned"
    )
    entry = _listing(client, auth_headers(PLAYER_B))[0]
    assert entry["status"] == "active"  # the listing lies
    assert _details(client, match_id).json()["status"] == "active"  # so do details


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(memory-only-abandonment) = bug 35, rediscovered from the "
        "history-consumer angle: start_match marks a stale match abandoned "
        "in in_memory_matches only and never issues the matches_collection "
        "update.  /matches/all and /match/{id}/details (both DB-first) "
        "report the abandoned match as active forever, contradicting "
        "/api/game/status, and after a restart the match rehydrates as "
        "active (feeding the zombie-match bug 8)."
    ),
)
def test_abandoned_match_should_be_listed_as_abandoned(
    client, mongo_matches, auth_headers
):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["created_at"] -= timedelta(seconds=10)
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    assert main.in_memory_matches[match_id]["status"] == "abandoned"

    entry = _listing(client, auth_headers(PLAYER_B))[0]
    assert entry["status"] == "abandoned"


# ---------------------------------------------------------------------------
# 10. rounds array numbering after ties
# ---------------------------------------------------------------------------


def test_current_behavior_tie_duplicates_round_number_in_rounds_array(
    client, mongo_matches, auth_headers, fixed_question
):
    # BUG pin (bug 10 rediscovered from the history-consumer angle):
    # _create_next_round numbers the $push'ed summary by score+1 while the
    # round doc numbers by round count.  A tie doesn't move the score, so
    # the round-2 summary repeats round_number 1.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)
    _question(client, auth_headers, match_id, PLAYER_A)  # round 2 begins

    rounds = _details(client, match_id, auth_headers(PLAYER_A)).json()["rounds"]
    assert [r["round_number"] for r in rounds] == [1, 1]
    assert [r["winner"] for r in rounds] == ["tie", None]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(rounds-array-score-numbering) = bug 10: the match-doc rounds "
        "array numbers rounds by player1_score+player2_score+1, so any tie "
        "round makes the next summary reuse the same round_number and the "
        "history shown by /match/{id}/details stops being sequential."
    ),
)
def test_details_round_numbers_should_stay_strictly_increasing_after_tie(
    client, mongo_matches, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)
    _question(client, auth_headers, match_id, PLAYER_A)

    rounds = _details(client, match_id, auth_headers(PLAYER_A)).json()["rounds"]
    assert [r["round_number"] for r in rounds] == [1, 2]


def test_current_behavior_post_tie_round_win_never_recorded_in_history(
    client, mongo_matches, auth_headers, fixed_question
):
    # Consequence of the numbering skew: every positional update after a tie
    # filters on the round doc's count-based number (2), which no array
    # element carries - so the winner, the winning answer AND the score $set
    # bundled into the same update are all silently dropped by Mongo.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)
    _win_round(client, auth_headers, match_id, PLAYER_A)  # round 2, A wins

    assert main.in_memory_matches[match_id]["player1_score"] == 1
    rounds = _details(client, match_id, auth_headers(PLAYER_A)).json()["rounds"]
    assert rounds[1]["winner"] is None  # decided round looks open forever
    assert rounds[1]["player1_answer"] is None  # winning answer never landed
    assert _listing(client, auth_headers(PLAYER_A))[0]["score"] == "0-0"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(rounds-array-score-numbering) = bug 10, persistence "
        "consequence: after a tie the positional rounds.$ updates match no "
        "array element, so post-tie round winners/answers (and the score "
        "$set sharing those updates) are never written to the match doc."
    ),
)
def test_post_tie_round_results_should_be_persisted_to_history(
    client, mongo_matches, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    rounds = _details(client, match_id, auth_headers(PLAYER_A)).json()["rounds"]
    assert rounds[1]["winner"] == "player1"


def test_current_behavior_completed_match_after_tie_lists_stale_score(
    client, mongo_matches, auth_headers, fixed_question
):
    # Completion itself persists (plain _id filter), but every post-tie score
    # write was dropped: the DB doc - and therefore the listing and details -
    # shows a completed match with winner set and a 0-0 score.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, PLAYER_A)
    assert main.in_memory_matches[match_id]["status"] == "completed"
    assert main.in_memory_matches[match_id]["player1_score"] == 3

    entry = _listing(client, auth_headers(PLAYER_A))[0]
    assert entry["status"] == "completed"
    assert entry["score"] == "0-0"  # the tie froze the persisted score
    assert entry["rounds_count"] == 4  # tie round + 3 played rounds

    body = _details(client, match_id, auth_headers(PLAYER_A)).json()
    assert body["winner"] == PLAYER_A  # winner set...
    assert body["score"] == "0-0"  # ...next to a score that can't produce one


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(rounds-array-score-numbering) = bug 10, listing consequence: "
        "the final score of any match containing a tie round is never "
        "persisted, so /matches/all and /match/{id}/details report a "
        "completed match at the score frozen when the first tie happened."
    ),
)
def test_completed_match_listing_score_should_match_final_result(
    client, mongo_matches, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _tie_current_round(client, auth_headers, match_id)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, PLAYER_A)

    entry = _listing(client, auth_headers(PLAYER_A))[0]
    assert entry["score"] == "3-0"
