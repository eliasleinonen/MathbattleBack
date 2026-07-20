"""
Audit pass 2: newly found under-tested bugs in the people-match code.

A second full read of main.py's match code (start_match, submit_answer,
get_question, the inline SymPy grading cascade, give_up_round, presence,
queue lifecycle) diffed against the seventeen existing edge-case suites and
the MATCH_EDGE_CASE_REPORT.md numbered list (bugs 1-37).  Every bug below
was untested before this file; each carries a strict xfail for the desired
behavior plus sibling tests pinning the CURRENT behavior.

Bugs pinned here (report numbering continues at 38):

38. submit_answer never enforces the 5-minute PvP round expiry.  get_question
    ties any round older than 300s, and the bot path forfeits at time_limit
    inside submit_answer -- but the people-vs-people answer path has NO
    expiry check at all.  A correct answer submitted hours after the round
    should have been voided still wins the round, and at match point it
    completes the match and moves real ELO on a round the very next
    get_question would have tied.

39. The numeric grading fallback accepts mathematically WRONG answers that
    sit within 1e-6 of the correct one.  After every symbolic check fails,
    submit_answer samples 5 random points in (1, 10) and compares with an
    ABSOLUTE 1e-6 tolerance; "2*x + 1e-7" and "2*x*(1+1e-9)" are
    deterministically accepted for "2·x" because the difference is below
    the tolerance at every possible sample point.

40. The same numeric fallback samples only x in (1, 10), so answers that
    agree with the derivative ONLY on the positive axis are accepted:
    "2*abs(x)" and "2*sqrt(x^2)" (both equal 2|x|, wrong for every x < 0)
    grade as correct for "2·x".

41. The <5s reconnect branch of /api/game/start never dequeues the caller.
    A player who queues for ranked and then gets a fresh active match by
    another route (friend join -- the bug-7 hijack window) is handed the
    reconnect while their matchmaking_queue entry stays live.  The next
    ranked searcher is then deterministically paired against them into a
    ghost match the reconnected player never learns about (their /active
    points at the earlier match), leaving the newcomer stranded.

Conventions match the sibling edge-case files: guest identities via
"Bearer guest-xxx" tokens, fixed_question for deterministic grading, strict
xfail markers for the desired behavior with current-behavior companion
pins.  See MATCH_EDGE_CASE_REPORT.md ("Audit pass 2").
"""

from datetime import timedelta

import pytest

import main


PLAYER_A = "guest-ap2-aaa"
PLAYER_B = "guest-ap2-bbb"
PLAYER_C = "guest-ap2-ccc"

CORRECT = "2*x"  # matches fixed_question's stored answer "2·x"


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _start(client, auth_headers, player):
    response = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _friend_match(client, auth_headers, p1=PLAYER_A, p2=PLAYER_B):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(p1)
    )
    assert created.status_code == 200, created.text
    body = created.json()
    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": body["match_code"]},
        headers=auth_headers(p2),
    )
    assert joined.status_code == 200, joined.text
    return body["match_id"]


def _ranked_match(client, auth_headers, player1=PLAYER_A, player2=PLAYER_B):
    """Queue player2 first so the joining player1 lands in the player1 slot."""
    assert _start(client, auth_headers, player2)["status"] == "searching"
    body = _start(client, auth_headers, player1)
    assert body["status"] == "matched", body
    return body["match_id"]


def _question(client, auth_headers, match_id, player):
    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _answer(client, auth_headers, match_id, player, answer):
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _expire_round(round_id, seconds=301):
    """Backdate a round past get_question's 300s tie threshold."""
    main.in_memory_rounds[round_id]["created_at"] = main.utc_now() - timedelta(
        seconds=seconds
    )


@pytest.fixture
def elo_writes(client, monkeypatch):
    """Record every users_collection.update_one $inc payload."""
    calls = []

    async def update_one(query, update, *args, **kwargs):
        if "$inc" in update:
            calls.append((query, update["$inc"]))

        class _Result:
            modified_count = 1
            matched_count = 1
            upserted_id = None

        return _Result()

    monkeypatch.setattr(main.users_collection, "update_one", update_one)
    return calls


# ===========================================================================
# Bug 38: submit_answer has no PvP round-expiry check
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(answer-ignores-round-expiry): get_question voids any round older "
        "than 300s as a tie, and the bot path forfeits at time_limit inside "
        "submit_answer, but the PvP answer path never checks the round's age. "
        "A correct answer submitted arbitrarily late still wins the round "
        "that the very next get_question would have tied. submit_answer "
        "should apply the same 300s expiry before grading."
    ),
)
def test_answer_on_expired_round_should_not_score(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    served = _question(client, auth_headers, match_id, PLAYER_A)
    _expire_round(served["round_id"])  # 301s: one past get_question's cutoff

    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert body["player1_score"] == 0, "expired round must not award a point"
    assert body["round_winner"] != PLAYER_A


def test_current_behavior_hours_old_round_answer_still_wins(
    client, auth_headers, fixed_question
):
    # CURRENT BEHAVIOR pin for bug 38: a round two hours past its expiry is
    # still fully scorable; the late answer takes the point exactly as if it
    # had been submitted in time.
    match_id = _friend_match(client, auth_headers)
    served = _question(client, auth_headers, match_id, PLAYER_A)
    _expire_round(served["round_id"], seconds=7200)

    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER_A
    assert body["player1_score"] == 1
    assert main.in_memory_rounds[served["round_id"]]["winner_id"] == PLAYER_A


def test_current_behavior_expired_round_completes_ranked_match_and_pays_elo(
    client, auth_headers, fixed_question, elo_writes
):
    # CURRENT BEHAVIOR pin for bug 38 at its most damaging: at match point,
    # the hours-late answer completes the ranked match and moves real ELO on
    # a round get_question would have voided.
    match_id = _ranked_match(client, auth_headers)
    for _ in range(2):
        _question(client, auth_headers, match_id, PLAYER_A)
        _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    served = _question(client, auth_headers, match_id, PLAYER_A)
    _expire_round(served["round_id"], seconds=7200)
    assert elo_writes == []  # nothing paid before the deciding round

    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert body["match_winner"] == PLAYER_A
    assert body["elo_change"] == 20  # 1000 vs 1000 snapshots
    assert main.in_memory_matches[match_id]["status"] == "completed"
    # Winner +elo/+wins and loser -elo/+losses were both applied.
    incs = [inc for _query, inc in elo_writes]
    assert {"elo": 20, "wins": 1} in incs
    assert {"elo": -20, "losses": 1} in incs


def test_current_behavior_question_would_have_tied_the_same_round(
    client, auth_headers, fixed_question
):
    # Sibling contrast for bug 38: the ONLY thing deciding between "point"
    # and "void" for an expired round is which endpoint touches it first.
    # Same setup as above, but the opponent polls /question before the late
    # answer lands: the round ties and the late answer bounces off
    # already_won with no score.
    match_id = _friend_match(client, auth_headers)
    served = _question(client, auth_headers, match_id, PLAYER_A)
    _expire_round(served["round_id"])

    fresh = _question(client, auth_headers, match_id, PLAYER_B)
    assert fresh["round_id"] != served["round_id"]
    assert main.in_memory_rounds[served["round_id"]]["winner_id"] == "tie"

    # The expired round is voided for good: the late answer lands on the NEW
    # round (scoring round 2 -- fixed_question repeats the same answer), and
    # the tied round's outcome is untouched.
    late = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert late["round_winner"] == PLAYER_A  # round 2, not the expired one
    assert late["player1_score"] == 1
    assert main.in_memory_rounds[served["round_id"]]["winner_id"] == "tie"
    assert main.in_memory_rounds[fresh["round_id"]]["winner_id"] == PLAYER_A


# ===========================================================================
# Bug 39: numeric fallback's absolute 1e-6 tolerance accepts wrong answers
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(numeric-fallback-epsilon): after every symbolic check fails, "
        "submit_answer's numeric fallback samples 5 points in (1, 10) with "
        "an absolute 1e-6 tolerance and flips correct=True. '2*x + 1e-7' is "
        "NOT the derivative of x^2, and simplify()/equals() both correctly "
        "reject it, yet the fallback overrides them and accepts it (the "
        "difference is 1e-7 < 1e-6 at every sample point, so this is "
        "deterministic). A symbolically-refuted answer should stay wrong."
    ),
)
def test_answer_off_by_a_tiny_constant_should_be_rejected(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, "2*x + 1e-7")
    assert body["correct"] is False
    assert body["player1_score"] == 0


@pytest.mark.parametrize(
    "near_miss",
    [
        "2*x + 1e-7",  # constant offset below the 1e-6 tolerance
        "2*x + 0.0000001",  # same, spelled out
        "2*x*(1+1e-9)",  # relative error: diff <= 2e-8 over (1, 10)
    ],
)
def test_current_behavior_within_tolerance_wrong_answers_win_rounds(
    client, auth_headers, fixed_question, near_miss
):
    # CURRENT BEHAVIOR pin for bug 39: these mathematically wrong answers
    # grade correct and take the round.  Deterministic: their difference
    # from 2*x is below 1e-6 for EVERY x the fallback can sample.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, near_miss)
    assert body["correct"] is True, near_miss
    assert body["round_winner"] == PLAYER_A


def test_current_behavior_just_above_tolerance_is_still_rejected(
    client, auth_headers, fixed_question
):
    # The cliff sits exactly at the fallback's 1e-6: an offset of 2e-6
    # exceeds it at every sample point and is rejected.  Together with the
    # pin above this brackets the tolerance from both sides.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, "2*x + 2e-6")
    assert body["correct"] is False
    assert body["player1_score"] == 0


# ===========================================================================
# Bug 40: numeric fallback samples only x in (1, 10) -- positive-axis-only
# "derivatives" are accepted
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(numeric-fallback-positive-domain): the numeric fallback's "
        "sample points all come from uniform(1, 10), so an answer that "
        "matches the derivative only for x > 0 is indistinguishable from a "
        "correct one. '2*abs(x)' equals 2|x|, which is WRONG for every "
        "x < 0 (simplify and equals both refuse to confirm it), yet the "
        "fallback accepts it deterministically because every sample is "
        "positive. Sampling should cover both signs (or the fallback should "
        "not override a symbolic refusal)."
    ),
)
def test_abs_answer_should_be_rejected_for_polynomial_derivative(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, "2*abs(x)")
    assert body["correct"] is False
    assert body["player1_score"] == 0


@pytest.mark.parametrize("positive_only", ["2*abs(x)", "2*sqrt(x^2)"])
def test_current_behavior_positive_axis_lookalikes_are_accepted(
    client, auth_headers, fixed_question, positive_only
):
    # CURRENT BEHAVIOR pin for bug 40: both spellings of 2|x| grade correct
    # for the answer "2·x" and win the round.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, positive_only)
    assert body["correct"] is True, positive_only
    assert body["round_winner"] == PLAYER_A


def test_current_behavior_sign_flip_is_still_rejected(
    client, auth_headers, fixed_question
):
    # Contrast pin: an answer wrong ON the sampled interval ('-2*x') is
    # caught by the very same fallback, proving the hole is specifically
    # about x < 0 never being sampled.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, "-2*x")
    assert body["correct"] is False


# ===========================================================================
# Bug 41: the /start reconnect branch never dequeues the caller
# ===========================================================================


def _queue_then_get_fresh_friend_match(client, auth_headers):
    """PLAYER_A queues for ranked, then a friend match of theirs goes active."""
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    assert PLAYER_A in main.matchmaking_queue

    friend_match_id = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)

    # A's ranked poll now hits the <5s reconnect branch of the scan and is
    # handed the freshly-activated FRIEND match (the bug-7 hijack window).
    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == friend_match_id
    return friend_match_id


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(reconnect-leaves-queue-entry): the <5s reconnect branch of "
        "start_match returns 'matched' without popping the caller's "
        "matchmaking_queue entry (only the pairing branch pops). A player "
        "who queued and then got an active match by another route keeps a "
        "live queue entry while playing, and the next ranked searcher is "
        "deterministically paired against them into a ghost match they "
        "never learn about. The reconnect return should dequeue the caller."
    ),
)
def test_reconnect_should_remove_the_callers_queue_entry(
    client, auth_headers, fixed_question
):
    _queue_then_get_fresh_friend_match(client, auth_headers)
    assert PLAYER_A not in main.matchmaking_queue


def test_current_behavior_leftover_queue_entry_pairs_a_ghost_match(
    client, auth_headers, fixed_question
):
    # CURRENT BEHAVIOR pin for bug 41, end to end: A's stale entry survives
    # the reconnect, C's first search is told "matched" against A, and A --
    # busy in the friend match -- can never even discover the ghost.
    friend_match_id = _queue_then_get_fresh_friend_match(client, auth_headers)
    assert PLAYER_A in main.matchmaking_queue  # the leftover entry

    third = _start(client, auth_headers, PLAYER_C)
    assert third["status"] == "matched"
    ghost_id = third["match_id"]
    assert ghost_id != friend_match_id

    ghost = main.in_memory_matches[ghost_id]
    assert ghost["match_type"] == "ranked"
    assert {str(ghost["player1_id"]), str(ghost["player2_id"])} == {
        PLAYER_C,
        PLAYER_A,
    }
    assert main.matchmaking_queue == {}  # the entry was consumed by pairing

    # A is now split across TWO simultaneously active matches...
    a_active = [
        mid
        for mid, m in main.in_memory_matches.items()
        if m["status"] == "active"
        and PLAYER_A in (str(m["player1_id"]), str(m["player2_id"]))
    ]
    assert sorted(a_active) == sorted([friend_match_id, ghost_id])

    # ...but /api/game/active surfaces only the earlier friend match
    # (insertion-order scan), so A has no way to find the ghost while C
    # waits in it against an absent opponent.
    active = client.get("/api/game/active", headers=auth_headers(PLAYER_A))
    assert active.json()["match_id"] == friend_match_id


def test_current_behavior_ghost_match_is_playable_solo_by_the_newcomer(
    client, auth_headers, fixed_question
):
    # Follow-through pin for bug 41: nothing stops the stranded newcomer
    # from playing the ghost to 3-0 against the absent opponent, completing
    # a "ranked" match the other participant never saw.
    _queue_then_get_fresh_friend_match(client, auth_headers)
    ghost_id = _start(client, auth_headers, PLAYER_C)["match_id"]

    for _ in range(3):
        _question(client, auth_headers, ghost_id, PLAYER_C)
        final = _answer(client, auth_headers, ghost_id, PLAYER_C, CORRECT)

    assert final["match_winner"] == PLAYER_C
    assert main.in_memory_matches[ghost_id]["status"] == "completed"
