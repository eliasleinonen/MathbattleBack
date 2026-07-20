"""
Newly found (audit pass) edge-case tests for people-vs-people matches.

A second bug-hunting pass over the match code in main.py (friend flow,
start_match, cancel, submit_answer, give_up, get_question, challenges,
get_match_by_code, get_match_details, get_all_matches, presence), diffed
against the fifteen existing edge-case suites and MATCH_EDGE_CASE_REPORT.md.
Every bug below was UNTESTED before this file.

Bugs pinned here (strict xfail + current-behavior companion each):

1. give_up_round's flag-initialization block ERASES the opponent's
   persisted give-up when the round is hydrated from Mongo.  The single
   field `$set` at give-up time means the Mongo round doc carries only the
   giver's flag; after a memory wipe the other player's give-up resets
   both flags to False before setting its own, so the round that should
   tie stays open and memory/Mongo diverge.

2. get_game_status never hydrates the current round from Mongo.  After a
   memory wipe (restart / other worker) a resolved round's winner_id and
   both gave-up flags silently report as None/False even though the round
   document in Mongo has the winner -- a client polling for the round
   result never sees it.

3. A creator can solo-play and solo-COMPLETE a `waiting` (unjoined)
   friend match: gameplay routes only reject `completed`, so the creator
   fetches questions, scores 3 rounds against nobody, the match flips to
   completed 3-0, and the invited friend's join is then rejected with
   "Match already started".

4. /api/game/match/{code} has no DB fallback.  Every other gameplay route
   hydrates a match from Mongo on a memory miss; the by-code lookup scans
   in_memory_matches only, so after a restart (DB healthy!) it 404s until
   some OTHER endpoint happens to cache the match back into memory.

5. The `current_round` field of the by-code response is hardwired to 0:
   the code reads match.get("current_round", 0) but no writer ever sets
   "current_round" (only "current_round_id"), so mid-match the endpoint
   still reports round 0.

6. /api/game/active labels every human guest opponent "AI Opponent":
   the opponent lookup users_collection.find_one misses for guest ids and
   the fallback string assumes the opponent is a bot, while the status
   endpoint labels the same human "Player 2".

Conventions match the sibling edge-case files: guest identities via
"Bearer guest-xxx" tokens; strict xfail for the desired behavior plus a
sibling test pinning the CURRENT behavior so regressions in either
direction are visible.
"""

import copy
from datetime import datetime

import pytest

import main


PLAYER_A = "guest-nfb-aaa"
PLAYER_B = "guest-nfb-bbb"

CORRECT = "2*x"  # matches fixed_question's stored answer "2·x"


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


class _UpdateResult:
    modified_count = 1
    matched_count = 1
    upserted_id = None


@pytest.fixture
def applying_rounds_db(mock_mongo, monkeypatch):
    """rounds_collection fake that APPLIES insert_one and `$set` updates.

    Unlike the write-discarding conftest mocks, this reproduces what a real
    Mongo round document contains after the production code's single-field
    `$set` writes -- which is exactly what the hydration bugs depend on.
    Returns deepcopies like Motor materializing a fresh doc per query.
    """
    docs = {}

    async def find_one(query, *args, **kwargs):
        doc = docs.get(query.get("_id"))
        return copy.deepcopy(doc) if doc is not None else None

    async def insert_one(doc, *args, **kwargs):
        docs[doc["_id"]] = copy.deepcopy(doc)

    async def update_one(query, update, *args, **kwargs):
        doc = docs.get(query.get("_id"))
        if doc is not None:
            for key, value in update.get("$set", {}).items():
                if "." not in key:
                    doc[key] = copy.deepcopy(value)
        return _UpdateResult()

    monkeypatch.setattr(main.rounds_collection, "find_one", find_one)
    monkeypatch.setattr(main.rounds_collection, "insert_one", insert_one)
    monkeypatch.setattr(main.rounds_collection, "update_one", update_one)
    return docs


@pytest.fixture
def fake_matches_db(mock_mongo, monkeypatch):
    """matches_collection.find_one backed by a dict keyed on _id."""
    docs = {}

    async def find_one(query, *args, **kwargs):
        doc = None
        if "_id" in query:
            doc = docs.get(query["_id"])
        elif "match_code" in query:
            for candidate in docs.values():
                if candidate.get("match_code") == query["match_code"]:
                    doc = candidate
                    break
        return copy.deepcopy(doc) if doc is not None else None

    monkeypatch.setattr(main.matches_collection, "find_one", find_one)
    return docs


def _db_match_doc(match_id, p1=PLAYER_A, p2=PLAYER_B, **overrides):
    """A match document shaped like Motor would return it."""
    doc = {
        "_id": match_id,
        "match_code": "NFBHYD",
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
    return client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(player)
    )


def _give_up(client, auth_headers, match_id, player):
    return client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )


# ---------------------------------------------------------------------------
# Bug 1: give-up flag erasure on a Mongo-hydrated round
# ---------------------------------------------------------------------------
#
# give_up_round contains:
#
#     if give_up_field not in round_doc:
#         round_doc["player1_gave_up"] = False
#         round_doc["player2_gave_up"] = False
#
# When a player gives up, only THEIR flag is `$set` to Mongo (the initial
# False pair lives in memory only).  So a Mongo round doc after one give-up
# has e.g. player2_gave_up=True and NO player1_gave_up key.  If the round
# is later hydrated from Mongo (restart, eviction, another worker) and the
# OTHER player gives up, their missing key triggers the init block, which
# resets the opponent's persisted True back to False -- the round that
# should tie stays open, and memory/Mongo diverge about who gave up.


def _one_sided_give_up_then_wipe(client, auth_headers, applying_rounds_db):
    """B gives up on round 1, then the rounds cache is wiped (restart)."""
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()[
        "round_id"
    ]

    body = _give_up(client, auth_headers, match_id, PLAYER_B).json()
    assert body == {"status": "gave_up", "waiting_for_opponent": True}

    # Precondition (faithful to production Mongo): the persisted round doc
    # carries only the giver's flag -- the False initialization of the other
    # flag was never written.
    db_round = applying_rounds_db[round_id]
    assert db_round["player2_gave_up"] is True
    assert "player1_gave_up" not in db_round

    main.in_memory_rounds.clear()  # simulated restart / cache eviction
    return match_id, round_id


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(give-up-erasure): give_up_round's flag-initialization block "
        "resets BOTH gave-up flags whenever the caller's own key is missing "
        "from the round doc.  A round hydrated from Mongo carries only the "
        "opponent's persisted give-up flag, so the caller's give-up erases "
        "it instead of completing the both-gave-up tie."
    ),
)
def test_hydrated_opponent_give_up_should_survive_and_tie_the_round(
    client, auth_headers, fixed_question, applying_rounds_db
):
    match_id, _ = _one_sided_give_up_then_wipe(
        client, auth_headers, applying_rounds_db
    )

    # A gives up too.  B's give-up is in Mongo, so both players have now
    # given up and the round should resolve to the tie.
    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    assert body["status"] == "both_gave_up"
    assert body["round_winner"] == "tie"


def test_current_behavior_hydrated_give_up_erases_opponent_flag(
    client, auth_headers, fixed_question, applying_rounds_db
):
    # BUG pin for the xfail above, step by step.
    match_id, round_id = _one_sided_give_up_then_wipe(
        client, auth_headers, applying_rounds_db
    )

    body = _give_up(client, auth_headers, match_id, PLAYER_A).json()
    # The round that should have tied is still waiting...
    assert body == {"status": "gave_up", "waiting_for_opponent": True}
    # ...because the init block erased B's give-up in the hydrated copy.
    memory_round = main.in_memory_rounds[round_id]
    assert memory_round["player1_gave_up"] is True
    assert memory_round["player2_gave_up"] is False

    # Meanwhile Mongo now says BOTH gave up (A's `$set` landed on top of
    # B's persisted flag) -- memory and DB disagree about the round state.
    db_round = applying_rounds_db[round_id]
    assert db_round["player1_gave_up"] is True
    assert db_round["player2_gave_up"] is True

    # B must give up a SECOND time to get the tie they already earned.
    second = _give_up(client, auth_headers, match_id, PLAYER_B).json()
    assert second["status"] == "both_gave_up"
    assert second["round_winner"] == "tie"


# ---------------------------------------------------------------------------
# Bug 2: get_game_status never hydrates the round -> resolved round invisible
# ---------------------------------------------------------------------------
#
# get_game_status only reads round state via
# `if current_round_id and current_round_id in in_memory_rounds:` -- there
# is no rounds_collection fallback (unlike submit_answer and give_up_round,
# which both hydrate the round).  After a memory wipe, the winner of the
# still-current round is in Mongo but the status poll reports
# round_winner: None and both gave-up flags False, so a client waiting on
# the poll for the round result never sees it.


def _won_round_then_wipe(client, auth_headers, applying_rounds_db):
    match_id = _friend_match(client, auth_headers)
    round_id = _question(client, auth_headers, match_id, PLAYER_A).json()[
        "round_id"
    ]
    body = _answer(client, auth_headers, match_id, PLAYER_A).json()
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER_A

    # Sanity: before the wipe, the poll shows the round winner...
    assert (
        _status(client, auth_headers, match_id, PLAYER_B).json()["round_winner"]
        == PLAYER_A
    )
    # ...and the winner is durably persisted in Mongo.
    assert applying_rounds_db[round_id]["winner_id"] == PLAYER_A

    main.in_memory_rounds.clear()  # simulated restart / cache eviction
    return match_id, round_id


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(status-no-round-hydration): get_game_status reads the current "
        "round only from in_memory_rounds, with no rounds_collection "
        "fallback.  After a memory wipe the resolved round's winner_id is "
        "in Mongo but the status poll reports round_winner: None, so a "
        "client polling for the round result never advances."
    ),
)
def test_status_should_report_round_winner_persisted_in_mongo(
    client, auth_headers, fixed_question, applying_rounds_db
):
    match_id, _ = _won_round_then_wipe(client, auth_headers, applying_rounds_db)

    body = _status(client, auth_headers, match_id, PLAYER_B).json()
    assert body["round_winner"] == PLAYER_A


def test_current_behavior_round_result_vanishes_from_status_after_wipe(
    client, auth_headers, fixed_question, applying_rounds_db
):
    # BUG pin for the xfail above.
    match_id, round_id = _won_round_then_wipe(
        client, auth_headers, applying_rounds_db
    )

    body = _status(client, auth_headers, match_id, PLAYER_B).json()
    # The score survived (it lives on the match doc)...
    assert body["player1_score"] == 1
    # ...but the round result is gone from the poll, even though Mongo
    # still knows the winner under the match's live current_round_id.
    assert body["round_winner"] is None
    assert body["player1_gave_up"] is False
    assert body["player2_gave_up"] is False
    assert (
        main.in_memory_matches[match_id]["current_round_id"] == round_id
    )
    assert applying_rounds_db[round_id]["winner_id"] == PLAYER_A


# ---------------------------------------------------------------------------
# Bug 3: a creator can solo-complete a waiting (unjoined) friend match
# ---------------------------------------------------------------------------
#
# Gameplay routes only reject status "completed".  A `waiting` friend match
# that nobody has joined serves questions to its creator, and the friend-
# match branch of submit_answer awards the round to whoever answers first
# -- i.e. always the creator, playing alone.  Three correct answers
# complete the match 3-0 with a winner, after which the invited friend's
# join is bounced with "Match already started".


def _waiting_match(client, auth_headers, creator=PLAYER_A):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(creator)
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "waiting"
    return body


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(waiting-match-solo-play): gameplay routes only reject status "
        "'completed', so the creator of an unjoined `waiting` friend match "
        "can fetch questions and score points against nobody."
    ),
)
def test_answer_on_unjoined_waiting_match_should_not_score(
    client, auth_headers, fixed_question
):
    match_id = _waiting_match(client, auth_headers)["match_id"]

    # Whether the fix rejects the question or the answer, no point may be
    # awarded on a match nobody has joined.
    question = _question(client, auth_headers, match_id, PLAYER_A)
    if question.status_code == 200:
        _answer(client, auth_headers, match_id, PLAYER_A)

    match = main.in_memory_matches[match_id]
    assert match["player1_score"] == 0
    assert match["status"] != "completed"


def test_current_behavior_creator_solo_completes_waiting_match(
    client, auth_headers, fixed_question
):
    # BUG pin for the xfail above: the full solo 3-0 walkthrough.
    created = _waiting_match(client, auth_headers)
    match_id = created["match_id"]

    for expected_score in (1, 2, 3):
        question = _question(client, auth_headers, match_id, PLAYER_A)
        assert question.status_code == 200  # waiting matches serve questions
        body = _answer(client, auth_headers, match_id, PLAYER_A).json()
        assert body["correct"] is True
        assert body["round_winner"] == PLAYER_A
        assert body["player1_score"] == expected_score

    # The never-joined match is now completed with a winner.
    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["winner_id"] == PLAYER_A
    assert match["player2_id"] is None  # nobody ever joined

    # And the invited friend is locked out with a misleading error.
    join = client.post(
        "/api/game/friend/join",
        json={"match_code": created["match_code"]},
        headers=auth_headers(PLAYER_B),
    )
    assert join.status_code == 400
    assert join.json()["detail"] == "Match already started"


# ---------------------------------------------------------------------------
# Bug 4: /api/game/match/{code} has no DB fallback
# ---------------------------------------------------------------------------
#
# question/answer/give-up/status all hydrate a match from Mongo when it is
# missing from memory; get_match_by_code scans in_memory_matches only.
# After a process restart with a healthy DB, the by-code lookup 404s until
# some other endpoint happens to cache the match back.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(by-code-no-db-fallback): get_match_by_code scans "
        "in_memory_matches only.  A live match that is present in Mongo "
        "but not in memory (restart / other worker) 404s on the by-code "
        "route while every other gameplay route hydrates it fine."
    ),
)
def test_match_by_code_should_hydrate_from_db_like_other_routes(
    client, auth_headers, fake_matches_db
):
    fake_matches_db["match-nfb-code"] = _db_match_doc(
        "match-nfb-code", match_code="NFBCODE1"
    )
    assert "match-nfb-code" not in main.in_memory_matches

    response = client.get(
        "/api/game/match/NFBCODE1", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 200
    assert response.json()["match_id"] == "match-nfb-code"


def test_current_behavior_by_code_404s_until_another_route_hydrates(
    client, auth_headers, fake_matches_db
):
    # BUG pin for the xfail above: the same code 404s, then magically works
    # once the status route has cached the match back into memory.
    fake_matches_db["match-nfb-code2"] = _db_match_doc(
        "match-nfb-code2", match_code="NFBCODE2"
    )

    miss = client.get(
        "/api/game/match/NFBCODE2", headers=auth_headers(PLAYER_A)
    )
    assert miss.status_code == 404

    hydrate = _status(client, auth_headers, "match-nfb-code2", PLAYER_A)
    assert hydrate.status_code == 200  # status DOES fall back to the DB

    hit = client.get(
        "/api/game/match/NFBCODE2", headers=auth_headers(PLAYER_A)
    )
    assert hit.status_code == 200
    assert hit.json()["match_id"] == "match-nfb-code2"


# ---------------------------------------------------------------------------
# Bug 5: the by-code response's current_round is hardwired to 0
# ---------------------------------------------------------------------------
#
# get_match_by_code returns match.get("current_round", 0), but nothing in
# the codebase ever writes a "current_round" key -- rounds are tracked via
# "current_round_id".  The field is therefore 0 forever, no matter how
# deep into the match the players are.


def _match_on_round_two(client, auth_headers, fixed_question):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    assert _answer(client, auth_headers, match_id, PLAYER_A).json()["correct"]
    second = _question(client, auth_headers, match_id, PLAYER_A).json()
    assert second["round_id"] == f"round-{match_id}-2"
    return match_id


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(current-round-never-written): get_match_by_code reads "
        "match.get('current_round', 0) but no writer ever sets "
        "'current_round' (only 'current_round_id'), so the field is 0 "
        "forever, even mid-match."
    ),
)
def test_by_code_current_round_should_track_round_progression(
    client, auth_headers, fixed_question
):
    match_id = _match_on_round_two(client, auth_headers, fixed_question)
    code = main.in_memory_matches[match_id]["match_code"]

    body = client.get(
        f"/api/game/match/{code}", headers=auth_headers(PLAYER_A)
    ).json()
    assert body["current_round"] != 0


def test_current_behavior_by_code_current_round_stuck_at_zero(
    client, auth_headers, fixed_question
):
    # BUG pin for the xfail above: round 2 is live, the field still says 0.
    match_id = _match_on_round_two(client, auth_headers, fixed_question)
    match = main.in_memory_matches[match_id]
    assert match["current_round_id"] == f"round-{match_id}-2"

    body = client.get(
        f"/api/game/match/{match['match_code']}",
        headers=auth_headers(PLAYER_A),
    ).json()
    assert body["current_round"] == 0
    assert body["player1_score"] == 1  # the match is demonstrably mid-game


# ---------------------------------------------------------------------------
# Bug 6: /api/game/active labels human guest opponents "AI Opponent"
# ---------------------------------------------------------------------------
#
# get_active_match resolves the opponent with users_collection.find_one,
# which misses for guest ids, and the fallback string is "AI Opponent" --
# a label the rest of the API reserves for the bot.  A friend match
# between two humans therefore tells each of them they are playing an AI,
# while the status endpoint labels the very same opponent "Player 2".


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(active-mislabels-humans-as-bot): get_active_match falls back "
        "to the string 'AI Opponent' whenever the opponent has no user "
        "document, which is every guest.  A human friend-match opponent "
        "must not be labeled as a bot."
    ),
)
def test_active_match_should_not_label_human_guest_as_ai(
    client, auth_headers
):
    _friend_match(client, auth_headers)

    body = client.get(
        "/api/game/active", headers=auth_headers(PLAYER_A)
    ).json()
    assert body["has_active_match"] is True
    assert body["opponent"] != "AI Opponent"


def test_current_behavior_active_calls_human_guest_ai_opponent(
    client, auth_headers
):
    # BUG pin for the xfail above, plus the internal inconsistency: the
    # status endpoint labels the same human "Player 2" in the same match.
    match_id = _friend_match(client, auth_headers)

    active = client.get(
        "/api/game/active", headers=auth_headers(PLAYER_A)
    ).json()
    assert active["has_active_match"] is True
    assert active["match_type"] == "friend"  # a human-vs-human match...
    assert active["opponent"] == "AI Opponent"  # ...labeled as a bot

    status = _status(client, auth_headers, match_id, PLAYER_A).json()
    assert status["player2_name"] == "Player 2"  # same opponent, same match
