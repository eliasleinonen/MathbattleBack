"""
Edge-case tests for direct challenges between people:

- /api/challenges/pending      (get_pending_challenges)
- /api/challenges/accept/{id}  (accept_challenge)
- /api/challenges/cancel/{id}  (cancel_challenge)

A challenge is a friend match created with an opponent_username that resolves
to a real user; it starts in status "pending" with player2_id pre-assigned.

Unlike the friend join/status endpoints, ALL THREE challenge endpoints read
exclusively from the database (matches_collection) with no in_memory_matches
fallback, so these tests run against a small in-process fake of the matches
collection that behaves like Mongo for the query shapes main.py uses.
Dedicated tests at the bottom document the missing-fallback inconsistency
against the default "DB always empty" mocks from conftest.

Known bugs are documented with comments / strict xfail instead of changing
main.py.  See MATCH_EDGE_CASE_REPORT.md for the summary.
"""

import copy

import pytest

import main


CHALLENGER = "guest-challenger-aaa"
INVITEE = "guest-invitee-bbb"
OUTSIDER = "guest-outsider-ccc"
SECOND_CHALLENGER = "guest-challenger-ddd"

INVITEE_USERNAME = "BeeKeeper"
CHALLENGER_USERNAME = "AlphaWolf"

USER_REGISTRY = {
    INVITEE_USERNAME: {
        "_id": INVITEE,
        "username": INVITEE_USERNAME,
        "name": "Bee Keeper",
        "elo": 1234,
        "wins": 0,
        "losses": 0,
    },
    CHALLENGER_USERNAME: {
        "_id": CHALLENGER,
        "username": CHALLENGER_USERNAME,
        "name": "Alpha Wolf",
        "elo": 1100,
        "wins": 0,
        "losses": 0,
    },
}


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
    Minimal stand-in for the Mongo matches collection.  Supports the flat
    equality queries and {"$set": ...} updates that the friend/challenge
    endpoints issue.  Documents are deep-copied on read, like a real driver.
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
def fake_users(mock_mongo, monkeypatch):
    """Exact, case-sensitive username lookups like Mongo's default collation."""

    async def find_one(query, *args, **kwargs):
        username = query.get("username")
        if username is not None:
            return copy.deepcopy(USER_REGISTRY.get(username))
        return None

    monkeypatch.setattr(main.users_collection, "find_one", find_one)


@pytest.fixture
def fake_matches_db(mock_mongo, monkeypatch):
    db = FakeMatchesDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.matches_collection, method, getattr(db, method))
    return db


def _create_challenge(
    client, auth_headers, challenger=CHALLENGER, opponent_username=INVITEE_USERNAME
):
    response = client.post(
        "/api/game/friend/create",
        json={"opponent_username": opponent_username},
        headers=auth_headers(challenger),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    return body


def _pending(client, auth_headers, user):
    response = client.get("/api/challenges/pending", headers=auth_headers(user))
    assert response.status_code == 200, response.text
    return response.json()


def _accept(client, auth_headers, match_id, user=INVITEE):
    return client.post(
        f"/api/challenges/accept/{match_id}", headers=auth_headers(user)
    )


def _cancel(client, auth_headers, match_id, user=CHALLENGER):
    return client.post(
        f"/api/challenges/cancel/{match_id}", headers=auth_headers(user)
    )


# ---------------------------------------------------------------------------
# get_pending_challenges
# ---------------------------------------------------------------------------


def test_pending_challenges_empty_for_fresh_user(client, fake_matches_db, auth_headers):
    assert _pending(client, auth_headers, INVITEE) == []


def test_pending_challenge_visible_to_invitee(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)

    pending = _pending(client, auth_headers, INVITEE)
    assert len(pending) == 1
    entry = pending[0]
    assert entry["match_id"] == created["match_id"]
    assert entry["match_code"] == created["match_code"]
    # Guests have no username, so the challenger label falls back to the
    # guest display name derived from the token suffix.
    assert entry["challenger"] == f"Guest {CHALLENGER[-4:]}"
    assert "created_at" in entry


def test_pending_challenge_not_visible_to_challenger_or_outsider(
    client, fake_users, fake_matches_db, auth_headers
):
    _create_challenge(client, auth_headers)

    # The listing filters on player2_id, so the creator sees nothing here.
    assert _pending(client, auth_headers, CHALLENGER) == []
    assert _pending(client, auth_headers, OUTSIDER) == []


def test_pending_list_excludes_non_pending_matches(
    client, fake_users, fake_matches_db, auth_headers
):
    accepted = _create_challenge(client, auth_headers)
    assert _accept(client, auth_headers, accepted["match_id"]).status_code == 200

    still_pending = _create_challenge(client, auth_headers, SECOND_CHALLENGER)

    pending = _pending(client, auth_headers, INVITEE)
    assert [c["match_id"] for c in pending] == [still_pending["match_id"]]


def test_multiple_pending_challenges_from_different_users(
    client, fake_users, fake_matches_db, auth_headers
):
    first = _create_challenge(client, auth_headers, CHALLENGER)
    second = _create_challenge(client, auth_headers, SECOND_CHALLENGER)
    third = _create_challenge(client, auth_headers, OUTSIDER)

    pending = _pending(client, auth_headers, INVITEE)
    assert {c["match_id"] for c in pending} == {
        first["match_id"],
        second["match_id"],
        third["match_id"],
    }


def test_pending_challenges_capped_at_ten(
    client, fake_users, fake_matches_db, auth_headers
):
    # get_pending_challenges hard-caps the listing via to_list(length=10);
    # any further challenges are silently invisible to the invitee.
    for i in range(12):
        _create_challenge(client, auth_headers, f"guest-spammer-{i:03d}")

    pending = _pending(client, auth_headers, INVITEE)
    assert len(pending) == 10


def test_same_user_can_stack_duplicate_challenges(
    client, fake_users, fake_matches_db, auth_headers
):
    # There is no dedupe: the same challenger can flood the same invitee
    # with identical pending challenges.  Documented behaviour, arguably a
    # spam vector.
    first = _create_challenge(client, auth_headers, CHALLENGER)
    second = _create_challenge(client, auth_headers, CHALLENGER)
    assert first["match_id"] != second["match_id"]

    pending = _pending(client, auth_headers, INVITEE)
    assert len(pending) == 2


# ---------------------------------------------------------------------------
# accept_challenge
# ---------------------------------------------------------------------------


def test_accept_challenge_happy_path(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    match_id = created["match_id"]

    response = _accept(client, auth_headers, match_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["match_id"] == match_id
    assert body["match_code"] == created["match_code"]
    assert body["status"] == "active"

    # Both stores agree the match is live.
    assert main.in_memory_matches[match_id]["status"] == "active"
    assert fake_matches_db.docs[match_id]["status"] == "active"

    # The public code-poller reflects it too.
    status = client.get(f"/api/game/friend/status/{created['match_code']}")
    assert status.json()["status"] == "active"


def test_accept_preserves_player_assignments(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    assert _accept(client, auth_headers, created["match_id"]).status_code == 200

    match = main.in_memory_matches[created["match_id"]]
    assert str(match["player1_id"]) == CHALLENGER
    assert str(match["player2_id"]) == INVITEE
    assert match["player2_username"] == INVITEE_USERNAME
    assert match["match_type"] == "friend"


def test_accept_someone_elses_challenge_is_403(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)

    response = _accept(client, auth_headers, created["match_id"], OUTSIDER)
    assert response.status_code == 403
    assert response.json()["detail"] == "Not your challenge to accept"
    # Challenge untouched.
    assert fake_matches_db.docs[created["match_id"]]["status"] == "pending"


def test_challenger_cannot_accept_own_challenge(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    response = _accept(client, auth_headers, created["match_id"], CHALLENGER)
    assert response.status_code == 403


def test_accept_nonexistent_challenge_is_404(client, fake_matches_db, auth_headers):
    response = _accept(client, auth_headers, "match-does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "Challenge not found"


def test_accept_already_accepted_challenge_is_400(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    assert _accept(client, auth_headers, created["match_id"]).status_code == 200

    again = _accept(client, auth_headers, created["match_id"])
    assert again.status_code == 400
    assert again.json()["detail"] == "Challenge already accepted or expired"


def test_accept_cancelled_challenge_is_404(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    assert _cancel(client, auth_headers, created["match_id"]).status_code == 200

    # Cancel deletes the document outright, so a late accept sees 404
    # rather than a "challenge was cancelled" message.
    response = _accept(client, auth_headers, created["match_id"])
    assert response.status_code == 404


def test_accept_abandoned_challenge_is_400(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    match_id = created["match_id"]
    fake_matches_db.docs[match_id]["status"] = "abandoned"
    main.in_memory_matches[match_id]["status"] = "abandoned"

    response = _accept(client, auth_headers, match_id)
    assert response.status_code == 400


def test_accept_completed_match_is_400(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    match_id = created["match_id"]
    fake_matches_db.docs[match_id]["status"] = "completed"

    response = _accept(client, auth_headers, match_id)
    assert response.status_code == 400


def test_accept_open_waiting_match_is_403_for_everyone(
    client, fake_users, fake_matches_db, auth_headers
):
    # A code-only friend match has player2_id None, so nobody can hijack it
    # through the accept endpoint (player2_id never equals a real user).
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(CHALLENGER)
    ).json()
    assert created["status"] == "waiting"

    response = _accept(client, auth_headers, created["match_id"], INVITEE)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# cancel_challenge
# ---------------------------------------------------------------------------


def test_cancel_challenge_by_creator(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    match_id = created["match_id"]

    response = _cancel(client, auth_headers, match_id)
    assert response.status_code == 200
    assert response.json() == {"status": "cancelled"}

    # Gone from both stores and from the invitee's pending list.
    assert match_id not in fake_matches_db.docs
    assert match_id not in main.in_memory_matches
    assert _pending(client, auth_headers, INVITEE) == []


def test_cancel_by_invitee_is_403(client, fake_users, fake_matches_db, auth_headers):
    created = _create_challenge(client, auth_headers)

    # Even the invited player cannot cancel -- only decline-by-ignoring.
    response = _cancel(client, auth_headers, created["match_id"], INVITEE)
    assert response.status_code == 403
    assert response.json()["detail"] == "Not your challenge to cancel"
    assert created["match_id"] in fake_matches_db.docs


def test_cancel_by_outsider_is_403(client, fake_users, fake_matches_db, auth_headers):
    created = _create_challenge(client, auth_headers)
    response = _cancel(client, auth_headers, created["match_id"], OUTSIDER)
    assert response.status_code == 403


def test_cancel_nonexistent_challenge_is_404(client, fake_matches_db, auth_headers):
    response = _cancel(client, auth_headers, "match-never-existed")
    assert response.status_code == 404


def test_cancel_accepted_challenge_is_400(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    assert _accept(client, auth_headers, created["match_id"]).status_code == 200

    response = _cancel(client, auth_headers, created["match_id"])
    assert response.status_code == 400
    assert response.json()["detail"] == "Challenge already active or completed"


def test_cancel_completed_match_is_400(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    fake_matches_db.docs[created["match_id"]]["status"] = "completed"

    response = _cancel(client, auth_headers, created["match_id"])
    assert response.status_code == 400


def test_creator_can_cancel_open_waiting_match_too(
    client, fake_matches_db, auth_headers
):
    # cancel_challenge also accepts status "waiting", so it doubles as the
    # "delete my unshared friend match" endpoint.
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(CHALLENGER)
    ).json()
    assert created["status"] == "waiting"

    response = _cancel(client, auth_headers, created["match_id"])
    assert response.status_code == 200

    # The code is dead afterwards.
    join = client.post(
        "/api/game/friend/join",
        json={"match_code": created["match_code"]},
        headers=auth_headers(INVITEE),
    )
    assert join.status_code == 404


def test_double_cancel_returns_404_on_second_call(
    client, fake_users, fake_matches_db, auth_headers
):
    created = _create_challenge(client, auth_headers)
    assert _cancel(client, auth_headers, created["match_id"]).status_code == 200
    assert _cancel(client, auth_headers, created["match_id"]).status_code == 404


# ---------------------------------------------------------------------------
# Challenge to self
# ---------------------------------------------------------------------------


def test_challenge_to_self_is_allowed_and_self_acceptable(
    client, fake_users, fake_matches_db, auth_headers
):
    """
    BUG (documented): there is no guard against challenging yourself.
    Creating a match with your own username yields a pending challenge where
    player1_id == player2_id, it shows up in your own pending list, and you
    can accept it to start a 1-player "duel".  Compare with the join path,
    which explicitly rejects joining your own match.
    """
    created = _create_challenge(
        client, auth_headers, CHALLENGER, opponent_username=CHALLENGER_USERNAME
    )
    match = main.in_memory_matches[created["match_id"]]
    assert str(match["player1_id"]) == CHALLENGER
    assert str(match["player2_id"]) == CHALLENGER

    pending = _pending(client, auth_headers, CHALLENGER)
    assert [c["match_id"] for c in pending] == [created["match_id"]]

    accepted = _accept(client, auth_headers, created["match_id"], CHALLENGER)
    assert accepted.status_code == 200
    assert main.in_memory_matches[created["match_id"]]["status"] == "active"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: create_friend_match should reject opponent_username equal to "
        "the creator (self-challenge), mirroring the 'Cannot join your own "
        "match' rule in join_friend_match.  Currently it returns a pending "
        "self-challenge."
    ),
)
def test_challenge_to_self_should_be_rejected(
    client, fake_users, fake_matches_db, auth_headers
):
    response = client.post(
        "/api/game/friend/create",
        json={"opponent_username": CHALLENGER_USERNAME},
        headers=auth_headers(CHALLENGER),
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Accept then actually play
# ---------------------------------------------------------------------------


def test_accept_then_play_a_full_round(
    client, fake_users, fake_matches_db, auth_headers, fixed_question
):
    created = _create_challenge(client, auth_headers)
    match_id = created["match_id"]
    assert _accept(client, auth_headers, match_id).status_code == 200

    q_challenger = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(CHALLENGER),
    )
    q_invitee = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(INVITEE),
    )
    assert q_challenger.status_code == 200, q_challenger.text
    assert q_invitee.status_code == 200
    assert q_challenger.json()["round_id"] == q_invitee.json()["round_id"]

    wrong = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "totally wrong"},
        headers=auth_headers(CHALLENGER),
    )
    assert wrong.status_code == 200
    assert wrong.json()["correct"] is False

    win = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(INVITEE),
    )
    assert win.status_code == 200, win.text
    body = win.json()
    assert body["correct"] is True
    assert str(body["round_winner"]) == INVITEE
    assert body["player1_score"] == 0
    assert body["player2_score"] == 1

    status = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(CHALLENGER)
    )
    assert status.status_code == 200
    assert status.json()["player2_score"] == 1


def test_unaccepted_challenge_is_already_playable(
    client, fake_users, fake_matches_db, auth_headers, fixed_question
):
    """
    BUG (documented): gameplay routes never check that the match status is
    "active" -- only that it is not "completed".  Both parties can therefore
    fetch questions and submit answers on a still-PENDING challenge, fully
    bypassing the accept step.
    """
    created = _create_challenge(client, auth_headers)
    match_id = created["match_id"]
    assert main.in_memory_matches[match_id]["status"] == "pending"

    question = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(CHALLENGER),
    )
    assert question.status_code == 200  # plays fine despite pending status

    answer = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(CHALLENGER),
    )
    assert answer.status_code == 200
    assert answer.json()["correct"] is True


# ---------------------------------------------------------------------------
# Missing in-memory fallback (inconsistency with the friend join/status path)
# ---------------------------------------------------------------------------


def test_pending_listing_misses_memory_only_challenges(
    client, fake_users, auth_headers
):
    """
    DOCUMENTED INCONSISTENCY: join_friend_match and get_match_status fall
    back to in_memory_matches when the DB misses, but the challenge
    endpoints query the DB only.  With the DB unavailable/empty (conftest
    default mocks), a challenge that exists in memory is invisible to the
    invitee...
    """
    created = _create_challenge(client, auth_headers)
    assert created["match_id"] in main.in_memory_matches

    assert _pending(client, auth_headers, INVITEE) == []


def test_accept_misses_memory_only_challenge(client, fake_users, auth_headers):
    """...and cannot be accepted either: accept_challenge 404s even though
    the pending match is sitting in in_memory_matches."""
    created = _create_challenge(client, auth_headers)
    assert main.in_memory_matches[created["match_id"]]["status"] == "pending"

    response = _accept(client, auth_headers, created["match_id"])
    assert response.status_code == 404
