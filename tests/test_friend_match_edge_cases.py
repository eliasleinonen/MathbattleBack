"""
Edge-case tests for friend matches (create / join / status / match-by-code).

These tests intentionally exercise niche behaviours of the friend-match
endpoints in main.py (~lines 866-1000 and /api/game/match/{match_code}):

- match-code validation, case normalization and uniqueness
- join-order rules (own match, third player, finished matches, double join)
- the unauthenticated /api/game/friend/status/{code} poller
- create with opponent_username (challenge) vs without (waiting/shareable code)
- concurrent joins on the same waiting code
- outsider access to gameplay routes
- /api/game/match/{match_code} lookup quirks

Known bugs are documented with comments and strict xfail markers instead of
changing main.py.  See MATCH_EDGE_CASE_REPORT.md for a summary.
"""

import asyncio
import copy

import pytest

import main


PLAYER_A = "guest-player-aaa"
PLAYER_B = "guest-player-bbb"
PLAYER_C = "guest-outsider-ccc"

# A "registered" user that friend challenges can target by username.
REGISTERED_USERNAME = "BeeKeeper"
REGISTERED_USER = {
    "_id": PLAYER_B,
    "username": REGISTERED_USERNAME,
    "name": "Bee Keeper",
    "elo": 1234,
    "wins": 0,
    "losses": 0,
}


@pytest.fixture
def known_users(mock_mongo, monkeypatch):
    """
    users_collection.find_one that resolves usernames exactly like Mongo's
    default collation: byte-for-byte, case-SENSITIVE equality.
    """
    registry = {REGISTERED_USERNAME: REGISTERED_USER}

    async def find_one(query, *args, **kwargs):
        username = query.get("username")
        if username is not None:
            return copy.deepcopy(registry.get(username))
        return None

    monkeypatch.setattr(main.users_collection, "find_one", find_one)
    return registry


def _create(client, auth_headers, player_id=PLAYER_A, body=None):
    response = client.post(
        "/api/game/friend/create",
        json=body if body is not None else {},
        headers=auth_headers(player_id),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _join(client, auth_headers, match_code, player_id=PLAYER_B):
    return client.post(
        "/api/game/friend/join",
        json={"match_code": match_code},
        headers=auth_headers(player_id),
    )


# ---------------------------------------------------------------------------
# Create: response shape, match code format, empty bodies
# ---------------------------------------------------------------------------


def test_create_returns_six_char_uppercase_code_and_link(client, auth_headers):
    body = _create(client, auth_headers)
    code = body["match_code"]

    assert len(code) == 6
    assert code == code.upper()
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for c in code)
    # The shareable link embeds the code verbatim.
    assert body["link"].endswith(f"/play/friend/{code}")
    assert body["status"] == "waiting"


def test_create_with_empty_json_body_defaults_to_waiting(client, auth_headers):
    body = _create(client, auth_headers, body={})
    assert body["status"] == "waiting"

    match = main.in_memory_matches[body["match_id"]]
    assert match["player2_id"] is None
    assert match["player2_username"] is None


def test_create_with_explicit_null_opponent_is_waiting(client, auth_headers):
    body = _create(client, auth_headers, body={"opponent_username": None})
    assert body["status"] == "waiting"


def test_create_without_body_is_rejected(client, auth_headers):
    # FriendMatchCreate is a required body parameter, so an empty request
    # body fails validation even though all its fields are optional.
    response = client.post("/api/game/friend/create", headers=auth_headers(PLAYER_A))
    assert response.status_code == 422


def test_create_stores_friend_match_type(client, auth_headers):
    body = _create(client, auth_headers)
    match = main.in_memory_matches[body["match_id"]]
    assert match["match_type"] == "friend"


def test_created_match_ids_are_unique_across_creates(client, auth_headers):
    ids = {_create(client, auth_headers)["match_id"] for _ in range(5)}
    assert len(ids) == 5


# ---------------------------------------------------------------------------
# Match code uniqueness
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: create_friend_match only checks the *database* for match-code "
        "collisions (matches_collection.find_one).  Matches held in "
        "in_memory_matches are never consulted, so when the DB is empty or "
        "unavailable two live matches can share the same code; the second "
        "match is then unreachable via /friend/join (the in-memory scan "
        "returns the first hit)."
    ),
)
def test_match_codes_unique_even_when_rng_repeats(client, auth_headers, monkeypatch):
    def rigged_choices(population, k=6):
        return list("AAAAAA")

    monkeypatch.setattr(main.random, "choices", rigged_choices)

    first = _create(client, auth_headers, PLAYER_A)
    second = _create(client, auth_headers, PLAYER_C)

    assert first["match_code"] != second["match_code"]


def test_colliding_codes_shadow_the_second_match_on_join(
    client, auth_headers, monkeypatch
):
    """
    Companion to the xfail above: documents what actually happens today when
    two in-memory matches share a code -- joining resolves to whichever match
    was created first, the second is unreachable by code.
    """
    def rigged_choices(population, k=6):
        return list("AAAAAA")

    monkeypatch.setattr(main.random, "choices", rigged_choices)

    first = _create(client, auth_headers, PLAYER_A)
    second = _create(client, auth_headers, PLAYER_C)
    assert first["match_code"] == second["match_code"] == "AAAAAA"

    joined = _join(client, auth_headers, "AAAAAA", PLAYER_B)
    assert joined.status_code == 200
    assert joined.json()["match_id"] == first["match_id"]
    # The second creator's match is still waiting and cannot be joined by
    # code any more (the scan always finds the first match, now active).
    assert main.in_memory_matches[second["match_id"]]["status"] == "waiting"
    blocked = _join(client, auth_headers, "AAAAAA", "guest-player-ddd")
    assert blocked.status_code == 400


# ---------------------------------------------------------------------------
# Join: code validation and case normalization
# ---------------------------------------------------------------------------


def test_join_nonexistent_code_returns_404(client, auth_headers):
    response = _join(client, auth_headers, "ZZZZ99")
    assert response.status_code == 404
    assert response.json()["detail"] == "Match not found"


def test_join_empty_code_returns_404(client, auth_headers):
    _create(client, auth_headers)
    response = _join(client, auth_headers, "")
    assert response.status_code == 404


def test_join_whitespace_code_returns_404(client, auth_headers):
    _create(client, auth_headers)
    response = _join(client, auth_headers, "      ")
    assert response.status_code == 404


def test_join_missing_code_field_is_422(client, auth_headers):
    response = client.post(
        "/api/game/friend/join", json={}, headers=auth_headers(PLAYER_B)
    )
    assert response.status_code == 422


def test_join_non_string_code_is_422(client, auth_headers):
    # pydantic v2 does not coerce ints to str for plain `str` fields.
    response = client.post(
        "/api/game/friend/join",
        json={"match_code": 123456},
        headers=auth_headers(PLAYER_B),
    )
    assert response.status_code == 422


def test_join_lowercase_code_is_normalized(client, auth_headers):
    created = _create(client, auth_headers)
    response = _join(client, auth_headers, created["match_code"].lower())
    assert response.status_code == 200
    assert response.json()["match_id"] == created["match_id"]


def test_join_mixed_case_code_is_normalized(client, auth_headers):
    created = _create(client, auth_headers)
    code = created["match_code"]
    mixed = "".join(
        c.lower() if i % 2 == 0 else c.upper() for i, c in enumerate(code)
    )
    response = _join(client, auth_headers, mixed)
    assert response.status_code == 200


def test_join_code_with_surrounding_whitespace_is_not_trimmed(client, auth_headers):
    # Documented behaviour: the code is upper-cased but never stripped, so a
    # code pasted with a trailing space does not match.  (Arguably a UX bug,
    # but it is deliberate enough that we pin it rather than xfail.)
    created = _create(client, auth_headers)
    response = _join(client, auth_headers, f" {created['match_code']} ")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Join: who may join, and when
# ---------------------------------------------------------------------------


def test_cannot_join_own_match(client, auth_headers):
    created = _create(client, auth_headers, PLAYER_A)
    response = _join(client, auth_headers, created["match_code"], PLAYER_A)
    assert response.status_code == 400
    assert "own match" in response.json()["detail"].lower()


def test_join_assigns_player2_id_and_elo(client, auth_headers):
    created = _create(client, auth_headers, PLAYER_A)
    response = _join(client, auth_headers, created["match_code"], PLAYER_B)
    assert response.status_code == 200

    match = main.in_memory_matches[created["match_id"]]
    assert str(match["player1_id"]) == PLAYER_A
    assert str(match["player2_id"]) == PLAYER_B
    assert match["player2_elo"] == 1000  # guest elo
    assert match["status"] == "active"


def test_third_player_cannot_join_active_match(client, auth_headers):
    created = _create(client, auth_headers, PLAYER_A)
    assert _join(client, auth_headers, created["match_code"], PLAYER_B).status_code == 200

    third = _join(client, auth_headers, created["match_code"], PLAYER_C)
    assert third.status_code == 400
    assert third.json()["detail"] == "Match already started"

    # player2 assignment untouched by the failed join
    match = main.in_memory_matches[created["match_id"]]
    assert str(match["player2_id"]) == PLAYER_B


def test_double_join_by_same_player2_is_rejected(client, auth_headers):
    # Join is not idempotent: once player B is in, a retried join by the very
    # same player gets 400 "Match already started".  Clients must not retry.
    created = _create(client, auth_headers, PLAYER_A)
    assert _join(client, auth_headers, created["match_code"], PLAYER_B).status_code == 200

    retry = _join(client, auth_headers, created["match_code"], PLAYER_B)
    assert retry.status_code == 400
    assert str(main.in_memory_matches[created["match_id"]]["player2_id"]) == PLAYER_B


def test_join_completed_match_is_rejected(client, auth_headers):
    created = _create(client, auth_headers, PLAYER_A)
    main.in_memory_matches[created["match_id"]]["status"] = "completed"

    response = _join(client, auth_headers, created["match_code"], PLAYER_B)
    assert response.status_code == 400
    # NOTE: the message "Match already started" is misleading for a match
    # that already *finished*, but that is the current behaviour.
    assert response.json()["detail"] == "Match already started"


def test_join_abandoned_match_is_rejected(client, auth_headers):
    created = _create(client, auth_headers, PLAYER_A)
    main.in_memory_matches[created["match_id"]]["status"] = "abandoned"

    response = _join(client, auth_headers, created["match_code"], PLAYER_B)
    assert response.status_code == 400


def test_pending_challenge_cannot_be_joined_via_code(
    client, known_users, auth_headers
):
    # A direct challenge is created with status "pending" (not "waiting"), so
    # the code-join path rejects it -- even for the invited player themself,
    # who must go through /api/challenges/accept instead.
    created = _create(
        client, auth_headers, PLAYER_A, body={"opponent_username": REGISTERED_USERNAME}
    )
    assert created["status"] == "pending"

    invited = _join(client, auth_headers, created["match_code"], PLAYER_B)
    assert invited.status_code == 400
    assert invited.json()["detail"] == "Match already started"

    stranger = _join(client, auth_headers, created["match_code"], PLAYER_C)
    assert stranger.status_code == 400


# ---------------------------------------------------------------------------
# Concurrent joins
# ---------------------------------------------------------------------------


def test_concurrent_joins_with_db_latency_both_succeed(
    client, auth_headers, monkeypatch
):
    """
    DOCUMENTED RACE: join_friend_match does a check-then-set with no
    per-match lock (unlike get_question, which uses get_match_lock).  When
    the match document is served from the database with any latency, two
    players can both read status == "waiting", both pass the checks, and
    both get a 200 -- the second writer silently overwrites player2_id, so
    the first "successful" joiner is kicked out of the match.

    The mocked find_one below behaves like a real DB read: it snapshots the
    document, then yields to the event loop before returning.
    """

    created = _create(client, auth_headers, PLAYER_A)
    match_id = created["match_id"]
    code = created["match_code"]

    async def db_find_one_with_latency(query, *args, **kwargs):
        snapshot = None
        for m in main.in_memory_matches.values():
            if m.get("match_code") == query.get("match_code"):
                snapshot = copy.deepcopy(m)
                break
        await asyncio.sleep(0)  # simulate network latency after the read
        return snapshot

    monkeypatch.setattr(main.matches_collection, "find_one", db_find_one_with_latency)

    async def join_concurrently():
        data = main.FriendMatchJoin(match_code=code)
        user_b = {"_id": PLAYER_B, "elo": 1000}
        user_c = {"_id": PLAYER_C, "elo": 1000}
        return await asyncio.gather(
            main.join_friend_match(data, current_user=user_b),
            main.join_friend_match(data, current_user=user_c),
            return_exceptions=True,
        )

    result_b, result_c = asyncio.run(join_concurrently())

    # BUG: both joins succeed; nobody gets a 400.
    assert not isinstance(result_b, Exception), result_b
    assert not isinstance(result_c, Exception), result_c
    assert result_b["status"] == "active"
    assert result_c["status"] == "active"

    # Last writer wins: player C overwrote player B's slot even though B's
    # join had already been acknowledged.
    final = main.in_memory_matches[match_id]
    assert str(final["player2_id"]) == PLAYER_C


def test_sequential_like_concurrent_joins_without_db_reject_second(
    client, auth_headers
):
    """
    Counterpart to the race above: when the match is resolved purely from
    in_memory_matches (DB miss, no awaits inside the critical section), the
    check-then-set runs atomically on the event loop and the second join is
    correctly rejected.
    """
    created = _create(client, auth_headers, PLAYER_A)
    code = created["match_code"]

    async def join_concurrently():
        data = main.FriendMatchJoin(match_code=code)
        user_b = {"_id": PLAYER_B, "elo": 1000}
        user_c = {"_id": PLAYER_C, "elo": 1000}
        return await asyncio.gather(
            main.join_friend_match(data, current_user=user_b),
            main.join_friend_match(data, current_user=user_c),
            return_exceptions=True,
        )

    result_b, result_c = asyncio.run(join_concurrently())

    assert result_b["status"] == "active"
    assert isinstance(result_c, main.HTTPException)
    assert result_c.status_code == 400
    assert str(main.in_memory_matches[created["match_id"]]["player2_id"]) == PLAYER_B


# ---------------------------------------------------------------------------
# Unauthenticated status endpoint /api/game/friend/status/{code}
# ---------------------------------------------------------------------------


def test_status_without_auth_for_waiting_match(client, auth_headers):
    created = _create(client, auth_headers)
    response = client.get(f"/api/game/friend/status/{created['match_code']}")
    assert response.status_code == 200
    body = response.json()
    assert body["match_id"] == created["match_id"]
    assert body["status"] == "waiting"
    assert body["player1_ready"] is True
    assert body["player2_ready"] is False


def test_status_without_auth_for_active_match(client, auth_headers):
    created = _create(client, auth_headers)
    assert _join(client, auth_headers, created["match_code"]).status_code == 200

    response = client.get(f"/api/game/friend/status/{created['match_code']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    assert body["player1_ready"] is True
    assert body["player2_ready"] is True


def test_status_without_auth_for_pending_challenge(client, known_users, auth_headers):
    created = _create(
        client, auth_headers, PLAYER_A, body={"opponent_username": REGISTERED_USERNAME}
    )
    response = client.get(f"/api/game/friend/status/{created['match_code']}")
    assert response.status_code == 200
    body = response.json()
    # A pending direct challenge already has both player ids assigned, so
    # both "ready" flags are True while the status is still "pending".
    assert body["status"] == "pending"
    assert body["player1_ready"] is True
    assert body["player2_ready"] is True


def test_status_without_auth_unknown_code_404(client):
    response = client.get("/api/game/friend/status/NOPE42")
    assert response.status_code == 404


def test_status_without_auth_normalizes_lowercase_code(client, auth_headers):
    created = _create(client, auth_headers)
    response = client.get(
        f"/api/game/friend/status/{created['match_code'].lower()}"
    )
    assert response.status_code == 200
    assert response.json()["match_id"] == created["match_id"]


def test_status_endpoint_leaks_no_scores_or_player_ids(client, auth_headers):
    # The unauthenticated poller intentionally exposes only readiness; make
    # sure it never starts leaking scores or raw player ids.
    created = _create(client, auth_headers)
    body = client.get(f"/api/game/friend/status/{created['match_code']}").json()
    assert set(body.keys()) == {
        "match_id",
        "status",
        "player1_ready",
        "player2_ready",
    }


# ---------------------------------------------------------------------------
# Create with opponent_username: challenge vs open (waiting) match
# ---------------------------------------------------------------------------


def test_create_with_existing_opponent_creates_pending_challenge(
    client, known_users, auth_headers
):
    created = _create(
        client, auth_headers, PLAYER_A, body={"opponent_username": REGISTERED_USERNAME}
    )
    assert created["status"] == "pending"

    match = main.in_memory_matches[created["match_id"]]
    assert match["player2_id"] == PLAYER_B
    assert match["player2_username"] == REGISTERED_USERNAME
    assert match["player2_elo"] == REGISTERED_USER["elo"]
    assert match["status"] == "pending"


def test_create_with_unknown_opponent_falls_back_to_waiting(
    client, known_users, auth_headers
):
    created = _create(
        client, auth_headers, PLAYER_A, body={"opponent_username": "NoSuchPlayer"}
    )
    # Nobody by that name: the match silently degrades to an open waiting
    # match with a shareable code -- the creator gets no error.
    assert created["status"] == "waiting"

    match = main.in_memory_matches[created["match_id"]]
    assert match["player2_id"] is None
    # QUIRK: player2_username is stored even though the user does not exist,
    # so the waiting match carries a bogus username until someone joins.
    assert match["player2_username"] == "NoSuchPlayer"


def test_join_does_not_refresh_stale_player2_username(
    client, known_users, auth_headers
):
    # Follow-on from the quirk above: when a different player joins the
    # degraded match by code, join_friend_match only updates player2_id/elo
    # and status -- player2_username keeps the bogus name from creation.
    created = _create(
        client, auth_headers, PLAYER_A, body={"opponent_username": "NoSuchPlayer"}
    )
    assert _join(client, auth_headers, created["match_code"], PLAYER_C).status_code == 200

    match = main.in_memory_matches[created["match_id"]]
    assert str(match["player2_id"]) == PLAYER_C
    assert match["player2_username"] == "NoSuchPlayer"  # stale, never corrected


def test_challenge_username_lookup_is_case_sensitive(
    client, known_users, auth_headers
):
    # Mongo's default collation is case-sensitive and create_friend_match
    # does an exact find_one, so "beekeeper" does not match "BeeKeeper":
    # instead of a pending challenge the creator silently gets an open match.
    created = _create(
        client,
        auth_headers,
        PLAYER_A,
        body={"opponent_username": REGISTERED_USERNAME.lower()},
    )
    assert created["status"] == "waiting"
    assert main.in_memory_matches[created["match_id"]]["player2_id"] is None


def test_create_with_empty_string_username_is_waiting(client, known_users, auth_headers):
    # Empty string is falsy, so it is treated like "no opponent given".
    created = _create(client, auth_headers, PLAYER_A, body={"opponent_username": ""})
    assert created["status"] == "waiting"
    assert main.in_memory_matches[created["match_id"]]["player2_username"] is None


# ---------------------------------------------------------------------------
# Outsider access to gameplay routes on a friend match
# ---------------------------------------------------------------------------


def test_outsider_cannot_touch_friend_match_gameplay(
    client, auth_headers, fixed_question
):
    created = _create(client, auth_headers, PLAYER_A)
    assert _join(client, auth_headers, created["match_code"], PLAYER_B).status_code == 200
    match_id = created["match_id"]

    # Prime a round as a legitimate player.
    assert (
        client.get(
            "/api/game/question",
            params={"match_id": match_id},
            headers=auth_headers(PLAYER_A),
        ).status_code
        == 200
    )

    outsider = auth_headers(PLAYER_C)
    assert (
        client.get(
            "/api/game/question", params={"match_id": match_id}, headers=outsider
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/game/answer",
            json={"match_id": match_id, "answer": "2*x"},
            headers=outsider,
        ).status_code
        == 403
    )
    assert (
        client.get(f"/api/game/status/{match_id}", headers=outsider).status_code == 403
    )
    assert (
        client.post(
            "/api/game/give-up", params={"match_id": match_id}, headers=outsider
        ).status_code
        == 403
    )


def test_outsider_cannot_use_match_by_code_lookup(client, auth_headers):
    created = _create(client, auth_headers, PLAYER_A)
    assert _join(client, auth_headers, created["match_code"], PLAYER_B).status_code == 200

    response = client.get(
        f"/api/game/match/{created['match_code']}", headers=auth_headers(PLAYER_C)
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# /api/game/match/{match_code} (get_match_by_code)
# ---------------------------------------------------------------------------


def test_get_match_by_code_for_active_match(client, auth_headers):
    created = _create(client, auth_headers, PLAYER_A)
    assert _join(client, auth_headers, created["match_code"], PLAYER_B).status_code == 200

    response = client.get(
        f"/api/game/match/{created['match_code']}", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["match_id"] == created["match_id"]
    assert body["status"] == "active"
    assert body["is_player1"] is True
    assert body["player1_id"] == PLAYER_A
    assert body["player2_id"] == PLAYER_B
    # Guest opponents are labelled "Guest" and never flagged as bots.
    assert body["opponent_name"] == "Guest"
    assert body["is_opponent_bot"] is False


def test_get_match_by_code_perspective_of_player2(client, auth_headers):
    created = _create(client, auth_headers, PLAYER_A)
    assert _join(client, auth_headers, created["match_code"], PLAYER_B).status_code == 200

    response = client.get(
        f"/api/game/match/{created['match_code']}", headers=auth_headers(PLAYER_B)
    )
    assert response.status_code == 200
    assert response.json()["is_player1"] is False


def test_get_match_by_code_unknown_code_404(client, auth_headers):
    response = client.get("/api/game/match/NOPE42", headers=auth_headers(PLAYER_A))
    assert response.status_code == 404


def test_get_match_by_code_waiting_match_reports_stringified_none(
    client, auth_headers
):
    # QUIRK: for a waiting match player2_id is None and the endpoint returns
    # the *string* "None" (str(None)), which clients must special-case.
    created = _create(client, auth_headers, PLAYER_A)

    response = client.get(
        f"/api/game/match/{created['match_code']}", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "waiting"
    assert body["player2_id"] == "None"


def test_get_match_by_code_accepts_lowercase_code(client, auth_headers):
    created = _create(client, auth_headers, PLAYER_A)
    assert _join(client, auth_headers, created["match_code"], PLAYER_B).status_code == 200

    response = client.get(
        f"/api/game/match/{created['match_code'].lower()}",
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# End-to-end sanity: join then immediately play
# ---------------------------------------------------------------------------


def test_joined_match_is_immediately_playable(client, auth_headers, fixed_question):
    created = _create(client, auth_headers, PLAYER_A)
    assert _join(client, auth_headers, created["match_code"], PLAYER_B).status_code == 200
    match_id = created["match_id"]

    q_a = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    q_b = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_B),
    )
    assert q_a.status_code == 200 and q_b.status_code == 200
    assert q_a.json()["round_id"] == q_b.json()["round_id"]

    answer = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_B),
    )
    assert answer.status_code == 200
    assert answer.json()["correct"] is True
    assert str(answer.json()["round_winner"]) == PLAYER_B
