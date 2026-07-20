"""
Edge-case tests for how caller *identity* — as decided by
``get_current_user`` in main.py — shapes people-vs-people matchmaking and
match ownership.

Everything here hinges on the demo-mode auth resolver (main.py lines
~242-310).  Its contract, read carefully, is:

1. If an ``Authorization: Bearer <token>`` is present and the token starts
   with ``"guest-"``, the caller *is* that guest id verbatim
   (``_id == token``).  No verification, no DB lookup.
2. Otherwise, with **no** credentials at all, the caller is the single
   shared fallback identity ``"guest-user-id"``.
3. Otherwise a JWT decode is attempted.  A token that again starts with
   ``"guest-"`` is (redundantly) treated as that guest; a valid JWT whose
   ``sub`` email resolves to a Mongo user becomes that user (an ObjectId
   ``_id``); and **every** failure mode — missing ``sub``, unknown email,
   bad signature, expired token, wrong scheme — silently falls back to the
   shared ``"guest-user-id"``.

The consequences for matching are subtle and are what these tests pin:

- ``"guest-user-id"`` itself starts with ``"guest-"``, so the explicit
  token ``Bearer guest-user-id`` and the no-auth fallback collapse into one
  identity (cases 1/3).
- Any two anonymous / malformed-credential callers therefore share one
  queue slot and can never match each other, while two *distinct* explicit
  guest tokens are two identities that pair normally — even when they
  belong to the same physical person in two tabs (cases 2/4/9).
- Ownership is bound to the exact token: change it mid-match and you are an
  outsider on your own match (case 8).
- Registered (ObjectId) identities and guest (string) identities coexist;
  gameplay routes ``str()``-compare ids while the ownership checks in
  join/accept compare raw, so ObjectId-vs-string handling differs by route
  (cases 6/7/10).

Known bugs / quirks are documented with comments (and strict ``xfail``
where a test pins a defect) rather than by changing main.py.  See
MATCH_EDGE_CASE_REPORT.md for the consolidated write-up.
"""

import copy
from datetime import datetime, timedelta, timezone

import pytest

import main
from bson import ObjectId


# ---------------------------------------------------------------------------
# identities
# ---------------------------------------------------------------------------

GUEST_A = "guest-identity-aaa"
GUEST_B = "guest-identity-bbb"
GUEST_C = "guest-identity-ccc"
SHARED_GUEST = "guest-user-id"  # the no-auth fallback id

CORRECT = "2*x"  # equivalent to fixed_question's stored answer "2·x"

# A registered (Google-signed-in) user: JWT sub -> Mongo doc with ObjectId _id.
REG_EMAIL = "alice@example.com"
REG_OID = ObjectId("64b7f0c0c0c0c0c0c0c0c0c0")
REG_USERNAME = "AliceR"

INVITEE_EMAIL = "bob@example.com"
INVITEE_OID = ObjectId("64b7f0c0c0c0c0c0c0c0c0c1")
INVITEE_USERNAME = "BobR"


REGISTRY = {
    REG_EMAIL: {
        "_id": REG_OID,
        "email": REG_EMAIL,
        "name": "Alice R",
        "username": REG_USERNAME,
        "elo": 1200,
        "wins": 0,
        "losses": 0,
    },
    INVITEE_EMAIL: {
        "_id": INVITEE_OID,
        "email": INVITEE_EMAIL,
        "name": "Bob R",
        "username": INVITEE_USERNAME,
        "elo": 1300,
        "wins": 0,
        "losses": 0,
    },
}


# ---------------------------------------------------------------------------
# header helpers
# ---------------------------------------------------------------------------


def _guest(token):
    return {"Authorization": f"Bearer {token}"}


def _jwt_header(email):
    return {"Authorization": f"Bearer {main.create_access_token({'sub': email})}"}


# ---------------------------------------------------------------------------
# fake collections
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        docs = [copy.deepcopy(d) for d in self._docs]
        return docs if length is None else docs[:length]


class FakeMatchesDB:
    """Minimal Mongo stand-in supporting the flat-equality queries and
    ``$set`` updates the friend/challenge endpoints issue."""

    def __init__(self):
        self.docs = {}

    @staticmethod
    def _matches(doc, query):
        return all(doc.get(k) == v for k, v in (query or {}).items())

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = copy.deepcopy(doc)

    async def find_one(self, query, *args, **kwargs):
        for doc in self.docs.values():
            if self._matches(doc, query):
                return copy.deepcopy(doc)
        return None

    def find(self, query=None, *args, **kwargs):
        hits = [copy.deepcopy(d) for d in self.docs.values() if self._matches(d, query)]
        return _Cursor(hits)

    async def update_one(self, query, update, *args, **kwargs):
        for doc in self.docs.values():
            if self._matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                break
        return type("R", (), {"modified_count": 1, "matched_count": 1, "upserted_id": None})()

    async def delete_one(self, query, *args, **kwargs):
        for mid, doc in list(self.docs.items()):
            if self._matches(doc, query):
                del self.docs[mid]
                break
        return None


@pytest.fixture
def registered_users(mock_mongo, monkeypatch):
    """Resolve users_collection.find_one by email, _id (ObjectId) or username,
    exactly like the queries get_current_user / the match routes issue."""

    async def find_one(query, *args, **kwargs):
        if "email" in query:
            return copy.deepcopy(REGISTRY.get(query["email"]))
        if "username" in query:
            for user in REGISTRY.values():
                if user["username"] == query["username"]:
                    return copy.deepcopy(user)
            return None
        if "_id" in query:
            for user in REGISTRY.values():
                if user["_id"] == query["_id"]:
                    return copy.deepcopy(user)
            return None
        return None

    monkeypatch.setattr(main.users_collection, "find_one", find_one)
    return REGISTRY


@pytest.fixture
def matches_db(mock_mongo, monkeypatch):
    db = FakeMatchesDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.matches_collection, method, getattr(db, method))
    return db


# ---------------------------------------------------------------------------
# small action helpers
# ---------------------------------------------------------------------------


def _start(client, headers=None, mode="random"):
    return client.post("/api/game/start", json={"mode": mode}, headers=headers)


def _me(client, headers=None):
    return client.get("/api/user/me", headers=headers)


def _friend_match(client, auth_headers, p1=GUEST_A, p2=GUEST_B):
    created = client.post("/api/game/friend/create", json={}, headers=auth_headers(p1))
    assert created.status_code == 200, created.text
    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": created.json()["match_code"]},
        headers=auth_headers(p2),
    )
    assert joined.status_code == 200, joined.text
    return created.json()["match_id"]


def _question(client, match_id, headers):
    return client.get(
        "/api/game/question", params={"match_id": match_id}, headers=headers
    )


def _answer(client, match_id, headers, answer=CORRECT):
    return client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=headers,
    )


def _status(client, match_id, headers):
    return client.get(f"/api/game/status/{match_id}", headers=headers)


def _give_up(client, match_id, headers):
    return client.post(
        "/api/game/give-up", params={"match_id": match_id}, headers=headers
    )


# ===========================================================================
# Case 1: anonymous / no-auth callers share "guest-user-id" and cannot match
# ===========================================================================


def test_anonymous_caller_is_the_shared_guest_user_id(client):
    body = _me(client).json()
    assert body["id"] == SHARED_GUEST


def test_two_anonymous_callers_share_one_queue_slot_and_never_match(client):
    first = _start(client)
    assert first.json()["status"] == "searching"
    assert SHARED_GUEST in main.matchmaking_queue
    assert len(main.matchmaking_queue) == 1

    # A "second" anonymous browser is indistinguishable from the first, so it
    # lands on the same queue slot -- a self-match is impossible.
    second = _start(client)
    assert second.json()["status"] == "searching"
    assert len(main.matchmaking_queue) == 1


def test_anonymous_pair_cannot_form_a_ranked_match(client):
    _start(client)
    again = _start(client)
    # No match was ever created for the shared identity.
    assert again.json()["status"] == "searching"
    assert main.in_memory_matches == {}


# ===========================================================================
# Case 2: two explicit distinct guest tokens are two identities that match
# ===========================================================================


def test_distinct_guest_tokens_are_distinct_identities(client):
    assert _me(client, _guest(GUEST_A)).json()["id"] == GUEST_A
    assert _me(client, _guest(GUEST_B)).json()["id"] == GUEST_B


def test_two_distinct_guests_pair_into_a_ranked_match(client):
    assert _start(client, _guest(GUEST_A)).json()["status"] == "searching"
    matched = _start(client, _guest(GUEST_B)).json()
    assert matched["status"] == "matched"

    match = main.in_memory_matches[matched["match_id"]]
    assert {str(match["player1_id"]), str(match["player2_id"])} == {GUEST_A, GUEST_B}
    assert match["match_type"] == "ranked"


# ===========================================================================
# Case 3: explicit "Bearer guest-user-id" collides with the no-auth fallback
# ===========================================================================


def test_explicit_guest_user_id_token_matches_no_auth_identity(client):
    # "guest-user-id" itself starts with "guest-", so branch 1 returns it
    # verbatim -- the same _id the no-auth fallback produces.
    assert _me(client, _guest(SHARED_GUEST)).json()["id"] == SHARED_GUEST
    assert _me(client).json()["id"] == SHARED_GUEST


def test_anonymous_then_explicit_guest_user_id_is_one_queue_slot(client):
    assert _start(client).json()["status"] == "searching"  # no auth
    # Explicit Bearer guest-user-id resolves to the SAME identity, so this is
    # a self-match attempt, not a pairing.
    again = _start(client, _guest(SHARED_GUEST)).json()
    assert again["status"] == "searching"
    assert len(main.matchmaking_queue) == 1


def test_explicit_guest_user_id_can_match_a_different_guest(client):
    # The shared id is only "stuck" against other anonymous callers; a
    # genuinely different guest token pairs with it normally.
    assert _start(client, _guest(SHARED_GUEST)).json()["status"] == "searching"
    matched = _start(client, _guest(GUEST_A)).json()
    assert matched["status"] == "matched"
    match = main.in_memory_matches[matched["match_id"]]
    assert {str(match["player1_id"]), str(match["player2_id"])} == {SHARED_GUEST, GUEST_A}


# ===========================================================================
# Case 4: invalid JWT -> guest fallback, and its matchability
# ===========================================================================


def test_invalid_jwt_falls_back_to_shared_guest(client):
    body = _me(client, {"Authorization": "Bearer not-a-real-jwt"}).json()
    assert body["id"] == SHARED_GUEST


def test_two_different_bad_tokens_collapse_to_one_identity_and_cannot_match(client):
    assert _start(client, {"Authorization": "Bearer garbage-one"}).json()["status"] == "searching"
    # A *different* malformed token still decodes to the same fallback id, so
    # the two "players" cannot match each other.
    again = _start(client, {"Authorization": "Bearer garbage-two-different"}).json()
    assert again["status"] == "searching"
    assert list(main.matchmaking_queue) == [SHARED_GUEST]


def test_wrong_scheme_and_empty_bearer_also_fall_back_to_guest(client):
    # Basic scheme (not Bearer) -> HTTPBearer yields no credentials -> fallback.
    assert _me(client, {"Authorization": "Basic dXNlcjpwYXNz"}).json()["id"] == SHARED_GUEST
    # Bearer with an empty token -> credentials present but blank, JWT decode
    # fails -> fallback.
    assert _me(client, {"Authorization": "Bearer "}).json()["id"] == SHARED_GUEST


def test_bad_token_can_still_match_a_real_guest(client):
    # The fallback identity behaves like any guest against a distinct token.
    assert _start(client, {"Authorization": "Bearer still-not-valid"}).json()["status"] == "searching"
    matched = _start(client, _guest(GUEST_A)).json()
    assert matched["status"] == "matched"
    match = main.in_memory_matches[matched["match_id"]]
    assert SHARED_GUEST in {str(match["player1_id"]), str(match["player2_id"])}


# ===========================================================================
# Case 5: username challenges require a REGISTERED user in the (fake) DB
# ===========================================================================


def test_challenge_to_registered_username_creates_pending(
    client, registered_users, matches_db, auth_headers
):
    created = client.post(
        "/api/game/friend/create",
        json={"opponent_username": INVITEE_USERNAME},
        headers=auth_headers(GUEST_A),
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "pending"

    match = main.in_memory_matches[created.json()["match_id"]]
    # The invitee is pinned by their ObjectId, resolved from the users table.
    assert match["player2_id"] == INVITEE_OID
    assert match["player2_username"] == INVITEE_USERNAME


def test_challenge_to_unknown_username_degrades_to_waiting(
    client, registered_users, matches_db, auth_headers
):
    created = client.post(
        "/api/game/friend/create",
        json={"opponent_username": "NoSuchAccount"},
        headers=auth_headers(GUEST_A),
    )
    assert created.status_code == 200
    # No user by that name -> silently becomes an open, code-joinable match.
    assert created.json()["status"] == "waiting"
    assert main.in_memory_matches[created.json()["match_id"]]["player2_id"] is None


def test_guest_display_names_are_not_registered_usernames(
    client, registered_users, matches_db, auth_headers
):
    # A guest's on-screen name ("Guest xxxx") is NOT a username in the users
    # collection, so you cannot challenge another guest by it: the lookup
    # misses and the challenge degrades to a waiting match.
    guest_display_name = f"Guest {GUEST_B[-4:]}"
    created = client.post(
        "/api/game/friend/create",
        json={"opponent_username": guest_display_name},
        headers=auth_headers(GUEST_A),
    )
    assert created.json()["status"] == "waiting"
    assert main.in_memory_matches[created.json()["match_id"]]["player2_id"] is None


# ===========================================================================
# Case 6: ObjectId vs string id comparison in join / accept / ownership checks
# ===========================================================================


def test_objectid_equals_its_hex_string_only_after_str():
    # The crux behind the route-by-route divergence below: an ObjectId and its
    # own hex string are equal ONLY once both are str()'d.  Routes that
    # str()-compare (question/answer/give-up/status) bridge the two forms;
    # routes that compare raw (join "own match", accept, cancel) do not.
    hex_id = str(REG_OID)
    assert str(REG_OID) == hex_id
    assert (REG_OID == hex_id) is False


def test_registered_user_cannot_join_their_own_waiting_match(
    client, registered_users, matches_db
):
    # create stores player1_id as an ObjectId; join's "cannot join your own
    # match" check compares raw (==), and ObjectId == ObjectId holds, so the
    # creator is correctly blocked from joining their own code.
    created = client.post(
        "/api/game/friend/create", json={}, headers=_jwt_header(REG_EMAIL)
    )
    code = created.json()["match_code"]
    assert main.in_memory_matches[created.json()["match_id"]]["player1_id"] == REG_OID

    rejoin = client.post(
        "/api/game/friend/join", json={"match_code": code}, headers=_jwt_header(REG_EMAIL)
    )
    assert rejoin.status_code == 400
    assert rejoin.json()["detail"] == "Cannot join your own match"


def test_registered_invitee_can_accept_objectid_challenge(
    client, registered_users, matches_db, auth_headers
):
    created = client.post(
        "/api/game/friend/create",
        json={"opponent_username": INVITEE_USERNAME},
        headers=auth_headers(GUEST_A),
    )
    match_id = created.json()["match_id"]

    # accept compares match["player2_id"] (ObjectId) against current_user["_id"]
    # RAW; the invitee's JWT resolves to the same ObjectId, so equality holds.
    accepted = client.post(
        f"/api/challenges/accept/{match_id}", headers=_jwt_header(INVITEE_EMAIL)
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "active"


def test_guest_cannot_accept_an_objectid_invitee_challenge(
    client, registered_users, matches_db, auth_headers
):
    # A guest identity ("guest-...") can never equal the ObjectId player2_id
    # under the raw != check, so no guest can hijack a registered challenge.
    created = client.post(
        "/api/game/friend/create",
        json={"opponent_username": INVITEE_USERNAME},
        headers=auth_headers(GUEST_A),
    )
    match_id = created.json()["match_id"]

    rejected = client.post(
        f"/api/challenges/accept/{match_id}", headers=auth_headers(GUEST_C)
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "Not your challenge to accept"


def test_gameplay_str_compare_admits_the_objectid_owner(
    client, registered_users, fixed_question, auth_headers
):
    # A friend match whose player1 is a registered ObjectId user: the owner's
    # own JWT is admitted to gameplay because the route str()-compares ids.
    created = client.post(
        "/api/game/friend/create", json={}, headers=_jwt_header(REG_EMAIL)
    )
    match_id = created.json()["match_id"]
    client.post(
        "/api/game/friend/join",
        json={"match_code": created.json()["match_code"]},
        headers=auth_headers(GUEST_B),
    )

    q = _question(client, match_id, _jwt_header(REG_EMAIL))
    assert q.status_code == 200, q.text
    body = _answer(client, match_id, _jwt_header(REG_EMAIL)).json()
    assert body["correct"] is True
    assert main.in_memory_matches[match_id]["player1_score"] == 1


# ===========================================================================
# Case 7: player1_id ObjectId vs guest string in status comparisons
# ===========================================================================


def _seed_mixed_match(match_id="match-mixed-1", p1=REG_OID, p2=GUEST_B):
    """A match whose player1 is an ObjectId and player2 is a guest string."""
    doc = {
        "_id": match_id,
        "match_code": "MIXED1",
        "match_type": "friend",
        "player1_id": p1,
        "player2_id": p2,
        "player1_score": 2,
        "player2_score": 1,
        "player1_elo": 1200,
        "player2_elo": 1000,
        "status": "active",
        "winner_id": None,
        "elo_change": 0,
        "created_at": datetime.utcnow(),
    }
    main.in_memory_matches[match_id] = doc
    return match_id


def test_status_admits_the_objectid_player_via_jwt(client, registered_users):
    match_id = _seed_mixed_match()
    body = _status(client, match_id, _jwt_header(REG_EMAIL)).json()
    # ids are stringified in the payload; the ObjectId owner is admitted.
    assert body["player1_id"] == str(REG_OID)
    assert body["player2_id"] == GUEST_B
    assert body["player1_score"] == 2 and body["player2_score"] == 1


def test_status_admits_the_guest_player_of_a_mixed_match(client, registered_users):
    match_id = _seed_mixed_match()
    resp = _status(client, match_id, _guest(GUEST_B))
    assert resp.status_code == 200
    assert resp.json()["player2_id"] == GUEST_B


def test_outsider_guest_is_403_on_a_mixed_id_match(client, registered_users):
    match_id = _seed_mixed_match()
    resp = _status(client, match_id, _guest(GUEST_C))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Not your match"


def test_anonymous_caller_is_outsider_unless_the_guest_is_shared_id(client, registered_users):
    # An anonymous caller is "guest-user-id".  On a match owned by REG_OID +
    # GUEST_B they are an outsider (403); only if the shared id were literally
    # a participant would they be admitted.
    match_id = _seed_mixed_match(p2=GUEST_B)
    assert _status(client, match_id, None).status_code == 403

    shared_match = _seed_mixed_match(match_id="match-mixed-2", p2=SHARED_GUEST)
    assert _status(client, shared_match, None).status_code == 200


# ===========================================================================
# Case 8: changing Authorization mid-match makes you an outsider on your match
# ===========================================================================


def test_changing_token_mid_match_locks_the_player_out(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers, GUEST_A, GUEST_B)
    # GUEST_A starts a round on their own match...
    assert _question(client, match_id, auth_headers(GUEST_A)).status_code == 200

    # ...then their token changes (new guest id after a storage wipe / re-login).
    # Every gameplay route now sees a stranger.
    assert _question(client, match_id, auth_headers(GUEST_C)).status_code == 403
    assert _answer(client, match_id, auth_headers(GUEST_C)).status_code == 403
    assert _give_up(client, match_id, auth_headers(GUEST_C)).status_code == 403
    assert _status(client, match_id, auth_headers(GUEST_C)).status_code == 403


def test_original_token_still_owns_the_match_after_the_switch(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers, GUEST_A, GUEST_B)
    _question(client, match_id, auth_headers(GUEST_A))

    # The stranger token is rejected...
    assert _answer(client, match_id, auth_headers(GUEST_C)).status_code == 403
    # ...but the original identity keeps full ownership and can still score.
    body = _answer(client, match_id, auth_headers(GUEST_A)).json()
    assert body["correct"] is True
    assert main.in_memory_matches[match_id]["player1_score"] == 1


def test_switching_to_no_auth_mid_match_is_also_an_outsider(
    client, auth_headers, fixed_question
):
    # Dropping the token entirely resolves to "guest-user-id", which is not a
    # participant, so an anonymous request cannot act on the match either.
    match_id = _friend_match(client, auth_headers, GUEST_A, GUEST_B)
    _question(client, match_id, auth_headers(GUEST_A))
    assert _status(client, match_id, None).status_code == 403


# ===========================================================================
# Case 9: one physical person with two guest tokens = two identities that
#         can match each OTHER (the server cannot detect self-play)
# ===========================================================================


def test_same_person_two_tokens_match_against_themselves(client):
    # Two browser tabs for one human each mint a distinct guest id.  To the
    # backend they are two players, so they pair -- an integrity gap: a user
    # can farm a match against themselves.
    assert _start(client, _guest(GUEST_A)).json()["status"] == "searching"
    matched = _start(client, _guest(GUEST_B)).json()
    assert matched["status"] == "matched"
    match = main.in_memory_matches[matched["match_id"]]
    assert {str(match["player1_id"]), str(match["player2_id"])} == {GUEST_A, GUEST_B}


def test_two_tokens_occupy_two_distinct_queue_slots(client):
    _start(client, _guest(GUEST_A))
    # Before the second tab arrives there is exactly one slot; the identities
    # are keyed independently, unlike the shared-guest anonymous case.
    assert list(main.matchmaking_queue) == [GUEST_A]
    _start(client, _guest(GUEST_C))  # pairs A+C, leaving the queue empty
    assert main.matchmaking_queue == {}


def test_same_person_can_play_a_full_self_match_to_completion(
    client, auth_headers, fixed_question
):
    # The self-match is fully playable: the "person" alternates tokens and
    # feeds points to whichever identity answers.
    match_id = _friend_match(client, auth_headers, GUEST_A, GUEST_B)
    for _ in range(3):
        _question(client, match_id, auth_headers(GUEST_A))
        won = _answer(client, match_id, auth_headers(GUEST_A)).json()
        assert won["correct"] is True
    assert main.in_memory_matches[match_id]["status"] == "completed"
    assert str(main.in_memory_matches[match_id]["winner_id"]) == GUEST_A


# ===========================================================================
# Case 10: get_current_user resolution edge cases that decide match ownership
# ===========================================================================


def test_valid_jwt_resolves_to_the_db_user_identity(client, registered_users):
    body = _me(client, _jwt_header(REG_EMAIL)).json()
    assert body["id"] == str(REG_OID)
    assert body["email"] == REG_EMAIL
    assert body["elo"] == 1200


def test_jwt_without_sub_falls_back_to_shared_guest(client, registered_users):
    # A token carrying no "sub" claim decodes fine but yields email=None ->
    # fallback identity, so it owns nothing a real account would.
    token = main.jwt.encode(
        {"exp": datetime.utcnow() + timedelta(minutes=5)},
        main.SECRET_KEY,
        algorithm=main.ALGORITHM,
    )
    body = _me(client, {"Authorization": f"Bearer {token}"}).json()
    assert body["id"] == SHARED_GUEST


def test_jwt_with_email_absent_from_db_falls_back_to_shared_guest(client, registered_users):
    body = _me(client, _jwt_header("ghost@example.com")).json()
    assert body["id"] == SHARED_GUEST


def test_expired_jwt_falls_back_to_shared_guest(client, registered_users):
    expired = main.jwt.encode(
        {"sub": REG_EMAIL, "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        main.SECRET_KEY,
        algorithm=main.ALGORITHM,
    )
    body = _me(client, {"Authorization": f"Bearer {expired}"}).json()
    # Even a well-formed token for a real user, once expired, drops to guest.
    assert body["id"] == SHARED_GUEST


def test_jwt_signed_with_wrong_secret_falls_back_to_shared_guest(client, registered_users):
    forged = main.jwt.encode(
        {"sub": REG_EMAIL, "exp": datetime.utcnow() + timedelta(minutes=5)},
        "a-totally-different-secret",
        algorithm=main.ALGORITHM,
    )
    body = _me(client, {"Authorization": f"Bearer {forged}"}).json()
    assert body["id"] == SHARED_GUEST


def test_registered_owner_and_expired_token_do_not_share_a_match(
    client, registered_users, fixed_question, auth_headers
):
    # Ownership follows the *resolved* identity, not the email in the token:
    # a registered user creates a match, but a request bearing that same
    # user's EXPIRED token resolves to "guest-user-id" and is an outsider.
    created = client.post(
        "/api/game/friend/create", json={}, headers=_jwt_header(REG_EMAIL)
    )
    match_id = created.json()["match_id"]
    client.post(
        "/api/game/friend/join",
        json={"match_code": created.json()["match_code"]},
        headers=auth_headers(GUEST_B),
    )

    expired = main.jwt.encode(
        {"sub": REG_EMAIL, "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        main.SECRET_KEY,
        algorithm=main.ALGORITHM,
    )
    resp = _status(client, match_id, {"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 403


def test_no_credentials_shares_ownership_across_all_anonymous_callers(
    client, fixed_question
):
    # Anything created anonymously is owned by the shared guest, so a *second*
    # anonymous caller inherits full access to it -- there is no per-session
    # isolation for tokenless users.
    created = client.post("/api/game/friend/create", json={})
    match_id = created.json()["match_id"]
    # A different anonymous browser is the same identity -> can join is blocked
    # ("cannot join your own match"), confirming shared ownership.
    rejoin = client.post(
        "/api/game/friend/join", json={"match_code": created.json()["match_code"]}
    )
    assert rejoin.status_code == 400
    assert rejoin.json()["detail"] == "Cannot join your own match"
