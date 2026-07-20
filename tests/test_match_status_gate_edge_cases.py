"""
Status-gating matrix for people matches: which match statuses allow which
operations, route by route.

For every status in {waiting, pending, active, completed, abandoned} and
every route in {question, answer, give-up, status-poll, friend/join,
challenges/accept, challenges/cancel, /start reconnect} there is a
parametrized test asserting the CURRENT allow/reject behavior, plus strict
xfails wherever a status should be rejected but is not.

Current gate matrix (what main.py actually enforces today):

    route \\ status        waiting  pending  active  completed  abandoned
    ------------------    -------  -------  ------  ---------  ---------
    GET  question          ALLOW*   ALLOW*   ALLOW   reject400   ALLOW*
    POST answer            ALLOW*   ALLOW*   ALLOW   reject400   ALLOW*
    POST give-up           ALLOW*   ALLOW*   ALLOW   ALLOW*      ALLOW*
    GET  status poll       ALLOW    ALLOW    ALLOW   ALLOW       ALLOW
    POST friend/join       ALLOW    rej400   rej400  reject400   rej400
    POST challenges/accept rej403   ALLOW    rej400  reject400   rej400
    POST challenges/cancel ALLOW    ALLOW    rej400  reject400   rej400
    POST /start reconnect  no       no       ALLOW   no          no

    * = inconsistency: the status should be rejected but is not (strict
        xfail in this file).  Gameplay routes (question/answer) reject only
        "completed"; give-up has NO status check at all -- not even
        completed.  The status poll is read-only and intentionally open to
        every status; /start reconnect correctly offers a reconnect only
        for "active" (everything else falls through to "searching").

Known bugs re-pinned here through the matrix lens (see
MATCH_EDGE_CASE_REPORT.md bugs 8, 9 and 30):
- pending challenges are fully playable without being accepted;
- a creator can solo-play and solo-COMPLETE a waiting (unjoined) match;
- abandoned "zombie" matches remain fully playable;
- completed is the only status the gameplay routes reject.

Transitions covered: waiting -> active -> completed (friend join + first-to-3),
pending -> active (challenge accept), active -> abandoned (stale /start scan),
each with before/after gate checks.

Conventions match the sibling edge-case files: guest identities via
"Bearer guest-xxx" tokens, a FakeMatchesDB stand-in because the challenge
endpoints are DB-only, strict xfail plus current-behavior sibling pins.
"""

import copy
from datetime import datetime, timedelta

import pytest

import main


PLAYER_A = "guest-gate-aaa"  # creator / player1
PLAYER_B = "guest-gate-bbb"  # invitee / player2
OUTSIDER = "guest-gate-ccc"  # third party used for join attempts

INVITEE_USERNAME = "GateInvitee"

USER_REGISTRY = {
    INVITEE_USERNAME: {
        "_id": PLAYER_B,
        "username": INVITEE_USERNAME,
        "name": "Gate Invitee",
        "elo": 1000,
        "wins": 0,
        "losses": 0,
    },
}

ALL_STATUSES = ["waiting", "pending", "active", "completed", "abandoned"]

# Statuses gameplay routes should reject but currently allow (bugs 8/9/30).
GAMEPLAY_LEAKY_STATUSES = ["waiting", "pending", "abandoned"]


# ---------------------------------------------------------------------------
# fakes / fixtures
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


class FakeMatchesDB:
    """
    Minimal Mongo stand-in for matches_collection.  The challenge endpoints
    (accept/cancel) are DB-only, so the matrix needs the seeded docs to be
    visible through find_one/update_one/delete_one.  Only flat equality
    queries and {"$set": ...} updates are supported (other operators like
    $push are ignored), which covers every query shape these routes issue.
    """

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
def fake_matches_db(mock_mongo, monkeypatch):
    db = FakeMatchesDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.matches_collection, method, getattr(db, method))
    return db


@pytest.fixture
def fake_users(mock_mongo, monkeypatch):
    async def find_one(query, *args, **kwargs):
        username = query.get("username")
        if username is not None:
            return copy.deepcopy(USER_REGISTRY.get(username))
        return None

    monkeypatch.setattr(main.users_collection, "find_one", find_one)


def _seed_match(fake_matches_db, status, match_id=None):
    """
    Seed one friend-style match in the given status into BOTH stores
    (in_memory_matches for the gameplay routes, the fake DB for the
    DB-only challenge routes).  A waiting match has no player2 yet;
    every other status has PLAYER_B in the player2 slot.
    """
    match_id = match_id or f"match-gate-{status}"
    match_doc = {
        "_id": match_id,
        "match_code": f"GT{status[:4].upper()}",
        "match_type": "friend",
        "player1_id": PLAYER_A,
        "player1_username": None,
        "player2_id": None if status == "waiting" else PLAYER_B,
        "player2_username": None,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": 1000,
        "player2_elo": 1000,
        "status": status,
        "winner_id": PLAYER_A if status == "completed" else None,
        "elo_change": 0,
        "created_at": datetime.utcnow(),
    }
    main.in_memory_matches[match_id] = match_doc
    fake_matches_db.docs[match_id] = copy.deepcopy(match_doc)
    return match_doc


def _seed_round(match_id, winner=None):
    """Attach a current round to a seeded match (open unless winner given)."""
    round_id = f"round-{match_id}-1"
    round_doc = {
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
        "winner_id": winner,
        "created_at": main.utc_now(),
    }
    main.in_memory_rounds[round_id] = round_doc
    main.in_memory_matches[match_id]["current_round_id"] = round_id
    main.in_memory_matches[match_id]["round_start_time"] = main.utc_now().isoformat()
    return round_doc


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------


def _question(client, auth_headers, match_id, player=PLAYER_A):
    return client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )


def _answer(client, auth_headers, match_id, player=PLAYER_A, answer="2*x"):
    return client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )


def _give_up(client, auth_headers, match_id, player=PLAYER_A):
    return client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )


def _status(client, auth_headers, match_id, player=PLAYER_A):
    return client.get(f"/api/game/status/{match_id}", headers=auth_headers(player))


def _join(client, auth_headers, match_code, player=OUTSIDER):
    return client.post(
        "/api/game/friend/join",
        json={"match_code": match_code},
        headers=auth_headers(player),
    )


def _accept(client, auth_headers, match_id, player=PLAYER_B):
    return client.post(
        f"/api/challenges/accept/{match_id}", headers=auth_headers(player)
    )


def _cancel(client, auth_headers, match_id, player=PLAYER_A):
    return client.post(
        f"/api/challenges/cancel/{match_id}", headers=auth_headers(player)
    )


def _start(client, auth_headers, player=PLAYER_A, continue_existing=False):
    response = client.post(
        "/api/game/start",
        json={"mode": "random", "continue_existing": continue_existing},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# GET /api/game/question x status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,allowed",
    [
        ("waiting", True),  # BUG(waiting-match-solo-play): should reject
        ("pending", True),  # BUG(pending-challenge-playable): should reject
        ("active", True),
        ("completed", False),
        ("abandoned", True),  # BUG(zombie-abandoned-match): should reject
    ],
)
def test_question_gate_current_behavior(
    client, fake_matches_db, auth_headers, fixed_question, status, allowed
):
    match = _seed_match(fake_matches_db, status)
    response = _question(client, auth_headers, match["_id"])

    if allowed:
        assert response.status_code == 200, response.text
        assert "expression" in response.json()
    else:
        assert response.status_code == 400
        assert response.json()["detail"] == "Match is already completed"


@pytest.mark.parametrize("status", GAMEPLAY_LEAKY_STATUSES)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(status-gate/question): get_question only rejects status "
        "'completed'; waiting, pending and abandoned matches should be "
        "rejected too but serve questions normally (bugs 8/9/30)."
    ),
)
def test_question_should_reject_non_active_statuses(
    client, fake_matches_db, auth_headers, fixed_question, status
):
    match = _seed_match(fake_matches_db, status)
    response = _question(client, auth_headers, match["_id"])
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/game/answer x status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,allowed",
    [
        ("waiting", True),  # BUG(waiting-match-solo-play): should reject
        ("pending", True),  # BUG(pending-challenge-playable): should reject
        ("active", True),
        ("completed", False),
        ("abandoned", True),  # BUG(zombie-abandoned-match): should reject
    ],
)
def test_answer_gate_current_behavior(
    client, fake_matches_db, auth_headers, status, allowed
):
    match = _seed_match(fake_matches_db, status)
    _seed_round(match["_id"])

    response = _answer(client, auth_headers, match["_id"])

    if allowed:
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["correct"] is True
        # The scored answer even counts: gating leaks are full-service.
        assert body["player1_score"] == 1
    else:
        assert response.status_code == 400
        assert response.json()["detail"] == "Match is already completed"


@pytest.mark.parametrize("status", GAMEPLAY_LEAKY_STATUSES)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(status-gate/answer): submit_answer only rejects status "
        "'completed'; waiting, pending and abandoned matches should be "
        "rejected too but accept and score answers (bugs 8/9/30)."
    ),
)
def test_answer_should_reject_non_active_statuses(
    client, fake_matches_db, auth_headers, status
):
    match = _seed_match(fake_matches_db, status)
    _seed_round(match["_id"])
    response = _answer(client, auth_headers, match["_id"])
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/game/give-up x status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected_body_status",
    [
        # give_up_round has NO status check whatsoever -- every status is
        # processed.  Non-completed matches get a normal solo give-up.
        ("waiting", "gave_up"),
        ("pending", "gave_up"),
        ("active", "gave_up"),
        # A realistic completed match's final round has a winner, so the
        # give-up is answered with already_ended -- still 200, never 400.
        ("completed", "already_ended"),
        ("abandoned", "gave_up"),
    ],
)
def test_give_up_gate_current_behavior(
    client, fake_matches_db, auth_headers, status, expected_body_status
):
    match = _seed_match(fake_matches_db, status)
    _seed_round(match["_id"], winner=PLAYER_A if status == "completed" else None)

    response = _give_up(client, auth_headers, match["_id"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected_body_status


@pytest.mark.parametrize("status", ["waiting", "pending", "abandoned", "completed"])
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(status-gate/give-up): give_up_round has no status check at all "
        "-- unlike question/answer it does not even reject 'completed'.  "
        "Non-active statuses should be 400."
    ),
)
def test_give_up_should_reject_non_active_statuses(
    client, fake_matches_db, auth_headers, status
):
    match = _seed_match(fake_matches_db, status)
    _seed_round(match["_id"], winner=PLAYER_A if status == "completed" else None)
    response = _give_up(client, auth_headers, match["_id"])
    assert response.status_code == 400


def test_current_behavior_give_up_processes_on_completed_match_with_open_round(
    client, fake_matches_db, auth_headers
):
    # Purest evidence of the missing gate: a completed match with a
    # still-open round (no winner) gets a full give-up flow -- the flag is
    # recorded and the caller waits for an opponent who will never come,
    # instead of the 400 that question/answer would return.
    match = _seed_match(fake_matches_db, "completed")
    round_doc = _seed_round(match["_id"], winner=None)

    response = _give_up(client, auth_headers, match["_id"])
    assert response.status_code == 200
    assert response.json() == {"status": "gave_up", "waiting_for_opponent": True}
    assert round_doc["player1_gave_up"] is True


# ---------------------------------------------------------------------------
# GET /api/game/status/{match_id} x status (read-only: open by design)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_status_poll_allowed_for_every_status(
    client, fake_matches_db, auth_headers, status
):
    match = _seed_match(fake_matches_db, status)
    response = _status(client, auth_headers, match["_id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == status
    assert body["match_id"] == match["_id"]


# ---------------------------------------------------------------------------
# POST /api/game/friend/join x status (strictest gate: waiting only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,allowed",
    [
        ("waiting", True),
        ("pending", False),
        ("active", False),
        ("completed", False),
        ("abandoned", False),
    ],
)
def test_friend_join_gate(client, fake_matches_db, auth_headers, status, allowed):
    match = _seed_match(fake_matches_db, status)
    response = _join(client, auth_headers, match["match_code"])

    if allowed:
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "active"
        assert str(main.in_memory_matches[match["_id"]]["player2_id"]) == OUTSIDER
    else:
        assert response.status_code == 400
        assert response.json()["detail"] == "Match already started"
        # Nothing mutated.
        assert main.in_memory_matches[match["_id"]]["status"] == status


# ---------------------------------------------------------------------------
# POST /api/challenges/accept/{id} x status (pending only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected_code",
    [
        # A waiting match has player2_id None, so the invitee check fires
        # before the status check: rejected, but as 403 instead of 400.
        ("waiting", 403),
        ("pending", 200),
        ("active", 400),
        ("completed", 400),
        ("abandoned", 400),
    ],
)
def test_challenge_accept_gate(
    client, fake_matches_db, auth_headers, status, expected_code
):
    match = _seed_match(fake_matches_db, status)
    response = _accept(client, auth_headers, match["_id"])
    assert response.status_code == expected_code, response.text

    if expected_code == 200:
        assert response.json()["status"] == "active"
        assert fake_matches_db.docs[match["_id"]]["status"] == "active"
    elif expected_code == 400:
        assert response.json()["detail"] == "Challenge already accepted or expired"
        assert fake_matches_db.docs[match["_id"]]["status"] == status


# ---------------------------------------------------------------------------
# POST /api/challenges/cancel/{id} x status (waiting or pending only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,allowed",
    [
        ("waiting", True),
        ("pending", True),
        ("active", False),
        ("completed", False),
        ("abandoned", False),
    ],
)
def test_challenge_cancel_gate(client, fake_matches_db, auth_headers, status, allowed):
    match = _seed_match(fake_matches_db, status)
    response = _cancel(client, auth_headers, match["_id"])

    if allowed:
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "cancelled"}
        assert match["_id"] not in fake_matches_db.docs
        assert match["_id"] not in main.in_memory_matches
    else:
        assert response.status_code == 400
        assert response.json()["detail"] == "Challenge already active or completed"
        assert match["_id"] in fake_matches_db.docs


# ---------------------------------------------------------------------------
# POST /api/game/start reconnect x status (active only gets a reconnect)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,reconnects",
    [
        ("waiting", False),
        ("pending", False),
        ("active", True),
        ("completed", False),
        ("abandoned", False),
    ],
)
def test_start_reconnect_gate(
    client, fake_matches_db, auth_headers, status, reconnects
):
    # Fresh (<5s old) match: the reconnect scan only ever matches status
    # "active"; every other status falls through to matchmaking.
    match = _seed_match(fake_matches_db, status)

    body = _start(client, auth_headers, PLAYER_A)

    if reconnects:
        assert body["status"] == "matched"
        assert body["match_id"] == match["_id"]
    else:
        assert body["status"] == "searching"
        assert PLAYER_A in main.matchmaking_queue
        # The scan does not disturb the fresh non-active match either.
        assert main.in_memory_matches[match["_id"]]["status"] == status


# ---------------------------------------------------------------------------
# Known-bug pins through the matrix lens
# ---------------------------------------------------------------------------


def test_pin_pending_challenge_playable_without_accept(
    client, fake_users, fake_matches_db, auth_headers, fixed_question
):
    # Bug 9 pin: a real (API-created) pending challenge is fully playable
    # by both parties before anyone accepts it.
    created = client.post(
        "/api/game/friend/create",
        json={"opponent_username": INVITEE_USERNAME},
        headers=auth_headers(PLAYER_A),
    ).json()
    match_id = created["match_id"]
    assert created["status"] == "pending"

    assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 200
    assert _question(client, auth_headers, match_id, PLAYER_B).status_code == 200
    win = _answer(client, auth_headers, match_id, PLAYER_B)
    assert win.status_code == 200
    assert win.json()["correct"] is True
    assert main.in_memory_matches[match_id]["status"] == "pending"  # never left


def test_pin_waiting_match_solo_completable_by_creator(
    client, fake_matches_db, auth_headers, fixed_question
):
    # Bug 30 pin: the creator of an unjoined (waiting) friend match can
    # play question/answer alone against nobody and complete the match 3-0.
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()
    match_id = created["match_id"]
    assert created["status"] == "waiting"

    for _ in range(3):
        assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 200
        response = _answer(client, auth_headers, match_id, PLAYER_A)
        assert response.status_code == 200
        assert response.json()["correct"] is True

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["player1_score"] == 3
    assert str(match["winner_id"]) == PLAYER_A
    assert match["player2_id"] is None  # completed against nobody


def test_pin_abandoned_zombie_match_plays_a_full_round(
    client, fake_matches_db, auth_headers, fixed_question
):
    # Bug 8 pin: an abandoned match keeps serving rounds and scoring points
    # for BOTH players, indistinguishable from a live one.
    match = _seed_match(fake_matches_db, "abandoned")
    match_id = match["_id"]

    q_a = _question(client, auth_headers, match_id, PLAYER_A)
    q_b = _question(client, auth_headers, match_id, PLAYER_B)
    assert q_a.status_code == 200 and q_b.status_code == 200
    assert q_a.json()["round_id"] == q_b.json()["round_id"]

    win = _answer(client, auth_headers, match_id, PLAYER_B)
    assert win.status_code == 200
    assert win.json()["player2_score"] == 1
    assert main.in_memory_matches[match_id]["status"] == "abandoned"


def test_pin_completed_match_rejects_every_mutating_gameplay_route(
    client, fake_matches_db, auth_headers
):
    # The one status the gameplay gates handle correctly: completed matches
    # reject question and answer with 400 and cannot be joined, accepted or
    # cancelled -- only the read-only status poll (and give-up, see the
    # give-up xfail) still answer 200.
    match = _seed_match(fake_matches_db, "completed")
    match_id = match["_id"]

    assert _question(client, auth_headers, match_id).status_code == 400
    assert _answer(client, auth_headers, match_id).status_code == 400
    assert _join(client, auth_headers, match["match_code"]).status_code == 400
    assert _accept(client, auth_headers, match_id).status_code == 400
    assert _cancel(client, auth_headers, match_id).status_code == 400
    assert _status(client, auth_headers, match_id).status_code == 200


# ---------------------------------------------------------------------------
# Transitions: gates flip as the status moves through its lifecycle
# ---------------------------------------------------------------------------


def test_transition_waiting_to_active_to_completed(
    client, fake_matches_db, auth_headers, fixed_question
):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER_A)
    ).json()
    match_id, code = created["match_id"], created["match_code"]

    # waiting: joinable and cancellable, not acceptable (no player2).
    assert created["status"] == "waiting"
    assert _accept(client, auth_headers, match_id).status_code == 403

    # waiting -> active via join.
    assert _join(client, auth_headers, code, PLAYER_B).status_code == 200
    assert main.in_memory_matches[match_id]["status"] == "active"

    # active: playable; join/cancel/accept now rejected.
    assert _join(client, auth_headers, code, OUTSIDER).status_code == 400
    assert _cancel(client, auth_headers, match_id).status_code == 400
    assert _accept(client, auth_headers, match_id).status_code == 400

    # active -> completed via first-to-3.
    for _ in range(3):
        assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 200
        assert _answer(client, auth_headers, match_id, PLAYER_A).json()["correct"] is True
    assert main.in_memory_matches[match_id]["status"] == "completed"

    # completed: everything mutating is rejected, polling still works.
    assert _question(client, auth_headers, match_id).status_code == 400
    assert _answer(client, auth_headers, match_id).status_code == 400
    assert _join(client, auth_headers, code, OUTSIDER).status_code == 400
    assert _cancel(client, auth_headers, match_id).status_code == 400
    status = _status(client, auth_headers, match_id)
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert str(status.json()["winner_id"]) == PLAYER_A


def test_transition_pending_to_active_via_accept(
    client, fake_users, fake_matches_db, auth_headers
):
    created = client.post(
        "/api/game/friend/create",
        json={"opponent_username": INVITEE_USERNAME},
        headers=auth_headers(PLAYER_A),
    ).json()
    match_id = created["match_id"]
    assert created["status"] == "pending"

    # pending: not joinable by code, but acceptable and cancellable.
    assert _join(client, auth_headers, created["match_code"]).status_code == 400

    accepted = _accept(client, auth_headers, match_id)
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "active"
    assert main.in_memory_matches[match_id]["status"] == "active"
    assert fake_matches_db.docs[match_id]["status"] == "active"

    # active: accept and cancel are one-shot -- both rejected now.
    assert _accept(client, auth_headers, match_id).status_code == 400
    assert _cancel(client, auth_headers, match_id).status_code == 400


def test_transition_active_to_abandoned_via_stale_start_scan(
    client, fake_matches_db, auth_headers, fixed_question
):
    # An active match older than the 5s reconnect window is flipped to
    # abandoned by the owner's next /start poll (memory only -- the DB
    # write never happens, bug 35).
    match = _seed_match(fake_matches_db, "active")
    match_id = match["_id"]
    main.in_memory_matches[match_id]["created_at"] = datetime.utcnow() - timedelta(
        seconds=30
    )

    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "searching"  # no reconnect offered
    assert main.in_memory_matches[match_id]["status"] == "abandoned"
    assert fake_matches_db.docs[match_id]["status"] == "active"  # bug 35 pin

    # Gates after the transition: dead to join/accept/cancel and to
    # reconnect, visible to polling -- yet still playable (zombie, bug 8).
    assert _join(client, auth_headers, match["match_code"]).status_code == 400
    assert _cancel(client, auth_headers, match_id).status_code == 400
    poll = _status(client, auth_headers, match_id, PLAYER_B)
    assert poll.status_code == 200
    assert poll.json()["status"] == "abandoned"
    assert _question(client, auth_headers, match_id, PLAYER_B).status_code == 200
