"""
Edge-case tests for ObjectId-vs-guest-string id *mixing* when people match.

Registered (Google/JWT) users live in the users collection with an
``ObjectId`` ``_id``; guests are bare ``"guest-..."`` strings minted by
``get_current_user``.  People matches freely mix the two: the matchmaking
queue keys everyone by ``str(_id)``, ``start_match`` converts a queued hex
string *back* into an ``ObjectId`` (``ObjectId(user_id) if
ObjectId.is_valid(user_id) else user_id``), friend/challenge docs store the
raw ``current_user["_id"]``, gameplay routes compare ``str()``-to-``str()``,
and the challenge accept/cancel routes compare raw.  This suite pins every
seam of that mixing:

1.  Registered ObjectId user queues, a guest joins (hex queue key, ObjectId
    round-trip, live-ELO read for the ObjectId side only).
2.  Guest queues, an ObjectId user joins (frozen elo=1000 for the guest,
    string cancel-flag set still bridges the ObjectId identity).
3.  Both-ObjectId concurrent pairing race — deepens the known
    BUG(pairing-race) xfail from tests/test_ranked_matchmaking_edge_cases.py:
    both racers report "matched" and the queued player ends up with a ghost
    second match that /api/game/active never surfaces.
4.  ObjectId player1 vs guest player2 in friend-match gameplay: the str()
    comparisons route round wins, give-up flags and completion correctly.
5.  Challenge accept with an ObjectId player2_id vs the caller's raw _id:
    ObjectId invitee accepts, lookalike guests cannot, and a challenge doc
    whose player2_id degraded to the hex *string* (JSON round-trip /
    external write) locks out its own invitee (strict xfail) while the very
    same credential is admitted by the str()-bridging gameplay routes.
6.  Cancel-challenge raw id comparisons (guest==guest and ObjectId==ObjectId
    work; hex-string-vs-ObjectId does not — same root cause as case 5).
7.  get_active_match with ObjectId ids: the raw-_id opponent lookup resolves
    the registered side but rediscovers
    BUG(active-mislabels-humans-as-bot) for the guest side of a RANKED
    human-vs-human match.
8.  Match details / status / by-code payloads with mixed types: ids are
    stringified, the ObjectId side resolves to a username, the guest side
    falls back to placeholders ("Player 2" / "Guest").
9.  ELO update on ranked completion targets the correct ObjectId doc (a real
    ObjectId in the update filter, never the hex string); the guest side is
    a silent users-collection no-op, so mixed matches apply rating changes
    to only one side.
10. A guest token embedding a full ObjectId hex ("guest-<24hex>") stays a
    plain string identity everywhere: no ObjectId coercion, no suffix
    cross-crediting, no access to the real user's matches.

Known bugs are pinned with strict ``xfail`` plus current-behavior sibling
tests, matching the campaign convention.  See MATCH_EDGE_CASE_REPORT.md.
"""

import asyncio
import copy
from datetime import datetime

import pytest
from bson import ObjectId

import main


# ---------------------------------------------------------------------------
# identities
# ---------------------------------------------------------------------------

GUEST_A = "guest-mixing-alpha"
GUEST_B = "guest-mixing-second"

REG_EMAIL = "alice.mixing@example.com"
REG_OID = ObjectId("64c0ffee0000000000000001")
REG_USERNAME = "AliceMix"
REG_ELO = 1200

INVITEE_EMAIL = "bob.mixing@example.com"
INVITEE_OID = ObjectId("64c0ffee0000000000000002")
INVITEE_USERNAME = "BobMix"

# A guest whose token embeds the registered user's full 24-hex ObjectId.
HEXY_GUEST = f"guest-{REG_OID}"
# ...and one embedding the invitee's hex, for the challenge-accept probe.
HEXY_INVITEE_GUEST = f"guest-{INVITEE_OID}"

CORRECT = "2*x"  # equivalent to fixed_question's stored answer "2·x"


def _guest(token):
    return {"Authorization": f"Bearer {token}"}


def _jwt(email):
    return {"Authorization": f"Bearer {main.create_access_token({'sub': email})}"}


# ---------------------------------------------------------------------------
# fake collections
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, matched):
        self.matched_count = matched
        self.modified_count = matched
        self.upserted_id = None


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


def _doc_matches(doc, query):
    # Flat equality, like Mongo: ObjectId == its hex string is False.
    return all(doc.get(k) == v for k, v in (query or {}).items())


class FakeUsersDB:
    """users_collection stand-in resolving by _id / email / username and
    applying $set/$inc.  Records every update_one call so tests can assert
    *which id type* the ELO writes target."""

    def __init__(self):
        self.docs = {}
        self.update_calls = []  # list of (filter, update) tuples

    def add(self, doc):
        self.docs[doc["_id"]] = copy.deepcopy(doc)

    async def find_one(self, query, *args, **kwargs):
        for doc in self.docs.values():
            if _doc_matches(doc, query):
                return copy.deepcopy(doc)
        return None

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = copy.deepcopy(doc)

    async def update_one(self, query, update, *args, **kwargs):
        self.update_calls.append((copy.deepcopy(query), copy.deepcopy(update)))
        for doc in self.docs.values():
            if _doc_matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                for k, v in update.get("$inc", {}).items():
                    doc[k] = doc.get(k, 0) + v
                return _Result(1)
        # No upsert: an unmatched filter is a silent no-op, like real Mongo.
        return _Result(0)

    def find(self, query=None, *args, **kwargs):
        return _Cursor([copy.deepcopy(d) for d in self.docs.values()
                        if _doc_matches(d, query)])


class FakeMatchesDB:
    """matches_collection stand-in for the DB-only challenge endpoints."""

    def __init__(self):
        self.docs = {}

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = copy.deepcopy(doc)

    async def find_one(self, query, *args, **kwargs):
        for doc in self.docs.values():
            if _doc_matches(doc, query):
                return copy.deepcopy(doc)
        return None

    def find(self, query=None, *args, **kwargs):
        return _Cursor([copy.deepcopy(d) for d in self.docs.values()
                        if _doc_matches(d, query)])

    async def update_one(self, query, update, *args, **kwargs):
        for doc in self.docs.values():
            if _doc_matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                return _Result(1)
        return _Result(0)

    async def delete_one(self, query, *args, **kwargs):
        for mid, doc in list(self.docs.items()):
            if _doc_matches(doc, query):
                del self.docs[mid]
                break
        return None


@pytest.fixture
def users_db(mock_mongo, monkeypatch):
    db = FakeUsersDB()
    db.add({
        "_id": REG_OID,
        "email": REG_EMAIL,
        "name": "Alice Mixing",
        "username": REG_USERNAME,
        "elo": REG_ELO,
        "wins": 0,
        "losses": 0,
    })
    db.add({
        "_id": INVITEE_OID,
        "email": INVITEE_EMAIL,
        "name": "Bob Mixing",
        "username": INVITEE_USERNAME,
        "elo": 1100,
        "wins": 0,
        "losses": 0,
    })
    for method in ("find_one", "insert_one", "update_one", "find"):
        monkeypatch.setattr(main.users_collection, method, getattr(db, method))
    return db


@pytest.fixture
def matches_db(mock_mongo, monkeypatch):
    db = FakeMatchesDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.matches_collection, method, getattr(db, method))
    return db


# ---------------------------------------------------------------------------
# action helpers
# ---------------------------------------------------------------------------


def _start(client, headers=None):
    return client.post("/api/game/start", json={"mode": "random"}, headers=headers)


def _question(client, match_id, headers):
    return client.get("/api/game/question", params={"match_id": match_id}, headers=headers)


def _answer(client, match_id, headers, answer=CORRECT):
    return client.post(
        "/api/game/answer", json={"match_id": match_id, "answer": answer}, headers=headers
    )


def _status(client, match_id, headers):
    return client.get(f"/api/game/status/{match_id}", headers=headers)


def _win_round(client, match_id, headers):
    q = _question(client, match_id, headers)
    assert q.status_code == 200, q.text
    body = _answer(client, match_id, headers).json()
    assert body["correct"] is True
    return body


def _pair_ranked(client, first_headers, second_headers):
    """Queue the first identity, pair by starting as the second; returns the
    second caller's 'matched' payload."""
    searching = _start(client, first_headers).json()
    assert searching["status"] == "searching"
    matched = _start(client, second_headers).json()
    assert matched["status"] == "matched", matched
    return matched


def _friend_mixed(client):
    """Friend match with ObjectId player1 (registered JWT) and guest player2."""
    created = client.post("/api/game/friend/create", json={}, headers=_jwt(REG_EMAIL))
    assert created.status_code == 200, created.text
    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": created.json()["match_code"]},
        headers=_guest(GUEST_B),
    )
    assert joined.status_code == 200, joined.text
    return created.json()["match_id"]


# ===========================================================================
# Case 1: registered ObjectId user queues, a guest joins
# ===========================================================================


def test_registered_user_queues_under_hex_string_key(client, users_db):
    body = _start(client, _jwt(REG_EMAIL)).json()
    assert body["status"] == "searching"
    # start_match keys the queue by str(_id): the hex string, never ObjectId.
    assert str(REG_OID) in main.matchmaking_queue
    assert all(isinstance(k, str) for k in main.matchmaking_queue)
    assert main.matchmaking_queue[str(REG_OID)]["elo"] == REG_ELO


def test_guest_joiner_pairs_with_queued_objectid_user(client, users_db):
    matched = _pair_ranked(client, _jwt(REG_EMAIL), _guest(GUEST_A))
    match = main.in_memory_matches[matched["match_id"]]

    assert match["match_type"] == "ranked"
    # The guest caller becomes player1 as a plain string...
    assert match["player1_id"] == GUEST_A
    assert isinstance(match["player1_id"], str)
    # ...and the queued hex string is converted BACK into a real ObjectId.
    assert isinstance(match["player2_id"], ObjectId)
    assert match["player2_id"] == REG_OID
    # The joiner is told the registered opponent's username.
    assert matched["opponent"] == REG_USERNAME


def test_pairing_reads_live_elo_through_the_reconstructed_objectid(client, users_db):
    _start(client, _jwt(REG_EMAIL))
    # The registered player's rating moves while they wait in the queue.
    users_db.docs[REG_OID]["elo"] = 1250

    matched = _start(client, _guest(GUEST_A)).json()
    match = main.in_memory_matches[matched["match_id"]]
    # ObjectId opponents get a fresh users_collection read (queue snapshot
    # ignored); contrast with the guest side's frozen 1000 in case 2.
    assert match["player2_elo"] == 1250
    assert match["player2_elo"] != main.matchmaking_queue.get(str(REG_OID), {}).get("elo", 1200)


def test_both_mixed_players_pass_the_gameplay_ownership_gate(
    client, users_db, fixed_question
):
    matched = _pair_ranked(client, _jwt(REG_EMAIL), _guest(GUEST_A))
    match_id = matched["match_id"]
    # str()-comparisons bridge ObjectId-vs-string: both are admitted.
    assert _question(client, match_id, _guest(GUEST_A)).status_code == 200
    assert _question(client, match_id, _jwt(REG_EMAIL)).status_code == 200
    # An unrelated guest is still an outsider.
    assert _question(client, match_id, _guest("guest-mixing-outsider")).status_code == 403


# ===========================================================================
# Case 2: guest queues, an ObjectId user joins
# ===========================================================================


def test_objectid_joiner_becomes_player1_as_objectid(client, users_db):
    matched = _pair_ranked(client, _guest(GUEST_A), _jwt(REG_EMAIL))
    match = main.in_memory_matches[matched["match_id"]]

    # The caller's str(_id) hex is round-tripped back into an ObjectId.
    assert isinstance(match["player1_id"], ObjectId)
    assert match["player1_id"] == REG_OID
    assert match["player2_id"] == GUEST_A
    # Guests have no user doc: elo is the hard-coded 1000 fallback and the
    # joiner is told they matched a generic "Player".
    assert match["player2_elo"] == 1000
    assert matched["opponent"] == "Player"


def test_cancel_flag_set_of_strings_bridges_the_objectid_identity(client, users_db):
    # cancel_matchmaking stores str(_id) in cancelled_users; the pairing path
    # checks the same str() form, so an ObjectId user's cancel is honored.
    client.post("/api/game/cancel", headers=_jwt(REG_EMAIL))
    assert str(REG_OID) in main.cancelled_users

    _start(client, _guest(GUEST_A))
    body = _start(client, _jwt(REG_EMAIL)).json()
    assert body["status"] == "cancelled"
    assert main.in_memory_matches == {}


# ===========================================================================
# Case 3: both-ObjectId concurrent pairing race (deepens BUG(pairing-race))
# ===========================================================================
#
# tests/test_ranked_matchmaking_edge_cases.py already pins that two
# concurrent joiners can both pair with the same queued ObjectId player
# (start_match awaits users_collection.find_one BETWEEN selecting the
# opponent and popping them).  Deepened here: BOTH racers get a "matched"
# response, and the queued player's /api/game/active only ever surfaces the
# first match -- the second racer waits forever inside a ghost match their
# opponent cannot discover.


QUEUED_OID = ObjectId("64c0ffee00000000000000aa")


def _race_two_objectid_joiners(monkeypatch):
    async def yielding_find_one(*args, **kwargs):
        await asyncio.sleep(0)  # force interleaving at the await point
        return None

    monkeypatch.setattr(main.users_collection, "find_one", yielding_find_one)
    monkeypatch.setattr(main.matches_collection, "find_one", yielding_find_one)

    main.matchmaking_queue[str(QUEUED_OID)] = {
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

    return asyncio.run(run())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(pairing-race) deepened: with an ObjectId player queued, two "
        "concurrent joiners both select them before either pops the queue "
        "entry (the users_collection.find_one await is the race window), so "
        "BOTH racers are told 'matched' against the same single opponent.  "
        "Exactly one should match; the other should keep searching."
    ),
)
def test_concurrent_objectid_joiners_should_not_both_report_matched(
    mock_mongo, monkeypatch
):
    first, second = _race_two_objectid_joiners(monkeypatch)
    assert sorted([first["status"], second["status"]]) == ["matched", "searching"]


def test_current_behavior_double_pairing_leaves_a_ghost_match(
    mock_mongo, monkeypatch
):
    # BUG pin for the xfail above, plus the downstream ghost: the queued
    # player is in TWO active matches but /api/game/active returns only the
    # first, so the second racer's match can never be discovered by their
    # opponent.
    first, second = _race_two_objectid_joiners(monkeypatch)
    assert first["status"] == "matched" and second["status"] == "matched"
    assert first["match_id"] != second["match_id"]

    queued_matches = [
        mid
        for mid, m in main.in_memory_matches.items()
        if str(QUEUED_OID) in (str(m["player1_id"]), str(m["player2_id"]))
        and m["status"] == "active"
    ]
    assert len(queued_matches) == 2

    active = asyncio.run(
        main.get_active_match(current_user={"_id": QUEUED_OID, "elo": 1000})
    )
    assert active["has_active_match"] is True
    ghost = ({first["match_id"], second["match_id"]} - {active["match_id"]}).pop()
    # The ghost stays active and playable, invisible to the queued player.
    assert main.in_memory_matches[ghost]["status"] == "active"


# ===========================================================================
# Case 4: ObjectId player1 vs guest player2 in friend-match gameplay
# ===========================================================================


def test_friend_match_stores_mixed_id_types_side_by_side(client, users_db):
    match_id = _friend_mixed(client)
    match = main.in_memory_matches[match_id]
    # No normalization on the friend path: the raw ObjectId and the raw
    # guest string coexist in one document.
    assert isinstance(match["player1_id"], ObjectId)
    assert match["player1_id"] == REG_OID
    assert match["player2_id"] == GUEST_B
    assert match["status"] == "active"


def test_objectid_player1_round_win_credits_player1_only(
    client, users_db, fixed_question
):
    match_id = _friend_mixed(client)
    _win_round(client, match_id, _jwt(REG_EMAIL))

    match = main.in_memory_matches[match_id]
    assert match["player1_score"] == 1
    assert match["player2_score"] == 0
    # The round winner is stored as the raw ObjectId...
    round_doc = main.in_memory_rounds[match["current_round_id"]]
    assert isinstance(round_doc["winner_id"], ObjectId)
    # ...and stringified to the hex form in the status payload.
    body = _status(client, match_id, _guest(GUEST_B)).json()
    assert body["round_winner"] == str(REG_OID)


def test_guest_player2_round_win_credits_player2_only(
    client, users_db, fixed_question
):
    match_id = _friend_mixed(client)
    body = _win_round(client, match_id, _guest(GUEST_B))
    assert body["round_winner"] == GUEST_B

    match = main.in_memory_matches[match_id]
    assert match["player1_score"] == 0
    assert match["player2_score"] == 1


def test_mixed_friend_match_completes_for_guest_winner_with_zero_elo(
    client, users_db, fixed_question
):
    match_id = _friend_mixed(client)
    for _ in range(3):
        body = _win_round(client, match_id, _guest(GUEST_B))
    assert body["match_winner"] == GUEST_B
    assert body["elo_change"] == 0

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["winner_id"] == GUEST_B
    # Friend matches never touch ratings: no $inc ever reached the users
    # collection, so the ObjectId player's doc is untouched.
    assert all("$inc" not in update for _, update in users_db.update_calls)
    assert users_db.docs[REG_OID]["elo"] == REG_ELO


def test_giveup_maps_mixed_ids_to_the_correct_flags(
    client, users_db, fixed_question
):
    match_id = _friend_mixed(client)
    _question(client, match_id, _jwt(REG_EMAIL))

    first = client.post(
        "/api/game/give-up", params={"match_id": match_id}, headers=_jwt(REG_EMAIL)
    ).json()
    assert first["status"] == "gave_up"
    round_doc = main.in_memory_rounds[
        main.in_memory_matches[match_id]["current_round_id"]
    ]
    # is_player1 is decided by str() comparison, so the ObjectId caller maps
    # to player1_gave_up (not the guest's flag).
    assert round_doc["player1_gave_up"] is True
    assert round_doc["player2_gave_up"] is False

    second = client.post(
        "/api/game/give-up", params={"match_id": match_id}, headers=_guest(GUEST_B)
    ).json()
    assert second["status"] == "both_gave_up"
    assert second["round_winner"] == "tie"


# ===========================================================================
# Case 5: challenge accept — ObjectId player2_id vs the caller's raw _id
# ===========================================================================


def _challenge_to_invitee(client, matches_db, challenger_headers=None):
    created = client.post(
        "/api/game/friend/create",
        json={"opponent_username": INVITEE_USERNAME},
        headers=challenger_headers or _guest(GUEST_A),
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "pending"
    return created.json()["match_id"]


def test_challenge_by_username_pins_the_invitee_objectid(
    client, users_db, matches_db
):
    match_id = _challenge_to_invitee(client, matches_db)
    doc = matches_db.docs[match_id]
    # The username lookup resolves to the users doc, so player2_id is the
    # raw ObjectId next to the challenger's guest string.
    assert isinstance(doc["player2_id"], ObjectId)
    assert doc["player2_id"] == INVITEE_OID
    assert doc["player1_id"] == GUEST_A


def test_objectid_invitee_accepts_via_jwt(client, users_db, matches_db):
    match_id = _challenge_to_invitee(client, matches_db)
    # accept compares raw: ObjectId == ObjectId holds for the real invitee.
    accepted = client.post(
        f"/api/challenges/accept/{match_id}", headers=_jwt(INVITEE_EMAIL)
    )
    assert accepted.status_code == 200, accepted.text
    assert matches_db.docs[match_id]["status"] == "active"
    assert main.in_memory_matches[match_id]["status"] == "active"


def test_guest_embedding_the_invitee_hex_cannot_accept(
    client, users_db, matches_db
):
    match_id = _challenge_to_invitee(client, matches_db)
    # "guest-<invitee hex>" is a different string from both the ObjectId and
    # its hex form: raw comparison correctly rejects the lookalike.
    rejected = client.post(
        f"/api/challenges/accept/{match_id}", headers=_guest(HEXY_INVITEE_GUEST)
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "Not your challenge to accept"


def _seed_hex_string_challenge(matches_db, status="pending"):
    """A challenge doc whose ids degraded to plain strings (JSON round-trip,
    external writer, backup restore): player2_id is the invitee's HEX STRING
    while the invitee's live identity is the ObjectId."""
    doc = {
        "_id": "match-json-restored-1",
        "match_code": "JSONRT",
        "match_type": "friend",
        "player1_id": GUEST_A,
        "player1_username": None,
        "player2_id": str(INVITEE_OID),
        "player2_username": INVITEE_USERNAME,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": 1000,
        "player2_elo": 1100,
        "status": status,
        "winner_id": None,
        "elo_change": 0,
        "created_at": datetime.utcnow(),
    }
    matches_db.docs[doc["_id"]] = copy.deepcopy(doc)
    main.in_memory_matches[doc["_id"]] = copy.deepcopy(doc)
    return doc["_id"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(challenge-id-type-lockout): accept_challenge compares "
        "match['player2_id'] to current_user['_id'] RAW, with no str() "
        "bridging, unlike every gameplay route.  A challenge whose "
        "player2_id is stored as the invitee's hex string (JSON round-trip "
        "/ legacy doc) 403s the genuine ObjectId invitee, who can therefore "
        "never accept a challenge that the gameplay routes agree is theirs."
    ),
)
def test_hex_string_challenge_should_be_acceptable_by_its_objectid_invitee(
    client, users_db, matches_db
):
    match_id = _seed_hex_string_challenge(matches_db)
    accepted = client.post(
        f"/api/challenges/accept/{match_id}", headers=_jwt(INVITEE_EMAIL)
    )
    assert accepted.status_code == 200  # currently: 403


def test_current_behavior_hex_string_challenge_locked_but_playable(
    client, users_db, matches_db
):
    # BUG pin for the xfail above, showing the route-by-route inconsistency
    # for ONE credential (the invitee's JWT / ObjectId identity):
    match_id = _seed_hex_string_challenge(matches_db)

    # 1. the pending list queries player2_id by ObjectId -> misses the
    #    string-typed doc entirely;
    pending = client.get("/api/challenges/pending", headers=_jwt(INVITEE_EMAIL))
    assert pending.json() == []

    # 2. accept raw-compares ObjectId vs hex string -> "not yours";
    accepted = client.post(
        f"/api/challenges/accept/{match_id}", headers=_jwt(INVITEE_EMAIL)
    )
    assert accepted.status_code == 403

    # 3. yet the str()-bridging gameplay surface admits the same credential
    #    to the same match as player2.
    main.in_memory_matches[match_id]["status"] = "active"
    status = _status(client, match_id, _jwt(INVITEE_EMAIL))
    assert status.status_code == 200
    assert status.json()["player2_id"] == str(INVITEE_OID)


# ===========================================================================
# Case 6: cancel-challenge id comparisons
# ===========================================================================


def test_guest_creator_cancels_challenge_to_objectid_invitee(
    client, users_db, matches_db
):
    match_id = _challenge_to_invitee(client, matches_db)
    # player1_id is the guest string; string == string passes the raw check.
    cancelled = client.post(
        f"/api/challenges/cancel/{match_id}", headers=_guest(GUEST_A)
    )
    assert cancelled.status_code == 200
    assert match_id not in matches_db.docs
    assert match_id not in main.in_memory_matches


def test_objectid_creator_cancels_own_challenge_via_jwt(
    client, users_db, matches_db
):
    match_id = _challenge_to_invitee(
        client, matches_db, challenger_headers=_jwt(REG_EMAIL)
    )
    assert isinstance(matches_db.docs[match_id]["player1_id"], ObjectId)
    cancelled = client.post(
        f"/api/challenges/cancel/{match_id}", headers=_jwt(REG_EMAIL)
    )
    assert cancelled.status_code == 200
    assert match_id not in matches_db.docs


def test_objectid_invitee_cannot_cancel_someone_elses_challenge(
    client, users_db, matches_db
):
    match_id = _challenge_to_invitee(client, matches_db)
    rejected = client.post(
        f"/api/challenges/cancel/{match_id}", headers=_jwt(INVITEE_EMAIL)
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "Not your challenge to cancel"


def test_current_behavior_hex_string_creator_id_locks_out_cancel(
    client, users_db, matches_db
):
    # Mirror of BUG(challenge-id-type-lockout) on the cancel side: a pending
    # doc whose player1_id degraded to the creator's hex string 403s the
    # genuine ObjectId creator.
    doc_id = "match-json-restored-2"
    matches_db.docs[doc_id] = {
        "_id": doc_id,
        "match_code": "JSONR2",
        "match_type": "friend",
        "player1_id": str(REG_OID),  # hex string, not ObjectId
        "player2_id": None,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": REG_ELO,
        "player2_elo": 1000,
        "status": "waiting",
        "winner_id": None,
        "elo_change": 0,
        "created_at": datetime.utcnow(),
    }
    rejected = client.post(f"/api/challenges/cancel/{doc_id}", headers=_jwt(REG_EMAIL))
    assert rejected.status_code == 403  # raw ObjectId != hex string
    assert doc_id in matches_db.docs  # the orphaned doc is uncancellable


# ===========================================================================
# Case 7: get_active_match with ObjectId ids
# ===========================================================================


def test_active_match_resolves_objectid_opponent_but_mislabels_guest(
    client, users_db
):
    matched = _pair_ranked(client, _jwt(REG_EMAIL), _guest(GUEST_A))

    # Guest side: the opponent lookup uses the raw ObjectId -> users doc
    # resolves -> real username.
    guest_view = client.get("/api/game/active", headers=_guest(GUEST_A)).json()
    assert guest_view["has_active_match"] is True
    assert guest_view["match_id"] == matched["match_id"]
    assert guest_view["opponent"] == REG_USERNAME

    # Registered side: the guest opponent has no users doc, and the fallback
    # label is "AI Opponent" -- BUG(active-mislabels-humans-as-bot)
    # (strict-xfailed in tests/test_match_newly_found_bugs_edge_cases.py)
    # rediscovered on a RANKED human-vs-human match.
    reg_view = client.get("/api/game/active", headers=_jwt(REG_EMAIL)).json()
    assert reg_view["has_active_match"] is True
    assert reg_view["match_type"] == "ranked"
    assert reg_view["opponent"] == "AI Opponent"


def test_active_match_admits_objectid_user_through_the_str_bridge(
    client, users_db
):
    matched = _pair_ranked(client, _guest(GUEST_A), _jwt(REG_EMAIL))
    # player1_id is a real ObjectId; the route still finds the match for the
    # JWT caller because both sides of the comparison are str()'d.
    body = client.get("/api/game/active", headers=_jwt(REG_EMAIL)).json()
    assert body["has_active_match"] is True
    assert body["match_id"] == matched["match_id"]


# ===========================================================================
# Case 8: match details / status / by-code payloads with mixed id types
# ===========================================================================


def test_status_endpoint_stringifies_mixed_ids_and_resolves_names(
    client, users_db
):
    matched = _pair_ranked(client, _jwt(REG_EMAIL), _guest(GUEST_A))
    body = _status(client, matched["match_id"], _guest(GUEST_A)).json()

    # ObjectId -> hex string; guest string passes through verbatim.
    assert body["player2_id"] == str(REG_OID)
    assert body["player1_id"] == GUEST_A
    # The raw-ObjectId users lookup resolves the registered name; the guest
    # side gets the placeholder.
    assert body["player2_name"] == REG_USERNAME
    assert body["player1_name"] == "Player 1"


def test_details_endpoint_reports_mixed_ids_as_strings(client, users_db):
    match_id = _friend_mixed(client)
    body = client.get(f"/match/{match_id}/details", headers=_guest(GUEST_B)).json()

    assert body["player1"]["id"] == str(REG_OID)
    assert body["player1"]["username"] == REG_USERNAME
    assert body["player2"]["id"] == GUEST_B
    assert body["player2"]["username"] == "Player 2"


def test_match_by_code_resolves_objectid_opponent_and_labels_guest(
    client, users_db
):
    matched = _pair_ranked(client, _jwt(REG_EMAIL), _guest(GUEST_A))
    code = matched["match_code"]

    # Guest caller sees the ObjectId opponent's username (double conversion:
    # ObjectId(opponent_id) on an already-ObjectId value is accepted).
    guest_view = client.get(f"/api/game/match/{code}", headers=_guest(GUEST_A)).json()
    assert guest_view["opponent_name"] == REG_USERNAME
    assert guest_view["is_opponent_bot"] is False
    assert guest_view["player2_id"] == str(REG_OID)

    # Registered caller sees the guest labeled "Guest" (substring check),
    # not mistaken for a bot.
    reg_view = client.get(f"/api/game/match/{code}", headers=_jwt(REG_EMAIL)).json()
    assert reg_view["opponent_name"] == "Guest"
    assert reg_view["is_opponent_bot"] is False
    assert reg_view["is_player1"] is False  # the guest caller was player1


def test_completed_mixed_match_winner_is_stringified_consistently(
    client, users_db, fixed_question
):
    matched = _pair_ranked(client, _guest(GUEST_A), _jwt(REG_EMAIL))
    match_id = matched["match_id"]
    for _ in range(3):
        body = _win_round(client, match_id, _jwt(REG_EMAIL))

    # The answer payload, the status payload and the stored doc agree once
    # str()'d, even though the doc holds a raw ObjectId.
    assert body["match_winner"] == str(REG_OID)
    assert isinstance(main.in_memory_matches[match_id]["winner_id"], ObjectId)
    status = _status(client, match_id, _guest(GUEST_A)).json()
    assert status["winner_id"] == str(REG_OID)
    assert status["status"] == "completed"


# ===========================================================================
# Case 9: ELO update targets on ranked completion
# ===========================================================================


def test_ranked_completion_updates_elo_on_the_correct_objectid_doc(
    client, users_db, fixed_question
):
    matched = _pair_ranked(client, _guest(GUEST_A), _jwt(REG_EMAIL))
    match_id = matched["match_id"]
    for _ in range(3):
        body = _win_round(client, match_id, _jwt(REG_EMAIL))

    change = main.calculate_elo_change(REG_ELO, 1000)
    assert body["elo_change"] == change
    # The winner's $inc landed on the ObjectId-keyed doc.
    assert users_db.docs[REG_OID]["elo"] == REG_ELO + change
    assert users_db.docs[REG_OID]["wins"] == 1

    # And the update FILTER carried a genuine ObjectId, never the hex string.
    winner_filters = [
        f for f, u in users_db.update_calls if u.get("$inc", {}).get("wins") == 1
    ]
    assert winner_filters, "no winner $inc reached the users collection"
    assert all(isinstance(f["_id"], ObjectId) for f in winner_filters)
    assert all(f["_id"] == REG_OID for f in winner_filters)


def test_guest_loser_elo_update_is_a_silent_users_noop(
    client, users_db, fixed_question
):
    matched = _pair_ranked(client, _guest(GUEST_A), _jwt(REG_EMAIL))
    for _ in range(3):
        _win_round(client, matched["match_id"], _jwt(REG_EMAIL))

    # The loser update targeted the guest string id: no users doc matches,
    # nothing is upserted, so the guest's "rating" silently evaporates.
    loser_filters = [
        f for f, u in users_db.update_calls if u.get("$inc", {}).get("losses") == 1
    ]
    assert loser_filters == [{"_id": GUEST_A}]
    assert GUEST_A not in users_db.docs
    assert set(users_db.docs) == {REG_OID, INVITEE_OID}  # nothing materialized


def test_guest_winner_drains_the_objectid_loser_one_sidedly(
    client, users_db, fixed_question
):
    # QUIRK/BUG-adjacent: in a mixed ranked match the rating change is only
    # half-applied -- the ObjectId loser pays real ELO while the guest
    # winner's gain goes nowhere.
    matched = _pair_ranked(client, _jwt(REG_EMAIL), _guest(GUEST_A))
    match_id = matched["match_id"]
    for _ in range(3):
        body = _win_round(client, match_id, _guest(GUEST_A))

    change = main.calculate_elo_change(1000, REG_ELO)
    assert body["match_winner"] == GUEST_A
    assert body["elo_change"] == change
    assert users_db.docs[REG_OID]["elo"] == REG_ELO - change
    assert users_db.docs[REG_OID]["losses"] == 1
    assert GUEST_A not in users_db.docs  # the winner side was a no-op


# ===========================================================================
# Case 10: a guest token embedding a full ObjectId hex stays a string
# ===========================================================================


def test_guest_prefixed_hex_token_is_never_coerced_to_objectid(
    client, users_db
):
    # "guest-<24hex>" fails ObjectId.is_valid (30 chars, non-hex prefix), so
    # both the queue key and the stored player id stay plain strings.
    assert ObjectId.is_valid(HEXY_GUEST) is False

    matched = _pair_ranked(client, _guest(HEXY_GUEST), _jwt(REG_EMAIL))
    match = main.in_memory_matches[matched["match_id"]]
    assert match["player2_id"] == HEXY_GUEST
    assert isinstance(match["player2_id"], str)
    assert isinstance(match["player1_id"], ObjectId)  # the real user


def test_hex_lookalike_guest_and_real_objectid_do_not_cross_credit(
    client, users_db, fixed_question
):
    # str(REG_OID) is a strict SUFFIX of HEXY_GUEST; equality comparisons
    # must not conflate them when both play in one match.
    matched = _pair_ranked(client, _guest(HEXY_GUEST), _jwt(REG_EMAIL))
    match_id = matched["match_id"]

    _win_round(client, match_id, _jwt(REG_EMAIL))
    match = main.in_memory_matches[match_id]
    assert match["player1_score"] == 1 and match["player2_score"] == 0

    _win_round(client, match_id, _guest(HEXY_GUEST))
    match = main.in_memory_matches[match_id]
    assert match["player1_score"] == 1 and match["player2_score"] == 1


def test_hex_lookalike_guest_is_an_outsider_on_the_real_users_match(
    client, users_db, matches_db, fixed_question
):
    # A friend match belonging to the real ObjectId user and an ordinary
    # guest: the lookalike passes NO ownership gate on it.
    match_id = _friend_mixed(client)
    assert _status(client, match_id, _guest(HEXY_GUEST)).status_code == 403
    assert _question(client, match_id, _guest(HEXY_GUEST)).status_code == 403
    assert _answer(client, match_id, _guest(HEXY_GUEST)).status_code == 403

    # Nor can it accept a challenge pinned to the real user's ObjectId.
    challenge_id = _challenge_to_invitee(client, matches_db)
    hexy_invitee = client.post(
        f"/api/challenges/accept/{challenge_id}", headers=_guest(HEXY_INVITEE_GUEST)
    )
    assert hexy_invitee.status_code == 403


def test_by_code_labels_the_hex_lookalike_guest_not_the_registered_user(
    client, users_db
):
    matched = _pair_ranked(client, _guest(HEXY_GUEST), _jwt(REG_EMAIL))
    body = client.get(
        f"/api/game/match/{matched['match_code']}", headers=_jwt(REG_EMAIL)
    ).json()
    # The lookalike is reported as a plain "Guest" -- its embedded hex never
    # resolves to (or leaks) the registered user's identity.
    assert body["opponent_name"] == "Guest"
    assert body["opponent_name"] != REG_USERNAME
    assert body["is_opponent_bot"] is False
    assert body["player2_id"] == HEXY_GUEST
