"""
Edge-case tests for cross-match isolation and access control in
people-vs-people matches.

Covers:
- two simultaneous friend matches: rounds, scores, give-ups and completion
  never leak across match ids (including two matches between the SAME pair)
- one user in a ranked and a friend match at once, and the reconnect-window
  scan in /api/game/start hijacking / abandoning friend matches
- outsiders on every gameplay route of a ranked match
- /api/game/match/{code} for ranked codes (mixed-case token_urlsafe vs the
  upper-casing friend endpoints)
- a player in match A trying to act on match B
- /matches/all and /match/{id}/details authorization (or lack thereof)
- abandoned vs completed matches: which routes still work
- a spectator armed only with a leaked match_id

Conventions (same as the sibling edge-case files):
- Guest identities via "Bearer guest-xxx" tokens.
- Known bugs are documented with strict xfail markers plus a sibling test
  pinning the CURRENT behavior.  See MATCH_EDGE_CASE_REPORT.md.
"""

import copy
from datetime import datetime, timedelta

import pytest

import main


PLAYER_A = "guest-iso-aaa"
PLAYER_B = "guest-iso-bbb"
PLAYER_C = "guest-iso-ccc"
PLAYER_D = "guest-iso-ddd"
OUTSIDER = "guest-iso-outsider"
SPECTATOR = "guest-iso-spectator"

CORRECT = "2*x"  # matches fixed_question's answer "2·x"
RANKED_CODE_MIXED = "aB3xY-z9Qk_"  # deterministic token_urlsafe stand-in
RANKED_CODE_UPPER = "RANKED9CODE"


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


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


def _win_round(client, auth_headers, match_id, player):
    q = _question(client, auth_headers, match_id, player)
    assert q.status_code == 200, q.text
    body = _answer(client, auth_headers, match_id, player).json()
    assert body["correct"] is True, body
    return body


def _scores(match_id):
    match = main.in_memory_matches[match_id]
    return (match["player1_score"], match["player2_score"])


def _rounds_of(match_id):
    return {
        rid for rid, r in main.in_memory_rounds.items() if r["match_id"] == match_id
    }


@pytest.fixture
def rigged_ranked_code(monkeypatch):
    """Make secrets.token_urlsafe deterministic (mixed-case, like real output)."""
    monkeypatch.setattr(main.secrets, "token_urlsafe", lambda nbytes=8: RANKED_CODE_MIXED)
    return RANKED_CODE_MIXED


@pytest.fixture
def rigged_upper_ranked_code(monkeypatch):
    """A token_urlsafe that happens to contain no lowercase characters."""
    monkeypatch.setattr(main.secrets, "token_urlsafe", lambda nbytes=8: RANKED_CODE_UPPER)
    return RANKED_CODE_UPPER


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


@pytest.fixture
def db_matches(mock_mongo, monkeypatch):
    """Back matches_collection.find / find_one with an in-process list, the
    shape Mongo would hold after the $push/$set updates ran for real."""
    docs = []

    def find(*args, **kwargs):
        return _Cursor(docs)

    async def find_one(query, *args, **kwargs):
        for doc in docs:
            if doc["_id"] == query.get("_id"):
                return copy.deepcopy(doc)
        return None

    monkeypatch.setattr(main.matches_collection, "find", find)
    monkeypatch.setattr(main.matches_collection, "find_one", find_one)
    return docs


def _persisted_match_doc(match_id, p1=PLAYER_A, p2=PLAYER_B, **overrides):
    doc = {
        "_id": match_id,
        "match_code": "LEAKED",
        "match_type": "friend",
        "player1_id": p1,
        "player2_id": p2,
        "player1_score": 1,
        "player2_score": 0,
        "player1_elo": 1000,
        "player2_elo": 1000,
        "status": "active",
        "winner_id": None,
        "elo_change": 0,
        "created_at": datetime.utcnow(),
        "rounds": [
            {
                "round_number": 1,
                "question": "x^2",
                "derivative": "2·x",
                "evaluate_at": 3,
                "answer": "2·x",
                "difficulty": 1,
                "winner": "player1",
                "player1_answer": "2*x",
                "player2_answer": None,
            },
            {
                "round_number": 2,
                "question": "x^3",
                "derivative": "3·x^2",
                "evaluate_at": 2,
                "answer": "3·x^2",  # unresolved round: the live answer
                "difficulty": 1,
                "winner": None,
                "player1_answer": None,
                "player2_answer": None,
            },
        ],
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# two simultaneous friend matches don't cross-contaminate
# ---------------------------------------------------------------------------


def test_parallel_friend_matches_have_disjoint_rounds(
    client, auth_headers, fixed_question
):
    m1 = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    m2 = _friend_match(client, auth_headers, PLAYER_C, PLAYER_D)

    r1 = _question(client, auth_headers, m1, PLAYER_A).json()["round_id"]
    r2 = _question(client, auth_headers, m2, PLAYER_C).json()["round_id"]

    assert r1 != r2
    # Deterministic per-match ids embed the owning match.
    assert r1 == f"round-{m1}-1"
    assert r2 == f"round-{m2}-1"
    assert main.in_memory_matches[m1]["current_round_id"] == r1
    assert main.in_memory_matches[m2]["current_round_id"] == r2


def test_winning_a_round_in_one_match_leaves_the_other_untouched(
    client, auth_headers, fixed_question
):
    m1 = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    m2 = _friend_match(client, auth_headers, PLAYER_C, PLAYER_D)
    _question(client, auth_headers, m1, PLAYER_A)
    r2 = _question(client, auth_headers, m2, PLAYER_C).json()["round_id"]

    _win_round(client, auth_headers, m1, PLAYER_A)

    assert _scores(m1) == (1, 0)
    assert _scores(m2) == (0, 0)
    assert main.in_memory_rounds[r2]["winner_id"] is None
    assert main.in_memory_matches[m2]["status"] == "active"


def test_give_up_tie_in_one_match_does_not_tie_the_other(
    client, auth_headers, fixed_question
):
    m1 = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    m2 = _friend_match(client, auth_headers, PLAYER_C, PLAYER_D)
    r1 = _question(client, auth_headers, m1, PLAYER_A).json()["round_id"]
    r2 = _question(client, auth_headers, m2, PLAYER_C).json()["round_id"]

    _give_up(client, auth_headers, m1, PLAYER_A)
    body = _give_up(client, auth_headers, m1, PLAYER_B).json()
    assert body["status"] == "both_gave_up"

    assert main.in_memory_rounds[r1]["winner_id"] == "tie"
    assert main.in_memory_rounds[r2]["winner_id"] is None
    assert main.in_memory_rounds[r2].get("player1_gave_up") is not True


def test_completing_one_match_leaves_parallel_match_active(
    client, auth_headers, fixed_question
):
    m1 = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    m2 = _friend_match(client, auth_headers, PLAYER_C, PLAYER_D)
    _question(client, auth_headers, m2, PLAYER_C)

    for _ in range(3):
        _win_round(client, auth_headers, m1, PLAYER_A)

    assert main.in_memory_matches[m1]["status"] == "completed"
    assert str(main.in_memory_matches[m1]["winner_id"]) == PLAYER_A
    assert main.in_memory_matches[m2]["status"] == "active"
    assert main.in_memory_matches[m2]["winner_id"] is None
    assert _scores(m2) == (0, 0)


def test_same_pair_can_run_two_matches_and_they_stay_separate(
    client, auth_headers, fixed_question
):
    # Nothing prevents A and B from having two live matches against each
    # other; state is keyed by match_id, so they do not blend.
    m1 = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    m2 = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    assert m1 != m2

    _question(client, auth_headers, m1, PLAYER_A)
    _question(client, auth_headers, m2, PLAYER_B)
    _win_round(client, auth_headers, m1, PLAYER_A)
    _win_round(client, auth_headers, m2, PLAYER_B)

    assert _scores(m1) == (1, 0)  # A is player1 in both
    assert _scores(m2) == (0, 1)
    assert len(_rounds_of(m1)) == 1
    assert len(_rounds_of(m2)) == 1


def test_match_locks_are_per_match_objects(client, auth_headers):
    m1 = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    m2 = _friend_match(client, auth_headers, PLAYER_C, PLAYER_D)

    lock1 = main.get_match_lock(m1)
    lock2 = main.get_match_lock(m2)
    assert lock1 is not lock2
    assert main.get_match_lock(m1) is lock1  # stable per id


# ---------------------------------------------------------------------------
# ranked + friend match for the same user
# ---------------------------------------------------------------------------


def test_user_can_play_ranked_and_friend_match_simultaneously(
    client, auth_headers, fixed_question
):
    ranked_id = _ranked_match(client, auth_headers, PLAYER_B, PLAYER_A)
    friend_id = _friend_match(client, auth_headers, PLAYER_A, PLAYER_C)

    _question(client, auth_headers, ranked_id, PLAYER_A)
    _question(client, auth_headers, friend_id, PLAYER_A)
    _win_round(client, auth_headers, friend_id, PLAYER_A)

    # The friend-round win never bleeds into the ranked match.
    assert _scores(friend_id) == (1, 0)
    assert _scores(ranked_id) == (0, 0)
    assert main.in_memory_matches[ranked_id]["current_round_id"] != (
        main.in_memory_matches[friend_id]["current_round_id"]
    )


def test_active_endpoint_reports_first_active_match_in_insertion_order(
    client, auth_headers
):
    ranked_id = _ranked_match(client, auth_headers, PLAYER_B, PLAYER_A)
    friend_id = _friend_match(client, auth_headers, PLAYER_A, PLAYER_C)
    assert main.in_memory_matches[friend_id]["status"] == "active"

    body = client.get("/api/game/active", headers=auth_headers(PLAYER_A)).json()
    # Quirk pin: with two live matches only the older one is reported; the
    # friend match is invisible to /api/game/active.
    assert body["has_active_match"] is True
    assert body["match_id"] == ranked_id
    assert body["match_type"] == "ranked"


def test_current_behavior_ranked_start_reconnects_into_fresh_friend_match(
    client, auth_headers
):
    # Quirk pin: the reconnect window in start_match scans ALL active matches
    # without filtering by match_type.  A user who queues for ranked within
    # 5s of their friend match going active is "reconnected" into the friend
    # match instead of entering the queue.
    friend_id = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)

    body = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_A)
    ).json()
    assert body["status"] == "matched"
    assert body["match_id"] == friend_id
    assert PLAYER_A not in main.matchmaking_queue
    assert main.in_memory_matches[friend_id]["match_type"] == "friend"


def test_current_behavior_ranked_start_abandons_older_friend_match(
    client, auth_headers
):
    # BUG pin for the xfail below: past the 5s window the same scan marks the
    # user's ACTIVE FRIEND MATCH abandoned as a side effect of queueing for
    # ranked — the friend opponent is never told except via status polls.
    friend_id = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    main.in_memory_matches[friend_id]["created_at"] = (
        datetime.utcnow() - timedelta(seconds=10)
    )

    body = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_A)
    ).json()
    assert body["status"] == "searching"
    assert main.in_memory_matches[friend_id]["status"] == "abandoned"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(friend-match-abandoned-by-ranked-queue): start_match's stale-"
        "match scan does not filter by match_type, so entering the ranked "
        "queue silently abandons the caller's active friend match (>5s old) "
        "— or hijacks it as the 'ranked' result when it is <5s old."
    ),
)
def test_ranked_queueing_should_not_abandon_active_friend_match(
    client, auth_headers
):
    friend_id = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    main.in_memory_matches[friend_id]["created_at"] = (
        datetime.utcnow() - timedelta(seconds=10)
    )

    client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(PLAYER_A)
    )
    assert main.in_memory_matches[friend_id]["status"] == "active"


# ---------------------------------------------------------------------------
# outsider on every gameplay route of a ranked match
# ---------------------------------------------------------------------------


def test_outsider_gets_403_on_question_answer_giveup_status(
    client, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)

    assert _question(client, auth_headers, match_id, OUTSIDER).status_code == 403
    assert _answer(client, auth_headers, match_id, OUTSIDER).status_code == 403
    assert _give_up(client, auth_headers, match_id, OUTSIDER).status_code == 403
    assert _status(client, auth_headers, match_id, OUTSIDER).status_code == 403


def test_outsider_gets_403_on_match_by_code(client, auth_headers):
    match_id = _ranked_match(client, auth_headers)
    code = main.in_memory_matches[match_id]["match_code"]

    response = client.get(f"/api/game/match/{code}", headers=auth_headers(OUTSIDER))
    assert response.status_code == 403
    assert response.json()["detail"] == "Not authorized to access this match"


def test_outsider_403s_leave_no_trace_on_the_match(
    client, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)

    _question(client, auth_headers, match_id, OUTSIDER)
    _answer(client, auth_headers, match_id, OUTSIDER)
    _status(client, auth_headers, match_id, OUTSIDER)

    match = main.in_memory_matches[match_id]
    # No round was created, no score moved, no presence heartbeat recorded.
    assert _rounds_of(match_id) == set()
    assert _scores(match_id) == (0, 0)
    assert OUTSIDER not in match.get("player_last_seen", {})


def test_outsider_correct_answer_scores_nothing_even_with_open_round(
    client, auth_headers, fixed_question
):
    match_id = _ranked_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()["round_id"]

    response = _answer(client, auth_headers, match_id, OUTSIDER)
    assert response.status_code == 403
    assert main.in_memory_rounds[round_id]["winner_id"] is None
    assert _scores(match_id) == (0, 0)


# ---------------------------------------------------------------------------
# /api/game/match/{code} for ranked codes (mixed-case token_urlsafe)
# ---------------------------------------------------------------------------


def test_ranked_code_exact_case_lookup_works_for_member(
    client, auth_headers, rigged_ranked_code
):
    match_id = _ranked_match(client, auth_headers)
    assert main.in_memory_matches[match_id]["match_code"] == RANKED_CODE_MIXED

    response = client.get(
        f"/api/game/match/{RANKED_CODE_MIXED}", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["match_id"] == match_id
    assert body["is_opponent_bot"] is False


def test_ranked_code_uppercased_input_404s(
    client, auth_headers, rigged_ranked_code
):
    # Quirk pin: /api/game/match/{code} compares codes case-sensitively while
    # the friend endpoints normalize with .upper().  A client that upper-cases
    # ranked codes (as it must for friend codes) can never find its match.
    _ranked_match(client, auth_headers)

    response = client.get(
        f"/api/game/match/{RANKED_CODE_MIXED.upper()}",
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404


def test_friend_join_cannot_resolve_mixed_case_ranked_code(
    client, auth_headers, rigged_ranked_code
):
    # join_friend_match upper-cases the incoming code before scanning, so a
    # ranked code containing lowercase letters is unreachable there even when
    # passed with its exact original casing.
    _ranked_match(client, auth_headers)

    response = client.post(
        "/api/game/friend/join",
        json={"match_code": RANKED_CODE_MIXED},
        headers=auth_headers(OUTSIDER),
    )
    assert response.status_code == 404


def test_all_uppercase_ranked_code_is_reachable_via_friend_join(
    client, auth_headers, rigged_upper_ranked_code
):
    # Quirk pin: the friend join scan covers ALL matches, ranked included.
    # When token_urlsafe happens to produce no lowercase characters, a third
    # party who knows the code reaches the ranked match through the friend
    # endpoint — it is only saved by the status!="waiting" guard (400, not
    # a join), which also confirms the match's existence to the outsider.
    match_id = _ranked_match(client, auth_headers)

    response = client.post(
        "/api/game/friend/join",
        json={"match_code": RANKED_CODE_UPPER.lower()},
        headers=auth_headers(OUTSIDER),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Match already started"
    assert str(main.in_memory_matches[match_id]["player2_id"]) != OUTSIDER


def test_all_uppercase_ranked_code_leaks_via_unauthenticated_status_poller(
    client, auth_headers, rigged_upper_ranked_code
):
    # Quirk pin: /api/game/friend/status/{code} requires NO auth and also
    # scans all matches.  An uppercase-only ranked code exposes the match_id
    # and live status to anyone; a mixed-case one 404s only because of the
    # .upper() mismatch, not because of any access control.
    match_id = _ranked_match(client, auth_headers)

    response = client.get(f"/api/game/friend/status/{RANKED_CODE_UPPER.lower()}")
    assert response.status_code == 200
    body = response.json()
    assert body["match_id"] == match_id
    assert body["status"] == "active"


def test_mixed_case_ranked_code_invisible_to_unauthenticated_status_poller(
    client, auth_headers, rigged_ranked_code
):
    _ranked_match(client, auth_headers)
    response = client.get(f"/api/game/friend/status/{RANKED_CODE_MIXED}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# a player in match A cannot act on match B
# ---------------------------------------------------------------------------


def test_player_of_match_a_cannot_answer_match_b(
    client, auth_headers, fixed_question
):
    m1 = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    m2 = _friend_match(client, auth_headers, PLAYER_C, PLAYER_D)
    _question(client, auth_headers, m1, PLAYER_A)
    r2 = _question(client, auth_headers, m2, PLAYER_C).json()["round_id"]

    response = _answer(client, auth_headers, m2, PLAYER_A)  # correct answer!
    assert response.status_code == 403
    assert response.json()["detail"] == "Not your match"

    # Neither match moved: no cross-credit, no self-credit.
    assert main.in_memory_rounds[r2]["winner_id"] is None
    assert _scores(m2) == (0, 0)
    assert _scores(m1) == (0, 0)


def test_player_of_match_a_cannot_fetch_match_b_question(
    client, auth_headers, fixed_question
):
    _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    m2 = _friend_match(client, auth_headers, PLAYER_C, PLAYER_D)

    assert _question(client, auth_headers, m2, PLAYER_A).status_code == 403
    # The 403 fired before round creation.
    assert _rounds_of(m2) == set()


def test_player_of_match_a_cannot_give_up_match_b_round(
    client, auth_headers, fixed_question
):
    _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    m2 = _friend_match(client, auth_headers, PLAYER_C, PLAYER_D)
    r2 = _question(client, auth_headers, m2, PLAYER_C).json()["round_id"]

    assert _give_up(client, auth_headers, m2, PLAYER_A).status_code == 403
    round_doc = main.in_memory_rounds[r2]
    assert round_doc.get("player1_gave_up") is not True
    assert round_doc.get("player2_gave_up") is not True


# ---------------------------------------------------------------------------
# /matches/all and /match/{id}/details authorization
# ---------------------------------------------------------------------------


def test_matches_all_returns_everyones_matches_to_any_caller(
    client, db_matches, auth_headers
):
    # Quirk pin: the "debugging" listing has no ownership filter — any
    # authenticated guest sees the last 50 matches of ALL players, with
    # scores and statuses.
    db_matches.append(_persisted_match_doc("match-priv-1", PLAYER_A, PLAYER_B))
    db_matches.append(_persisted_match_doc("match-priv-2", PLAYER_C, PLAYER_D))

    response = client.get("/matches/all", headers=auth_headers(OUTSIDER))
    assert response.status_code == 200
    body = response.json()
    assert {m["match_id"] for m in body} == {"match-priv-1", "match-priv-2"}
    assert body[0]["score"] == "1-0"


def test_matches_all_needs_no_auth_at_all(client, db_matches):
    # No Authorization header: demo-mode get_current_user still admits the
    # request as the shared guest identity.
    db_matches.append(_persisted_match_doc("match-priv-3"))

    response = client.get("/matches/all")
    assert response.status_code == 200
    assert response.json()[0]["match_id"] == "match-priv-3"


def test_current_behavior_match_details_leak_round_answers_to_outsiders(
    client, db_matches, auth_headers
):
    # BUG pin for the xfail below: /match/{id}/details has NO membership
    # check, and the persisted rounds array embeds the correct answer of the
    # still-unresolved current round.  Anyone with the match_id — including
    # the opponent using a second browser tab — can read the answer mid-round.
    db_matches.append(_persisted_match_doc("match-leak-1", PLAYER_A, PLAYER_B))

    response = client.get("/match/match-leak-1/details", headers=auth_headers(OUTSIDER))
    assert response.status_code == 200
    body = response.json()
    open_round = body["rounds"][1]
    assert open_round["winner"] is None  # round still in play…
    assert open_round["answer"] == "3·x^2"  # …but its answer is readable


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(details-no-authz): /match/{match_id}/details never verifies the "
        "caller is a participant, and its response includes the rounds array "
        "with the correct answer of the open round — a live answer oracle "
        "for opponents and spectators."
    ),
)
def test_match_details_should_reject_non_participants(
    client, db_matches, auth_headers
):
    db_matches.append(_persisted_match_doc("match-leak-2", PLAYER_A, PLAYER_B))

    response = client.get("/match/match-leak-2/details", headers=auth_headers(OUTSIDER))
    assert response.status_code == 403


def test_match_details_needs_no_auth_at_all(client, db_matches):
    db_matches.append(_persisted_match_doc("match-leak-3"))

    response = client.get("/match/match-leak-3/details")
    assert response.status_code == 200
    assert response.json()["match_id"] == "match-leak-3"


# ---------------------------------------------------------------------------
# abandoned vs completed access differences
# ---------------------------------------------------------------------------


def _completed_match(client, auth_headers, p1=PLAYER_A, p2=PLAYER_B):
    match_id = _friend_match(client, auth_headers, p1, p2)
    for _ in range(3):
        _win_round(client, auth_headers, match_id, p1)
    assert main.in_memory_matches[match_id]["status"] == "completed"
    return match_id


def _abandoned_match(client, auth_headers, p1=PLAYER_A, p2=PLAYER_B):
    match_id = _friend_match(client, auth_headers, p1, p2)
    main.in_memory_matches[match_id]["status"] = "abandoned"
    return match_id


def test_completed_match_rejects_question_and_answer_with_400(
    client, auth_headers, fixed_question
):
    match_id = _completed_match(client, auth_headers)

    q = _question(client, auth_headers, match_id, PLAYER_B)
    a = _answer(client, auth_headers, match_id, PLAYER_B)
    assert q.status_code == 400
    assert a.status_code == 400
    assert q.json()["detail"] == "Match is already completed"
    assert a.json()["detail"] == "Match is already completed"


def test_current_behavior_abandoned_match_still_accepts_play(
    client, auth_headers, fixed_question
):
    # BUG pin (zombie matches, xfailed in the presence/lifecycle file): only
    # status == "completed" blocks gameplay, so the SAME player performing the
    # SAME actions gets 400 on a completed match but full service on an
    # abandoned one — new rounds, scoring, everything.
    match_id = _abandoned_match(client, auth_headers)

    assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 200
    body = _answer(client, auth_headers, match_id, PLAYER_A).json()
    assert body["correct"] is True
    assert _scores(match_id) == (1, 0)
    assert main.in_memory_matches[match_id]["status"] == "abandoned"


def test_status_poll_serves_both_abandoned_and_completed(
    client, auth_headers, fixed_question
):
    completed = _completed_match(client, auth_headers, PLAYER_A, PLAYER_B)
    abandoned = _abandoned_match(client, auth_headers, PLAYER_C, PLAYER_D)

    completed_status = _status(client, auth_headers, completed, PLAYER_B).json()
    abandoned_status = _status(client, auth_headers, abandoned, PLAYER_C).json()
    assert completed_status["status"] == "completed"
    assert str(completed_status["winner_id"]) == PLAYER_A
    assert abandoned_status["status"] == "abandoned"
    assert abandoned_status["winner_id"] is None


def test_active_endpoint_hides_both_abandoned_and_completed(
    client, auth_headers, fixed_question
):
    _completed_match(client, auth_headers, PLAYER_A, PLAYER_B)
    _abandoned_match(client, auth_headers, PLAYER_C, PLAYER_D)

    for player in (PLAYER_A, PLAYER_B, PLAYER_C, PLAYER_D):
        body = client.get("/api/game/active", headers=auth_headers(player)).json()
        assert body == {"has_active_match": False}


def test_match_by_code_serves_members_regardless_of_terminal_status(
    client, auth_headers, fixed_question
):
    completed = _completed_match(client, auth_headers, PLAYER_A, PLAYER_B)
    abandoned = _abandoned_match(client, auth_headers, PLAYER_C, PLAYER_D)

    completed_code = main.in_memory_matches[completed]["match_code"]
    abandoned_code = main.in_memory_matches[abandoned]["match_code"]

    done = client.get(
        f"/api/game/match/{completed_code}", headers=auth_headers(PLAYER_A)
    ).json()
    gone = client.get(
        f"/api/game/match/{abandoned_code}", headers=auth_headers(PLAYER_C)
    ).json()
    assert done["status"] == "completed"
    assert gone["status"] == "abandoned"


def test_give_up_on_completed_match_reports_already_ended(
    client, auth_headers, fixed_question
):
    # Quirk pin: give_up_round never checks match status; on a completed
    # match it reaches the (won) final round and answers already_ended
    # instead of the 400 the other gameplay routes give.
    match_id = _completed_match(client, auth_headers)

    body = _give_up(client, auth_headers, match_id, PLAYER_B)
    assert body.status_code == 200
    assert body.json()["status"] == "already_ended"
    assert body.json()["round_winner"] == PLAYER_A


# ---------------------------------------------------------------------------
# spectator with a leaked match_id
# ---------------------------------------------------------------------------


def test_spectator_is_blocked_from_all_gameplay_routes(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    assert _status(client, auth_headers, match_id, SPECTATOR).status_code == 403
    assert _question(client, auth_headers, match_id, SPECTATOR).status_code == 403
    assert _answer(client, auth_headers, match_id, SPECTATOR).status_code == 403
    assert _give_up(client, auth_headers, match_id, SPECTATOR).status_code == 403
    # And none of that left a heartbeat behind.
    assert SPECTATOR not in main.in_memory_matches[match_id]["player_last_seen"]


def test_spectator_can_watch_live_score_through_details_endpoint(
    client, auth_headers, fixed_question
):
    # Quirk pin: the details endpoint's in-memory fallback turns a leaked
    # match_id into a live scoreboard for any spectator, even with the DB
    # down — polls reflect each round as it is won.
    match_id = _friend_match(client, auth_headers)

    before = client.get(
        f"/match/{match_id}/details", headers=auth_headers(SPECTATOR)
    ).json()
    assert before["score"] == "0-0"
    assert before["status"] == "active"

    _win_round(client, auth_headers, match_id, PLAYER_A)

    after = client.get(
        f"/match/{match_id}/details", headers=auth_headers(SPECTATOR)
    ).json()
    assert after["score"] == "1-0"


def test_details_endpoint_upgrades_leaked_match_id_to_match_code(
    client, auth_headers
):
    # Quirk pin: details also reveals the match_code, which unlocks the
    # unauthenticated /api/game/friend/status/{code} poller — so a leaked
    # match_id escalates into anonymous, tokenless status polling.
    match_id = _friend_match(client, auth_headers)

    details = client.get(
        f"/match/{match_id}/details", headers=auth_headers(SPECTATOR)
    ).json()
    leaked_code = details["match_code"]
    assert leaked_code == main.in_memory_matches[match_id]["match_code"]

    anonymous = client.get(f"/api/game/friend/status/{leaked_code}")
    assert anonymous.status_code == 200
    assert anonymous.json()["match_id"] == match_id
