"""
Miscellaneous niche people-match edge cases that no dedicated suite owns.

1.  Friend-create response link format embeds the match code
2.  MatchStart.continue_existing default (model + omitted/false/null/true wire
    behavior against a stale active match)
3.  Player usernames None/missing for guests in match docs (create/join/status)
4.  Friend match-code charset: exactly 6 chars from A-Z0-9
5.  Ranked/bot match codes: secrets.token_urlsafe(8) -> 11 URL-safe chars
6.  random.seed makes the bot name + ELO offset reproducible (module-level RNG)
7.  Question difficulty always uses the LOWER of the two player ELOs,
    regardless of which slot holds it
8.  In-memory match evicted mid-game, then rehydrated from a stateful FakeDB
9.  Rediscovery pin: tz-aware created_at turns /api/game/start into the
    generic-500 handler (TypeError; known bug 12, datetime suite xfail)
10. /api/leaderboard survives a completed ranked match
11. Daily-challenge flow leaves live match state untouched
12. /api/user/me identity is stable during (and after) an active match

Conventions match the sibling edge-case files: guest identities via
"Bearer guest-xxx" tokens, fixed_question for deterministic grading, and
current-behavior pins for already-catalogued bugs.  See
MATCH_EDGE_CASE_REPORT.md.
"""

import copy
import random
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main


PLAYER_A = "guest-misc-aaa1"
PLAYER_B = "guest-misc-bbb2"

CORRECT = "2*x"  # matches fixed_question's answer "2·x"

BOT_ROSTER = [
    "James (bot)",
    "Alex (bot)",
    "Sam (bot)",
    "Taylor (bot)",
    "Jordan (bot)",
    "Casey (bot)",
    "Morgan (bot)",
]

FRIEND_CODE_RE = re.compile(r"^[A-Z0-9]{6}$")
# token_urlsafe(8) = 8 random bytes -> ceil(8*4/3) = 11 base64url chars.
TOKEN_URLSAFE_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _start(client, auth_headers, player, **extra):
    response = client.post(
        "/api/game/start", json={"mode": "random", **extra}, headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ranked_match(client, auth_headers, player1=PLAYER_A, player2=PLAYER_B):
    """Queue player2 first so the joining `player1` lands in the player1 slot."""
    assert _start(client, auth_headers, player2)["status"] == "searching"
    body = _start(client, auth_headers, player1)
    assert body["status"] == "matched", body
    return body


def _bot_match(client, auth_headers, player):
    """Queue `player`, expire the 10s window, poll again -> bot match."""
    assert _start(client, auth_headers, player)["status"] == "searching"
    main.matchmaking_queue[player]["joined_at"] -= timedelta(seconds=11)
    body = _start(client, auth_headers, player)
    assert body["status"] == "matched", body
    return body


def _friend_create(client, auth_headers, player=PLAYER_A, opponent_username=None):
    response = client.post(
        "/api/game/friend/create",
        json={"opponent_username": opponent_username},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _friend_join(client, auth_headers, code, player=PLAYER_B):
    return client.post(
        "/api/game/friend/join",
        json={"match_code": code},
        headers=auth_headers(player),
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


def _stale_active_match(match_id, player, opponent="guest-misc-opp", age_seconds=10):
    """Seed an in-memory active match old enough to be outside the 5s window."""
    doc = {
        "_id": match_id,
        "match_code": "STALE1",
        "match_type": "ranked",
        "player1_id": player,
        "player2_id": opponent,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": 1000,
        "player2_elo": 1000,
        "status": "active",
        "winner_id": None,
        "elo_change": 0,
        "created_at": datetime.utcnow() - timedelta(seconds=age_seconds),
    }
    main.in_memory_matches[match_id] = doc
    return doc


def _win_ranked(client, auth_headers, winner=PLAYER_A, loser=PLAYER_B):
    """Play a ranked match to first-to-3 with `winner` sweeping every round."""
    match_id = _ranked_match(client, auth_headers, winner, loser)["match_id"]
    for round_number in range(1, 4):
        q = _question(client, auth_headers, match_id, winner)
        assert q.status_code == 200, q.text
        a = _answer(client, auth_headers, match_id, winner)
        assert a.status_code == 200, a.text
        assert a.json()["correct"] is True
    assert main.in_memory_matches[match_id]["status"] == "completed"
    return match_id


@pytest.fixture
def client_no_reraise(mock_mongo):
    """Client that returns the handler's 500 instead of re-raising in-test."""
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# 1. friend-create link format
# ---------------------------------------------------------------------------


def test_friend_create_link_embeds_the_match_code(client, auth_headers):
    body = _friend_create(client, auth_headers)

    assert body["link"] == f"http://localhost:3000/play/friend/{body['match_code']}"
    assert body["link"].endswith(body["match_code"])
    # The link is hardcoded to the localhost dev frontend, not an env-driven
    # origin — a share link generated in production points at localhost:3000.
    assert body["link"].startswith("http://localhost:3000/play/friend/")


def test_friend_create_link_format_holds_for_named_challenges(client, auth_headers):
    # opponent_username changes status semantics but not the link shape.
    body = _friend_create(client, auth_headers, opponent_username="somebody")

    assert body["link"] == f"http://localhost:3000/play/friend/{body['match_code']}"


# ---------------------------------------------------------------------------
# 2. MatchStart.continue_existing default
# ---------------------------------------------------------------------------


def test_matchstart_model_defaults_continue_existing_to_false():
    assert main.MatchStart(mode="random").continue_existing is False
    # Optional[bool] also admits an explicit null; it is preserved (not
    # coerced to the False default) and behaves falsy downstream.
    assert main.MatchStart(mode="random", continue_existing=None).continue_existing is None


def test_start_without_continue_existing_abandons_stale_active_match(
    client, auth_headers
):
    doc = _stale_active_match("match-ce-omit", PLAYER_A)

    body = _start(client, auth_headers, PLAYER_A)  # no continue_existing key

    assert body["status"] == "searching"
    assert doc["status"] == "abandoned"


def test_start_with_explicit_false_matches_the_omitted_default(client, auth_headers):
    doc = _stale_active_match("match-ce-false", PLAYER_A)

    body = _start(client, auth_headers, PLAYER_A, continue_existing=False)

    assert body["status"] == "searching"
    assert doc["status"] == "abandoned"


def test_start_with_null_continue_existing_behaves_like_false(client, auth_headers):
    doc = _stale_active_match("match-ce-null", PLAYER_A)

    body = _start(client, auth_headers, PLAYER_A, continue_existing=None)

    assert body["status"] == "searching"
    assert doc["status"] == "abandoned"


def test_start_with_continue_existing_true_preserves_stale_match_but_still_queues(
    client, auth_headers
):
    # QUIRK: continue_existing=True only *suppresses the abandonment*; the
    # response is still "searching" and the caller is queued for a brand-new
    # match while the old one stays active in memory.
    doc = _stale_active_match("match-ce-true", PLAYER_A)

    body = _start(client, auth_headers, PLAYER_A, continue_existing=True)

    assert body["status"] == "searching"
    assert doc["status"] == "active"
    assert PLAYER_A in main.matchmaking_queue


# ---------------------------------------------------------------------------
# 3. guest usernames None/missing in match docs
# ---------------------------------------------------------------------------


def test_guest_friend_create_stores_fallback_name_and_null_player2_username(
    client, auth_headers
):
    body = _friend_create(client, auth_headers, PLAYER_A)
    doc = main.in_memory_matches[body["match_id"]]

    # Guests carry no "username" key, so .get("username", name) falls back to
    # the synthetic display name — the doc never stores a literal None here.
    assert doc["player1_username"] == f"Guest {PLAYER_A[-4:]}"
    # Open invite: no opponent yet, username explicitly None.
    assert doc["player2_id"] is None
    assert doc["player2_username"] is None
    assert doc["status"] == "waiting"


def test_named_challenge_to_unknown_user_stores_username_without_id(
    client, auth_headers
):
    # QUIRK: users_collection has no such user, so opponent_id stays None and
    # the match silently degrades to an open "waiting" code invite — but the
    # unmatched username is still written onto the doc.
    body = _friend_create(client, auth_headers, PLAYER_A, opponent_username="ghost-user")
    doc = main.in_memory_matches[body["match_id"]]

    assert doc["player2_id"] is None
    assert doc["player2_username"] == "ghost-user"
    assert body["status"] == "waiting"  # not "pending": that needs a real id


def test_join_leaves_player2_username_null_and_status_serves_placeholders(
    client, auth_headers
):
    body = _friend_create(client, auth_headers, PLAYER_A)
    assert _friend_join(client, auth_headers, body["match_code"], PLAYER_B).status_code == 200

    doc = main.in_memory_matches[body["match_id"]]
    assert doc["player2_id"] == PLAYER_B
    # join_friend_match never backfills player2_username.
    assert doc["player2_username"] is None

    # The status poll looks players up in users_collection (guests are never
    # there), so both stored names are ignored in favor of placeholders.
    status = client.get(
        f"/api/game/status/{body['match_id']}", headers=auth_headers(PLAYER_A)
    )
    assert status.status_code == 200
    assert status.json()["player1_name"] == "Player 1"
    assert status.json()["player2_name"] == "Player 2"


# ---------------------------------------------------------------------------
# 4. friend match-code charset
# ---------------------------------------------------------------------------


def test_friend_match_codes_are_six_chars_of_uppercase_alphanumerics(
    client, auth_headers
):
    codes = []
    for i in range(12):
        body = _friend_create(client, auth_headers, f"guest-misc-code-{i}")
        codes.append(body["match_code"])
        # The stored doc carries the exact same code as the response.
        assert main.in_memory_matches[body["match_id"]]["match_code"] == body["match_code"]

    for code in codes:
        assert FRIEND_CODE_RE.fullmatch(code), code
        assert code == code.upper()


# ---------------------------------------------------------------------------
# 5. ranked / bot match-code charset (secrets.token_urlsafe)
# ---------------------------------------------------------------------------


def test_ranked_match_code_is_eleven_urlsafe_characters(client, auth_headers):
    body = _ranked_match(client, auth_headers)

    assert TOKEN_URLSAFE_RE.fullmatch(body["match_code"]), body["match_code"]
    assert main.in_memory_matches[body["match_id"]]["match_code"] == body["match_code"]


def test_bot_fallback_match_code_is_also_token_urlsafe(client, auth_headers):
    body = _bot_match(client, auth_headers, PLAYER_A)

    assert TOKEN_URLSAFE_RE.fullmatch(body["match_code"]), body["match_code"]
    # Unlike friend codes these can contain lowercase, so the friend-join
    # route's .upper() normalization could never find them by code.
    assert main.in_memory_matches[body["match_id"]]["player2_id"] == "bot-opponent"


# ---------------------------------------------------------------------------
# 6. random.seed reproducibility of the bot roster draw
# ---------------------------------------------------------------------------


def test_seeded_rng_reproduces_the_same_bot_name_and_elo_offset(client, auth_headers):
    # start_match draws the bot name and ELO offset from the module-level
    # `random` (not `secrets`), so seeding the global RNG is enough to make
    # the whole bot identity deterministic.
    saved_state = random.getstate()
    try:
        def sample(tag):
            main.random.seed(20260720)
            player = f"guest-misc-seed-{tag}"
            body = _bot_match(client, auth_headers, player)
            match = main.in_memory_matches[body["match_id"]]
            return body["opponent"], match["player2_elo"] - match["player1_elo"]

        first = sample("one")
        # Wipe process state so the second run is a fresh, identical draw.
        main.in_memory_matches.clear()
        main.matchmaking_queue.clear()
        second = sample("two")
    finally:
        random.setstate(saved_state)

    assert first == second
    assert first[0] in BOT_ROSTER
    assert -150 <= first[1] <= -50


# ---------------------------------------------------------------------------
# 7. question difficulty uses the lower player ELO in either slot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "p1_elo, p2_elo, expected_elo",
    [
        (2000, 800, 800),   # low ELO in the player2 slot
        (800, 2000, 800),   # same players, slots swapped
        (1500, 1500, 1500), # equal ELOs: no lower one to prefer
    ],
)
def test_question_generator_receives_the_lower_elo(
    client, auth_headers, monkeypatch, p1_elo, p2_elo, expected_elo
):
    seen_elos = []

    def spy_generate(elo):
        seen_elos.append(elo)
        return {
            "expression": "x^2",
            "derivative": "2·x",
            "evaluate_at": 0,
            "answer": "2·x",
            "difficulty": 1,
            "ask_for_derivative_only": True,
        }

    monkeypatch.setattr(main, "generate_question", spy_generate)

    main.in_memory_matches["match-elo-swap"] = {
        "_id": "match-elo-swap",
        "match_code": "ELOSWP",
        "match_type": "friend",
        "player1_id": PLAYER_A,
        "player2_id": PLAYER_B,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": p1_elo,
        "player2_elo": p2_elo,
        "status": "active",
        "winner_id": None,
        "elo_change": 0,
        "created_at": datetime.utcnow(),
    }

    response = _question(client, auth_headers, "match-elo-swap", PLAYER_A)
    assert response.status_code == 200, response.text
    assert seen_elos == [expected_elo]


# ---------------------------------------------------------------------------
# 8. in-memory match evicted mid-game, rehydrated from a stateful FakeDB
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
    """Stateful Mongo stand-in (same shape as the hydrate suite's fake)."""

    def __init__(self):
        self.docs = {}

    @staticmethod
    def _matches(doc, query):
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


def _played_friend_match(client, auth_headers):
    """Create + join a friend match and serve round 1 (persisted to fakes)."""
    body = _friend_create(client, auth_headers, PLAYER_A)
    assert _friend_join(client, auth_headers, body["match_code"], PLAYER_B).status_code == 200
    q = _question(client, auth_headers, body["match_id"], PLAYER_A)
    assert q.status_code == 200, q.text
    return body["match_id"], q.json()


def test_match_evicted_mid_round_rehydrates_and_resumes_the_same_round(
    client, fake_matches_db, fake_rounds_db, auth_headers, fixed_question
):
    match_id, round1 = _played_friend_match(client, auth_headers)

    # Simulate cache eviction of the match doc only (round stays cached).
    del main.in_memory_matches[match_id]
    assert match_id in fake_matches_db.docs  # DB copy survives

    response = _question(client, auth_headers, match_id, PLAYER_B)
    assert response.status_code == 200, response.text
    body = response.json()
    # The hydrated match doc carries the written-back current_round_id, so the
    # opponent resumes the exact same round instead of forking a new one.
    assert body["round_id"] == round1["round_id"]
    assert body["expression"] == round1["expression"]
    assert body["round_start_time"] == round1["round_start_time"]
    assert match_id in main.in_memory_matches  # cached back

    # Gameplay continues seamlessly off the hydrated doc.
    answer = _answer(client, auth_headers, match_id, PLAYER_B)
    assert answer.status_code == 200, answer.text
    assert answer.json()["correct"] is True
    assert answer.json()["player2_score"] == 1
    assert fake_matches_db.docs[match_id]["player2_score"] == 1


def test_match_evicted_after_scoring_rehydrates_with_the_persisted_score(
    client, fake_matches_db, fake_rounds_db, auth_headers, fixed_question
):
    match_id, _ = _played_friend_match(client, auth_headers)
    answer = _answer(client, auth_headers, match_id, PLAYER_A)
    assert answer.json()["player1_score"] == 1

    del main.in_memory_matches[match_id]

    # The status poll hydrates the persisted doc: score intact, still active.
    status = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_B)
    )
    assert status.status_code == 200, status.text
    assert status.json()["player1_score"] == 1
    assert status.json()["status"] == "active"

    # Round 1 is resolved (its doc is still cached), so the next question
    # rolls forward to round 2 rather than replaying round 1.
    q = _question(client, auth_headers, match_id, PLAYER_B)
    assert q.status_code == 200, q.text
    assert q.json()["round_id"] == f"round-{match_id}-2"
    assert len(fake_matches_db.docs[match_id]["rounds"]) == 2


# ---------------------------------------------------------------------------
# 9. rediscovery pin: aware created_at 500s /api/game/start
# ---------------------------------------------------------------------------


def test_rediscovered_aware_created_at_500s_start_with_generic_detail(
    client_no_reraise, auth_headers
):
    # Known bug 12 (xfail-pinned in the datetime suite): the reconnect window
    # computes naive utcnow() minus created_at with no ensure_utc, so a
    # tz-aware timestamp raises TypeError.  Re-discovered here from the
    # error-handling side: the global handler converts it to a generic 500
    # with no internals leaked, and every retry hits the same wall.
    doc = _stale_active_match("match-aware-500", PLAYER_A, age_seconds=0)
    doc["created_at"] = main.utc_now()  # tz-aware
    assert doc["created_at"].tzinfo is not None

    for _ in range(2):  # persistent: retrying does not clear it
        response = client_no_reraise.post(
            "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_A)
        )
        assert response.status_code == 500
        assert response.json()["detail"] == "Something went wrong. Please try again."
        assert "TypeError" not in response.text
        assert "traceback" not in response.text.lower()

    # The poisoned match is untouched (never abandoned, never matched).
    assert doc["status"] == "active"


# ---------------------------------------------------------------------------
# 10. leaderboard survives a completed ranked match
# ---------------------------------------------------------------------------


def test_leaderboard_survives_a_completed_ranked_match(
    client, auth_headers, fixed_question
):
    before = client.get("/api/leaderboard")
    assert before.status_code == 200
    assert before.json() == []

    match_id = _win_ranked(client, auth_headers)
    assert main.in_memory_matches[match_id]["elo_change"] > 0

    # Guests never land in users_collection, so the board stays empty — but
    # the endpoint must not crash on the freshly completed match state.
    after = client.get("/api/leaderboard")
    assert after.status_code == 200
    assert after.json() == []


# ---------------------------------------------------------------------------
# 11. daily challenge does not pollute live match state
# ---------------------------------------------------------------------------


def test_daily_challenge_flow_leaves_live_match_state_untouched(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id = _ranked_match(client, auth_headers)["match_id"]
    assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 200

    match_snapshot = copy.deepcopy(main.in_memory_matches[match_id])
    rounds_snapshot = copy.deepcopy(main.in_memory_rounds)

    # Pin today's challenge to a known answer (setitem restores the original).
    today = datetime.now(timezone.utc).date().isoformat()
    monkeypatch.setitem(
        main.daily_challenges_storage,
        today,
        {
            "date": today,
            "expression": "x^2",
            "derivative": "2·x",
            "answer": "2·x",
            "difficulty": 1,
        },
    )

    fetched = client.get("/api/daily-challenge/today", headers=auth_headers(PLAYER_A))
    assert fetched.status_code == 200
    assert fetched.json()["user_completed"] is False

    submitted = client.post(
        "/api/daily-challenge/submit",
        json={"answer": CORRECT, "time": 12.5},
        headers=auth_headers(PLAYER_A),
    )
    assert submitted.status_code == 200
    assert submitted.json()["correct"] is True

    # The whole daily flow touched neither the match nor the round stores.
    assert main.in_memory_matches[match_id] == match_snapshot
    assert main.in_memory_rounds == rounds_snapshot
    assert main.matchmaking_queue == {}

    # And the match is still fully playable afterwards.
    answer = _answer(client, auth_headers, match_id, PLAYER_A)
    assert answer.status_code == 200, answer.text
    assert answer.json()["correct"] is True


# ---------------------------------------------------------------------------
# 12. /api/user/me during an active match
# ---------------------------------------------------------------------------


def test_user_me_identity_is_stable_during_an_active_match(client, auth_headers):
    _ranked_match(client, auth_headers)

    response = client.get("/api/user/me", headers=auth_headers(PLAYER_A))
    assert response.status_code == 200
    body = response.json()

    assert set(body.keys()) == {"id", "email", "name", "username", "elo"}
    assert body["id"] == PLAYER_A
    assert body["email"] == f"{PLAYER_A}@derivative-duel.com"
    assert body["name"] == f"Guest {PLAYER_A[-4:]}"
    assert body["username"] is None  # guests never have a username
    assert body["elo"] == 1000


def test_user_me_elo_stays_1000_for_guests_even_after_winning_ranked(
    client, auth_headers, fixed_question
):
    # The completed match paid out a positive elo_change, but guest identities
    # are rebuilt from the token on every request — the payout only ever went
    # to the (mocked) users_collection, so /api/user/me still reports 1000.
    match_id = _win_ranked(client, auth_headers)
    assert main.in_memory_matches[match_id]["elo_change"] > 0

    for player in (PLAYER_A, PLAYER_B):
        body = client.get("/api/user/me", headers=auth_headers(player)).json()
        assert body["elo"] == 1000
