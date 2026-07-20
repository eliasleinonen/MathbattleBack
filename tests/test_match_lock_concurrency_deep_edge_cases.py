"""
Deep lock/concurrency edge cases for the people-match flows.

This suite deepens the concurrency coverage around the ONE lock the match
code has (`match_locks` / `get_match_lock`, used only by `get_question`) and
the three mutating endpoints that DON'T take it (`submit_answer`,
`give_up_round`, `join_friend_match`), plus the queue endpoints:

 1. get_question lock serialization, varied: a 10-caller stampede that
    yields mid-creation, a concurrent 300s-expiry tie, and a same-player
    burst - all must produce exactly one (new) round.
 2. submit_answer without the lock: double-score variants under different
    hydration latencies - symmetric, staggered, same-player double-submit,
    and a late WRONG answer whose hydration write-back reopens a decided
    round (new bug 42).
 3. give_up_round without the lock: lost-update variants - a fully
    ACKNOWLEDGED give-up erased by a slower hydrating give-up, and the
    manual second-give-up recovery path.
 4. join_friend_match without the lock: overwrite variants - four-way join
    race (all 200, last writer keeps the seat + elo, first "successful"
    joiner is locked out with 403), creator-vs-joiner race.
 5. Mixed: answer + get_question concurrent right after a round win - the
    answer can blind-snipe the freshly created round before its
    round_start_time, or land mid-creation while the lock is held so the
    question is already won when delivered.
 6. Mixed: give_up + answer concurrent on a cache-missed round - the
    give-up's hydration write-back erases the answer's recorded winner
    while the score sticks (bug 42 sibling).
 7. match_locks growth: one lock per questioned match, and neither
    completion, abandonment nor match-doc eviction ever cleans them up.
 8. Same-lock-object identity: get_match_lock returns the identical object
    across calls/rounds/lifecycle, and get_question really blocks on THAT
    object (acquiring it by hand stalls the endpoint until release).
 9. Concurrent status polls never corrupt state - but a poll interleaved
    with a winning answer serves a TORN payload (new score, stale
    round_winner).
10. Concurrent cancel + start: cancel losing the race leaves the pair
    matched with lingering cancel flags; cancel winning it re-queues
    cleanly; a same-tick start+cancel plants the stale flag that later
    eats a pairing (bug 6, concurrent framing).

Conventions (same as the sibling suites): guest identities via
"Bearer guest-xxx" tokens; "simultaneous" arrivals are coroutines gathered
on one event loop (single-uvicorn-worker semantics); DB latency is
simulated by monkeypatched Motor methods that await asyncio.sleep before
returning a snapshot copy. Known bugs are pinned with strict xfails plus
sibling current-behavior pins. See MATCH_EDGE_CASE_REPORT.md ("Deep
lock/concurrency suite").
"""

import asyncio
import copy
from datetime import datetime, timedelta

import pytest

import main


PLAYER_A = "guest-lockdeep-a"
PLAYER_B = "guest-lockdeep-b"
PLAYER_C = "guest-lockdeep-c"
PLAYER_D = "guest-lockdeep-d"
PLAYER_E = "guest-lockdeep-e"

CORRECT = "2*x"  # fixed_question's stored answer is "2·x"
WRONG = "7"


# ---------------------------------------------------------------------------
# helpers: HTTP
# ---------------------------------------------------------------------------


def _start(client, auth_headers, player):
    response = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _question(client, auth_headers, match_id, player, expect=200):
    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert response.status_code == expect, response.text
    return response.json()


def _answer(client, auth_headers, match_id, player, answer, expect=200):
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )
    assert response.status_code == expect, response.text
    return response.json()


def _give_up(client, auth_headers, match_id, player):
    response = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _status(client, auth_headers, match_id, player):
    response = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_friend(client, auth_headers, creator):
    response = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(creator)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _friend_match(client, auth_headers, creator=PLAYER_A, joiner=PLAYER_B):
    created = _create_friend(client, auth_headers, creator)
    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": created["match_code"]},
        headers=auth_headers(joiner),
    )
    assert joined.status_code == 200, joined.text
    return created["match_id"]


def _win_round(client, auth_headers, match_id, winner):
    _question(client, auth_headers, match_id, winner)
    body = _answer(client, auth_headers, match_id, winner, CORRECT)
    assert body["correct"] is True, body
    return body


def _match_rounds(match_id):
    return [r for r in main.in_memory_rounds.values() if r["match_id"] == match_id]


# ---------------------------------------------------------------------------
# helpers: latency-injecting DB mocks
# ---------------------------------------------------------------------------


def _evict_round_with_latent_reads(monkeypatch, round_id, delays):
    """Pop the round from memory; serve deepcopies from a fake DB read that
    sleeps `delays[i]` on the i-th call (last delay repeats)."""
    snapshot = copy.deepcopy(main.in_memory_rounds.pop(round_id))
    calls = {"n": 0}

    async def find_one(query, *args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        await asyncio.sleep(delays[min(i, len(delays) - 1)])
        if query.get("_id") == round_id:
            return copy.deepcopy(snapshot)
        return None

    monkeypatch.setattr(main.rounds_collection, "find_one", find_one)
    return snapshot


def _yielding(monkeypatch, collection, method, delay=0):
    async def slow(*args, **kwargs):
        await asyncio.sleep(delay)
        return None

    monkeypatch.setattr(getattr(main, collection), method, slow)


def _run(*coros):
    """Gather route coroutines on a fresh event loop, exceptions captured."""

    async def go():
        return await asyncio.gather(*coros, return_exceptions=True)

    return asyncio.run(go())


# ===========================================================================
# 1. get_question lock serialization - varied
# ===========================================================================


def test_question_stampede_of_ten_creates_exactly_one_round(
    client, auth_headers, fixed_question, monkeypatch
):
    # Ten concurrent first-question calls (5 per player) while round creation
    # itself yields mid-write (insert + match update both sleep). The creator
    # suspends INSIDE the critical section; everyone else must queue on the
    # lock and then resume the one existing round.
    match_id = _friend_match(client, auth_headers)
    main.match_locks.pop(match_id, None)  # fresh lock for this event loop
    _yielding(monkeypatch, "rounds_collection", "insert_one")
    _yielding(monkeypatch, "matches_collection", "update_one")

    results = _run(
        *[
            main.get_question(
                match_id, current_user={"_id": PLAYER_A if i % 2 else PLAYER_B}
            )
            for i in range(10)
        ]
    )

    assert all(not isinstance(r, Exception) for r in results)
    assert {r["round_id"] for r in results} == {f"round-{match_id}-1"}
    assert {r["expression"] for r in results} == {"x^2"}
    assert {r["round_start_time"] for r in results} == {
        main.in_memory_matches[match_id]["round_start_time"]
    }
    assert len(_match_rounds(match_id)) == 1


def test_concurrent_questions_on_expired_round_tie_it_once_and_fork_nothing(
    client, auth_headers, fixed_question, monkeypatch
):
    # Both players poll a round that crossed the 300s expiry at the same
    # moment. Exactly one caller must tie round 1 and create round 2; the
    # other resumes round 2 - never a second tie, never a third round.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_1 = f"round-{match_id}-1"
    main.in_memory_rounds[round_1]["created_at"] = main.utc_now() - timedelta(
        seconds=301
    )
    main.match_locks.pop(match_id, None)
    _yielding(monkeypatch, "rounds_collection", "update_one")
    _yielding(monkeypatch, "matches_collection", "update_one")

    result_a, result_b = _run(
        main.get_question(match_id, current_user={"_id": PLAYER_A}),
        main.get_question(match_id, current_user={"_id": PLAYER_B}),
    )

    assert result_a["round_id"] == result_b["round_id"] == f"round-{match_id}-2"
    assert main.in_memory_rounds[round_1]["winner_id"] == "tie"
    assert len(_match_rounds(match_id)) == 2
    assert main.in_memory_rounds[f"round-{match_id}-2"]["winner_id"] is None


def test_same_player_question_burst_with_slow_writes_shares_one_round(
    client, auth_headers, fixed_question, monkeypatch
):
    # One client re-firing the question request 6 times (retry storm) while
    # the DB writes yield: the lock still collapses the burst to one round.
    match_id = _friend_match(client, auth_headers)
    main.match_locks.pop(match_id, None)
    _yielding(monkeypatch, "rounds_collection", "insert_one")
    _yielding(monkeypatch, "matches_collection", "update_one")

    results = _run(
        *[
            main.get_question(match_id, current_user={"_id": PLAYER_A})
            for _ in range(6)
        ]
    )

    assert {r["round_id"] for r in results} == {f"round-{match_id}-1"}
    assert len(_match_rounds(match_id)) == 1


# ===========================================================================
# 2. submit_answer takes no lock - double-score latency variants
# ===========================================================================


def test_symmetric_hydration_race_pays_both_players_for_one_round(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG(double-score, bug 2 deepened): mid-match (0-0, far from match
    # point) both players submit correct answers while the round doc is a
    # cache miss. Equal hydration latency -> both read a winnerless copy,
    # both pass the winner_id gate, both score: 1-1 from a single round.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"
    _evict_round_with_latent_reads(monkeypatch, round_id, delays=[0, 0])

    result_a, result_b = _run(
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_A},
        ),
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_B},
        ),
    )

    assert result_a["correct"] is True and result_b["correct"] is True
    assert result_a["round_winner"] == PLAYER_A
    assert result_b["round_winner"] == PLAYER_B
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (1, 1)
    assert len(_match_rounds(match_id)) == 1  # one round, two points


def test_staggered_hydration_race_still_double_pays_and_slow_racer_owns_round(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG(double-score, staggered variant): the second reader's hydration
    # returns only AFTER the first racer fully scored and returned. The
    # stale copy it writes back erases the first winner, it scores anyway,
    # and the round doc ends up crediting only the SLOW racer while the
    # match paid both.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"
    _evict_round_with_latent_reads(monkeypatch, round_id, delays=[0, 0.01])

    result_a, result_b = _run(
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_A},
        ),
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_B},
        ),
    )

    assert result_a["round_winner"] == PLAYER_A
    assert result_b["round_winner"] == PLAYER_B
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (1, 1)
    # The surviving round doc remembers only the slow racer's win; the fast
    # racer's acknowledged round win exists nowhere but in the score.
    assert str(main.in_memory_rounds[round_id]["winner_id"]) == PLAYER_B
    # A late third submit is bounced by the slow racer's winner_id.
    late = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert late["already_won"] is True


def test_same_player_double_submit_race_scores_twice(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG(double-score, same-player variant): one player's duplicated
    # request (double-click / client retry) hydrating a cache-missed round
    # pays that player TWO points for one round.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"
    _evict_round_with_latent_reads(monkeypatch, round_id, delays=[0, 0])

    first, second = _run(
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_A},
        ),
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_A},
        ),
    )

    assert first["correct"] is True and second["correct"] is True
    assert (first["player1_score"], second["player1_score"]) == (1, 2)
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (2, 0)
    assert len(_match_rounds(match_id)) == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(hydration-reopens-decided-round): submit_answer hydrates a "
        "cache-missed round with `in_memory_rounds[round_id] = round_doc` "
        "BEFORE grading, so a slow WRONG answer racing a correct one "
        "clobbers the recorded winner_id with its stale winnerless copy. "
        "The decided round becomes replayable and the winner can score it "
        "again - points per round are unbounded. The round must stay "
        "decided once a winner was acknowledged."
    ),
)
def test_late_wrong_answer_should_not_reopen_a_decided_round(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"
    _evict_round_with_latent_reads(monkeypatch, round_id, delays=[0, 0.01])

    _run(
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_A},
        ),
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=WRONG),
            current_user={"_id": PLAYER_B},
        ),
    )

    assert main.in_memory_rounds[round_id].get("winner_id") is not None


def test_current_behavior_late_wrong_answer_reopens_the_round_for_a_second_win(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG pin: current behavior of the xfail above - the loser's late wrong
    # answer erases the winner and the winner re-scores the SAME round.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"
    _evict_round_with_latent_reads(monkeypatch, round_id, delays=[0, 0.01])

    win, wrong = _run(
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_A},
        ),
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=WRONG),
            current_user={"_id": PLAYER_B},
        ),
    )
    assert win["round_winner"] == PLAYER_A and win["player1_score"] == 1
    assert wrong["correct"] is False

    # The wrong answer's hydration write-back erased the decided winner...
    assert main.in_memory_rounds[round_id].get("winner_id") is None
    # ...so the winner scores round 1 AGAIN: two points from one round.
    again = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert again["correct"] is True
    assert again["player1_score"] == 2
    assert len(_match_rounds(match_id)) == 1


# ===========================================================================
# 3. give_up_round takes no lock - lost-update variants
# ===========================================================================


def test_acknowledged_give_up_is_erased_by_a_slower_hydrating_give_up(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG(giveup-lost-update, staggered variant of bug 34): player A's
    # give-up COMPLETES and is acknowledged before player B's hydration
    # even returns - no interleaved flag writes at all - yet B's stale
    # write-back still erases A's flag. The erasure window is the whole
    # hydration latency, not just a simultaneous write race.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"
    _evict_round_with_latent_reads(monkeypatch, round_id, delays=[0, 0.01])

    result_a, result_b = _run(
        main.give_up_round(match_id, current_user={"_id": PLAYER_A}),
        main.give_up_round(match_id, current_user={"_id": PLAYER_B}),
    )

    # Both players gave up, both were told to wait for the opponent.
    assert result_a == {"status": "gave_up", "waiting_for_opponent": True}
    assert result_b == {"status": "gave_up", "waiting_for_opponent": True}
    survivor = main.in_memory_rounds[round_id]
    assert survivor["player1_gave_up"] is False  # A's acknowledged flag lost
    assert survivor["player2_gave_up"] is True
    assert survivor.get("winner_id") is None  # the round is stuck open


def test_stuck_double_give_up_recovers_only_by_giving_up_again(
    client, auth_headers, fixed_question, monkeypatch
):
    # Recovery pin for the lost update above: the round stays open until
    # the erased player repeats their give-up, which finally ties it.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"
    _evict_round_with_latent_reads(monkeypatch, round_id, delays=[0, 0.01])

    _run(
        main.give_up_round(match_id, current_user={"_id": PLAYER_A}),
        main.give_up_round(match_id, current_user={"_id": PLAYER_B}),
    )
    assert main.in_memory_rounds[round_id].get("winner_id") is None

    retry = _give_up(client, auth_headers, match_id, PLAYER_A)
    assert retry["status"] == "both_gave_up"
    assert retry["round_winner"] == "tie"
    assert main.in_memory_rounds[round_id]["winner_id"] == "tie"
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (0, 0)


# ===========================================================================
# 4. join_friend_match takes no lock - overwrite variants
# ===========================================================================


def _latent_join_reads(monkeypatch):
    """join_friend_match's DB read: snapshot the in-memory doc, yield, return."""

    async def find_one(query, *args, **kwargs):
        snapshot = None
        for m in main.in_memory_matches.values():
            if m.get("match_code") == query.get("match_code"):
                snapshot = copy.deepcopy(m)
                break
        await asyncio.sleep(0)
        return snapshot

    monkeypatch.setattr(main.matches_collection, "find_one", find_one)


def test_four_way_join_race_all_succeed_and_last_writer_keeps_the_seat(
    client, auth_headers, monkeypatch
):
    # BUG(join-overwrite, bug 4 deepened): with any DB read latency, FOUR
    # concurrent joiners all read status == "waiting", all pass the checks,
    # all get 200/active - and the seat (id AND elo snapshot) belongs to
    # whichever coroutine wrote last.
    created = _create_friend(client, auth_headers, PLAYER_A)
    match_id = created["match_id"]
    _latent_join_reads(monkeypatch)

    joiners = [
        (PLAYER_B, 1000),
        (PLAYER_C, 1100),
        (PLAYER_D, 1200),
        (PLAYER_E, 1300),
    ]
    results = _run(
        *[
            main.join_friend_match(
                main.FriendMatchJoin(match_code=created["match_code"]),
                current_user={"_id": joiner, "elo": elo},
            )
            for joiner, elo in joiners
        ]
    )

    assert all(not isinstance(r, Exception) for r in results), results
    assert all(r == {"match_id": match_id, "status": "active"} for r in results)
    final = main.in_memory_matches[match_id]
    assert str(final["player2_id"]) == PLAYER_E  # last writer wins
    assert final["player2_elo"] == 1300  # elo snapshot follows the seat


def test_join_race_locks_the_first_acknowledged_joiner_out_of_the_match(
    client, auth_headers, monkeypatch
):
    # BUG pin: the first joiner got a 200 naming this match_id, but every
    # follow-up call they make is rejected as an outsider (403) because the
    # last writer overwrote their seat.
    created = _create_friend(client, auth_headers, PLAYER_A)
    match_id = created["match_id"]
    _latent_join_reads(monkeypatch)

    _run(
        main.join_friend_match(
            main.FriendMatchJoin(match_code=created["match_code"]),
            current_user={"_id": PLAYER_B, "elo": 1000},
        ),
        main.join_friend_match(
            main.FriendMatchJoin(match_code=created["match_code"]),
            current_user={"_id": PLAYER_C, "elo": 1000},
        ),
    )
    assert str(main.in_memory_matches[match_id]["player2_id"]) == PLAYER_C

    denied = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_B)
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Not your match"
    # The evicted joiner's by-code poll still says the match is active -
    # they have no way to learn they were kicked.
    by_code = client.get(f"/api/game/friend/status/{created['match_code']}")
    assert by_code.json()["status"] == "active"


def test_creator_racing_a_joiner_is_rejected_and_the_joiner_seats(
    client, auth_headers, monkeypatch
):
    # The self-join guard is snapshot-independent, so even under the racy
    # hydrated read the creator is rejected and the real joiner seats.
    created = _create_friend(client, auth_headers, PLAYER_A)
    _latent_join_reads(monkeypatch)

    creator_result, joiner_result = _run(
        main.join_friend_match(
            main.FriendMatchJoin(match_code=created["match_code"]),
            current_user={"_id": PLAYER_A, "elo": 1000},
        ),
        main.join_friend_match(
            main.FriendMatchJoin(match_code=created["match_code"]),
            current_user={"_id": PLAYER_B, "elo": 1000},
        ),
    )

    assert isinstance(creator_result, main.HTTPException)
    assert creator_result.status_code == 400
    assert creator_result.detail == "Cannot join your own match"
    assert joiner_result["status"] == "active"
    final = main.in_memory_matches[created["match_id"]]
    assert str(final["player2_id"]) == PLAYER_B
    assert final["status"] == "active"


# ===========================================================================
# 5. mixed: answer + get_question concurrent after a round win
# ===========================================================================


def test_answer_racing_next_question_blind_snipes_the_new_round(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG/quirk: right after A wins round 1, B's next-question poll and B's
    # (retried) answer arrive together. The question creates round 2 under
    # the lock; the answer - which takes NO lock - then grades against
    # round 2 and wins it although B never saw the question and the round's
    # synchronized start is still ~3s in the future.
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    main.match_locks.pop(match_id, None)

    question, answer = _run(
        main.get_question(match_id, current_user={"_id": PLAYER_B}),
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_B},
        ),
    )

    round_2 = f"round-{match_id}-2"
    assert question["round_id"] == round_2
    assert answer["correct"] is True
    assert answer["round_winner"] == PLAYER_B
    # The delivered round was won before its start time ever arrived.
    assert str(main.in_memory_rounds[round_2]["winner_id"]) == PLAYER_B
    round_start = main.parse_round_start(question["round_start_time"])
    assert round_start > main.utc_now()
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (1, 1)


def test_answer_ordered_before_question_bounces_off_the_won_round(
    client, auth_headers, fixed_question
):
    # Opposite interleaving: the answer lands first, hits round 1's winner
    # gate ("already_won"), and only then does the question roll round 2.
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    main.match_locks.pop(match_id, None)

    answer, question = _run(
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_B},
        ),
        main.get_question(match_id, current_user={"_id": PLAYER_B}),
    )

    assert answer["already_won"] is True
    assert answer["round_winner"] == PLAYER_A
    assert question["round_id"] == f"round-{match_id}-2"
    assert main.in_memory_rounds[f"round-{match_id}-2"]["winner_id"] is None
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (1, 0)


def test_answer_landing_mid_creation_wins_the_round_before_it_is_delivered(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG/quirk: _create_next_round publishes current_round_id to memory
    # BEFORE its match-doc write. If that write yields while the lock is
    # still held, a lockless answer slips in, wins the half-created round,
    # and the question response then hands out an already-decided round.
    match_id = _friend_match(client, auth_headers)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    main.match_locks.pop(match_id, None)
    _yielding(monkeypatch, "matches_collection", "update_one")

    question, answer = _run(
        main.get_question(match_id, current_user={"_id": PLAYER_B}),
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_B},
        ),
    )

    round_2 = f"round-{match_id}-2"
    assert question["round_id"] == round_2
    assert answer["round_winner"] == PLAYER_B
    # The question was delivered for a round that was already won.
    assert str(main.in_memory_rounds[round_2]["winner_id"]) == PLAYER_B


# ===========================================================================
# 6. mixed: give_up + answer concurrent on a cache-missed round
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(giveup-erases-answer-winner): give_up_round hydrates a "
        "cache-missed round via an awaited find_one and writes the stale "
        "copy back into in_memory_rounds. Racing a correct answer, the "
        "give-up's write-back erases the answer's recorded winner_id while "
        "the match score already moved - the paid round reopens. A round "
        "won by an answer must keep its winner."
    ),
)
def test_concurrent_give_up_should_not_erase_the_answers_round_winner(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)  # keep B connected
    round_id = f"round-{match_id}-1"
    _evict_round_with_latent_reads(monkeypatch, round_id, delays=[0, 0.01])

    _run(
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_B},
        ),
        main.give_up_round(match_id, current_user={"_id": PLAYER_A}),
    )

    assert str(main.in_memory_rounds[round_id].get("winner_id")) == PLAYER_B


def test_current_behavior_give_up_write_back_reopens_the_scored_round(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG pin: current behavior of the xfail above. B's answer scored 0-1
    # and set the winner; A's hydrated give-up erased it. The point sticks,
    # the round reopens, and B can win the SAME round a second time.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _status(client, auth_headers, match_id, PLAYER_B)
    round_id = f"round-{match_id}-1"
    _evict_round_with_latent_reads(monkeypatch, round_id, delays=[0, 0.01])

    answered, gave_up = _run(
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_B},
        ),
        main.give_up_round(match_id, current_user={"_id": PLAYER_A}),
    )

    assert answered["correct"] is True
    assert answered["round_winner"] == PLAYER_B
    assert gave_up == {"status": "gave_up", "waiting_for_opponent": True}

    survivor = main.in_memory_rounds[round_id]
    assert survivor.get("winner_id") is None  # B's recorded win erased
    assert survivor["player1_gave_up"] is True
    match = main.in_memory_matches[match_id]
    assert (match["player1_score"], match["player2_score"]) == (0, 1)

    # The reopened round pays B a second point.
    again = _answer(client, auth_headers, match_id, PLAYER_B, CORRECT)
    assert again["correct"] is True
    assert again["player2_score"] == 2
    assert len(_match_rounds(match_id)) == 1


# ===========================================================================
# 7. match_locks growth - locks are never cleaned up
# ===========================================================================


def test_every_questioned_match_grows_one_lock(
    client, auth_headers, fixed_question
):
    creators = [f"guest-lockdeep-c{i:02d}" for i in range(12)]
    joiners = [f"guest-lockdeep-j{i:02d}" for i in range(12)]
    match_ids = []
    for creator, joiner in zip(creators, joiners):
        match_id = _friend_match(client, auth_headers, creator, joiner)
        _question(client, auth_headers, match_id, creator)
        match_ids.append(match_id)

    assert set(main.match_locks) == set(match_ids)
    assert len(main.match_locks) == 12
    # One asyncio.Lock per match, all distinct objects.
    assert len({id(lock) for lock in main.match_locks.values()}) == 12


def test_completion_and_abandonment_never_release_locks(
    client, auth_headers, fixed_question
):
    # BUG/quirk (bug 22 family): the lock dict only ever grows. Completing
    # a match, abandoning another via the stale-match scan, and playing a
    # third leaves every lock exactly in place.
    finished = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)
    abandoned = _friend_match(client, auth_headers, PLAYER_C, PLAYER_D)
    _question(client, auth_headers, abandoned, PLAYER_C)
    for _ in range(3):
        _win_round(client, auth_headers, finished, PLAYER_A)
    assert main.in_memory_matches[finished]["status"] == "completed"
    lock_ids = {mid: id(lock) for mid, lock in main.match_locks.items()}

    # Backdate the abandoned match and trigger the start-scan abandonment.
    main.in_memory_matches[abandoned]["created_at"] = (
        datetime.utcnow() - timedelta(seconds=6)
    )
    assert _start(client, auth_headers, PLAYER_C)["status"] == "searching"
    assert main.in_memory_matches[abandoned]["status"] == "abandoned"

    assert {mid: id(lock) for mid, lock in main.match_locks.items()} == lock_ids
    assert finished in main.match_locks
    assert abandoned in main.match_locks


def test_lock_outlives_even_the_evicted_match_doc(
    client, auth_headers, fixed_question
):
    # BUG/quirk: deleting the match from in_memory_matches (cache eviction)
    # leaves its lock behind, and rehydrating the match reuses the very
    # same lock object.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    lock_before = main.match_locks[match_id]

    del main.in_memory_matches[match_id]
    assert match_id in main.match_locks
    assert main.get_match_lock(match_id) is lock_before


# ===========================================================================
# 8. same lock object is reused for a match_id
# ===========================================================================


def test_lock_object_identity_survives_the_whole_match_lifecycle(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    lock = main.match_locks[match_id]
    assert main.get_match_lock(match_id) is lock

    # Two full rounds, a give-up tie, status polls - the object never moves.
    _win_round(client, auth_headers, match_id, PLAYER_A)
    _question(client, auth_headers, match_id, PLAYER_B)
    _give_up(client, auth_headers, match_id, PLAYER_A)
    _give_up(client, auth_headers, match_id, PLAYER_B)
    _status(client, auth_headers, match_id, PLAYER_A)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    assert main.in_memory_matches[match_id]["status"] == "completed"

    assert main.match_locks[match_id] is lock
    assert main.get_match_lock(match_id) is lock


def test_get_question_blocks_on_the_stored_lock_object_until_released(
    client, auth_headers, fixed_question
):
    # Behavioral identity proof: manually holding the dict's lock object
    # stalls get_question; releasing it lets the round get created.
    match_id = _friend_match(client, auth_headers)
    main.match_locks.pop(match_id, None)  # fresh lock for this event loop

    async def run():
        lock = main.get_match_lock(match_id)
        await lock.acquire()
        task = asyncio.create_task(
            main.get_question(match_id, current_user={"_id": PLAYER_A})
        )
        await asyncio.sleep(0.01)
        assert not task.done()  # blocked on OUR lock object
        assert len(_match_rounds(match_id)) == 0  # nothing created yet
        lock.release()
        return await asyncio.wait_for(task, timeout=2)

    result = asyncio.run(run())
    assert result["round_id"] == f"round-{match_id}-1"
    assert len(_match_rounds(match_id)) == 1


# ===========================================================================
# 9. concurrent status polls never corrupt state
# ===========================================================================


def test_status_poll_storm_with_db_latency_leaves_state_byte_identical(
    client, auth_headers, fixed_question, monkeypatch
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    round_id = f"round-{match_id}-1"

    def _strip(match):
        clean = copy.deepcopy(match)
        clean.pop("player_last_seen", None)
        return clean

    match_snapshot = _strip(main.in_memory_matches[match_id])
    round_snapshot = copy.deepcopy(main.in_memory_rounds[round_id])

    async def slow_users_find_one(*args, **kwargs):
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(main.users_collection, "find_one", slow_users_find_one)

    results = _run(
        *[
            main.get_game_status(
                match_id, current_user={"_id": PLAYER_A if i % 2 else PLAYER_B}
            )
            for i in range(16)
        ]
    )

    assert all(not isinstance(r, Exception) for r in results)
    # Every interleaved poll served the identical open-round payload.
    for status in results:
        assert status["status"] == "active"
        assert (status["player1_score"], status["player2_score"]) == (0, 0)
        assert status["round_winner"] is None
        assert status["winner_id"] is None
    # Polls only touched the heartbeat map; everything else is untouched.
    assert _strip(main.in_memory_matches[match_id]) == match_snapshot
    assert main.in_memory_rounds[round_id] == round_snapshot
    assert set(main.in_memory_matches[match_id]["player_last_seen"]) == {
        PLAYER_A,
        PLAYER_B,
    }


def test_status_polls_during_locked_round_creation_stay_coherent(
    client, auth_headers, fixed_question, monkeypatch
):
    # Status takes no lock, so polls interleave with round creation while
    # the lock is held mid-write. They must still serve a coherent payload
    # (the new round is already the published current round) and never fork
    # extra rounds or locks.
    match_id = _friend_match(client, auth_headers)
    main.match_locks.pop(match_id, None)
    _yielding(monkeypatch, "matches_collection", "update_one")

    results = _run(
        main.get_question(match_id, current_user={"_id": PLAYER_A}),
        main.get_game_status(match_id, current_user={"_id": PLAYER_B}),
        main.get_game_status(match_id, current_user={"_id": PLAYER_A}),
    )

    question, status_b, status_a = results
    assert question["round_id"] == f"round-{match_id}-1"
    for status in (status_b, status_a):
        assert status["status"] == "active"
        assert status["round_winner"] is None
        assert (status["player1_score"], status["player2_score"]) == (0, 0)
        # The half-created round is already visible through its start time.
        assert status["round_start_time"] == question["round_start_time"]
    assert len(_match_rounds(match_id)) == 1
    assert list(main.match_locks) == [match_id]


def test_status_poll_racing_a_win_serves_a_torn_payload(
    client, auth_headers, fixed_question, monkeypatch
):
    # BUG/quirk: get_game_status reads the round info BEFORE its awaited
    # user lookups but the scores AFTER them, so a poll interleaved with a
    # winning answer reports the NEW score with the OLD round_winner.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    async def slow_users_find_one(*args, **kwargs):
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(main.users_collection, "find_one", slow_users_find_one)

    status, answer = _run(
        main.get_game_status(match_id, current_user={"_id": PLAYER_B}),
        main.submit_answer(
            main.AnswerSubmit(match_id=match_id, answer=CORRECT),
            current_user={"_id": PLAYER_A},
        ),
    )

    assert answer["round_winner"] == PLAYER_A
    # Torn read: the score already moved, the round winner hasn't.
    assert (status["player1_score"], status["player2_score"]) == (1, 0)
    assert status["round_winner"] is None
    # The very next (sequential) poll is consistent again.
    clean = _status(client, auth_headers, match_id, PLAYER_B)
    assert clean["round_winner"] == PLAYER_A
    assert (clean["player1_score"], clean["player2_score"]) == (1, 0)


# ===========================================================================
# 10. concurrent cancel + start for two users
# ===========================================================================


def test_cancel_winning_the_race_requeues_cleanly(client, auth_headers):
    # A is queued; A's cancel and B's start arrive together with the cancel
    # ordered first: A leaves, B becomes the lone searcher, no match forms.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"

    cancel_result, start_result = _run(
        main.cancel_matchmaking(current_user={"_id": PLAYER_A}),
        main.start_match(
            main.MatchStart(mode="random"),
            current_user={"_id": PLAYER_B, "elo": 1000},
        ),
    )

    assert cancel_result == {"status": "cancelled"}
    assert start_result["status"] == "searching"
    assert list(main.matchmaking_queue) == [PLAYER_B]
    assert main.in_memory_matches == {}
    assert PLAYER_A in main.cancelled_users  # the flag lingers (bug 6)


def test_cancel_losing_the_race_leaves_the_pair_matched_and_flagged(
    client, auth_headers
):
    # BUG/quirk: same tick, opposite order - B's start pairs with A a
    # moment before A's cancel lands. The cancel cannot unwind the match:
    # it only plants A's stale cancelled flag next to a live match.
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"

    start_result, cancel_result = _run(
        main.start_match(
            main.MatchStart(mode="random"),
            current_user={"_id": PLAYER_B, "elo": 1000},
        ),
        main.cancel_matchmaking(current_user={"_id": PLAYER_A}),
    )

    assert start_result["status"] == "matched"
    assert cancel_result == {"status": "cancelled"}
    match_id = start_result["match_id"]
    assert main.in_memory_matches[match_id]["status"] == "active"
    assert PLAYER_A in main.cancelled_users
    assert main.matchmaking_queue == {}

    # A's very next poll ignores the cancel and reconnects into the match.
    reconnect = _start(client, auth_headers, PLAYER_A)
    assert reconnect["status"] == "matched"
    assert reconnect["match_id"] == match_id
    # The reconnect branch never consumes the flag either.
    assert PLAYER_A in main.cancelled_users


def test_same_tick_start_and_cancel_plants_the_stale_flag_bomb(
    client, auth_headers
):
    # BUG pin (bug 6, concurrent framing): one user taps play+cancel in the
    # same tick. Net queue state is clean, but the leftover flag detonates
    # the NEXT pairing: B queues, A tries to play again, and the pairing is
    # eaten as "cancelled" - dropping B from the queue too.
    start_result, cancel_result = _run(
        main.start_match(
            main.MatchStart(mode="random"),
            current_user={"_id": PLAYER_A, "elo": 1000},
        ),
        main.cancel_matchmaking(current_user={"_id": PLAYER_A}),
    )
    assert start_result["status"] == "searching"
    assert cancel_result == {"status": "cancelled"}
    assert main.matchmaking_queue == {}
    assert PLAYER_A in main.cancelled_users

    assert _start(client, auth_headers, PLAYER_B)["status"] == "searching"
    eaten = _start(client, auth_headers, PLAYER_A)
    assert eaten["status"] == "cancelled"
    assert main.in_memory_matches == {}
    assert main.matchmaking_queue == {}  # B was silently dequeued as well
    assert PLAYER_A not in main.cancelled_users  # flag consumed by the abort


def test_both_users_cancel_right_after_pairing_and_stay_stuck_in_the_match(
    client, auth_headers
):
    # BUG/quirk: A queues, B pairs, then BOTH cancels land in the same
    # gather - too late. The match stands, both flags linger, and both
    # players' next polls reconnect them into the match they just tried to
    # leave.
    results = _run(
        main.start_match(
            main.MatchStart(mode="random"),
            current_user={"_id": PLAYER_A, "elo": 1000},
        ),
        main.start_match(
            main.MatchStart(mode="random"),
            current_user={"_id": PLAYER_B, "elo": 1000},
        ),
        main.cancel_matchmaking(current_user={"_id": PLAYER_A}),
        main.cancel_matchmaking(current_user={"_id": PLAYER_B}),
    )

    assert results[0]["status"] == "searching"
    assert results[1]["status"] == "matched"
    match_id = results[1]["match_id"]
    assert main.in_memory_matches[match_id]["status"] == "active"
    assert {PLAYER_A, PLAYER_B} <= main.cancelled_users

    for player in (PLAYER_A, PLAYER_B):
        body = _start(client, auth_headers, player)
        assert body["status"] == "matched"
        assert body["match_id"] == match_id
    # Both stale flags survive the reconnects, primed to eat future pairings.
    assert {PLAYER_A, PLAYER_B} <= main.cancelled_users
