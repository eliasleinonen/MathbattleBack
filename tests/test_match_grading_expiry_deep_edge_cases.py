"""
Audit pass 3: deeper coverage around the newly found grading/expiry bugs.

This file drills into the four bugs first pinned in
test_match_audit_pass_2_edge_cases.py (report numbers 38-41) plus the
adjacent grading holes they exposed.  It does not re-pin the pass-2 cases;
it widens each one along an axis the pass-2 file left open:

* Bug 38 (PvP answer path ignores the 5-minute round expiry): the pass-2
  file pinned 301s and 7200s.  Here the "still accepted" behavior is
  parametrized across 301s / 600s / 3600s, the exact 300s boundary is
  bracketed against get_question's strict ">300" cutoff, and the
  match-point ELO interaction gets its own strict xfail (a stale round
  should not be allowed to complete a ranked match and move ELO).

* Bug 39 (numeric fallback's absolute 1e-6 tolerance): the pass-2 file
  pinned three near-misses and one just-above-tolerance rejection.  Here
  the 1e-6 cliff is swept with many more samples, from both constant and
  relative offsets, bracketing it tightly from both sides.  A separate
  section covers the "symbolic reject then numeric accept" pattern for
  polynomial near-misses that simplify() explicitly refutes.

* Bug 40 (numeric fallback samples only x in (1, 10)): the pass-2 file
  pinned 2*abs(x) and 2*sqrt(x^2).  Here the family of positive-axis
  lookalikes is broadened well beyond abs/sqrt -- Abs(2*x), sqrt(4*x^2),
  x + Abs(x), and 2*Max(x, -x) all agree with 2*x for x > 0 and are
  wrong for some x < 0, yet all grade correct.

* Bug 41 (reconnect branch never dequeues the caller): the pass-2 file
  pinned the ghost-pairing and solo-completion.  Here the full exploit
  path is followed to the damaging end -- the stranded newcomer solo-plays
  the ghost to a completed ranked match that pays REAL ELO against the
  absent player, who is charged a loss on a match they never saw.  A
  distinct reconnect-dequeue xfail asserts the post-fix invariant (the
  next searcher should stay "searching", not be ghost-paired).

Conventions follow the sibling edge-case suites: guest identities via
"Bearer guest-xxx" tokens, fixed_question for deterministic grading,
strict xfail markers for the desired behavior with current-behavior
companion pins.  See MATCH_EDGE_CASE_REPORT.md ("Audit pass 3").
"""

from datetime import timedelta

import pytest

import main


PLAYER_A = "guest-ap3-aaa"
PLAYER_B = "guest-ap3-bbb"
PLAYER_C = "guest-ap3-ccc"

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


def _backdate_round(round_id, seconds):
    """Backdate a round's created_at by `seconds` so age math sees it as old."""
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
# Bug 38 (deepened): PvP answer path never enforces the 5-minute round expiry
# ===========================================================================


@pytest.mark.parametrize("age", [301, 600, 3600])
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(answer-ignores-round-expiry): get_question ties any round older "
        "than 300s, but submit_answer's PvP path has no age check, so a "
        "correct answer submitted 301s / 600s / 3600s late still wins the "
        "round the very next get_question would have voided. submit_answer "
        "should apply the same 300s cutoff and void the stale round."
    ),
)
def test_pvp_answer_after_expiry_should_void(
    client, auth_headers, fixed_question, age
):
    match_id = _friend_match(client, auth_headers)
    served = _question(client, auth_headers, match_id, PLAYER_A)
    _backdate_round(served["round_id"], age)

    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert body["player1_score"] == 0, f"expired round ({age}s) must not score"
    assert body["round_winner"] != PLAYER_A


@pytest.mark.parametrize("age", [301, 600, 3600])
def test_current_behavior_pvp_answer_after_expiry_still_wins(
    client, auth_headers, fixed_question, age
):
    # CURRENT BEHAVIOR pin: at every one of these ages -- 5min+1s, 10min,
    # 1h -- the late answer takes the point exactly as if it were on time.
    match_id = _friend_match(client, auth_headers)
    served = _question(client, auth_headers, match_id, PLAYER_A)
    _backdate_round(served["round_id"], age)

    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert body["correct"] is True, age
    assert body["round_winner"] == PLAYER_A
    assert body["player1_score"] == 1
    assert main.in_memory_rounds[served["round_id"]]["winner_id"] == PLAYER_A


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(answer-ignores-round-expiry) at the boundary: get_question uses "
        "a strict '>300s' cutoff, so a round just past 300s is voided by the "
        "next question poll but is still fully scorable via submit_answer. "
        "The two endpoints should agree on the same round-expiry boundary."
    ),
)
def test_boundary_answer_and_question_should_agree_just_past_300s(
    client, auth_headers, fixed_question
):
    match_id = _friend_match(client, auth_headers)
    served = _question(client, auth_headers, match_id, PLAYER_A)
    _backdate_round(served["round_id"], 301)  # just past get_question's cutoff

    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    # get_question would tie this exact round; the answer path should too.
    assert body["round_winner"] != PLAYER_A
    assert body["player1_score"] == 0


def test_current_behavior_boundary_question_voids_but_answer_scores(
    client, auth_headers, fixed_question
):
    # CURRENT BEHAVIOR pin bracketing the ~300s boundary from both sides:
    #   * A round aged just UNDER the cutoff is still live: get_question
    #     re-serves the SAME round rather than voiding it.
    #   * A round aged just OVER the cutoff is voided by get_question (tie),
    #     yet an answer submitted to an equally-aged sibling round still
    #     scores -- the two endpoints disagree at the boundary.
    match_id = _friend_match(client, auth_headers)

    served = _question(client, auth_headers, match_id, PLAYER_A)
    _backdate_round(served["round_id"], 299)  # just under: still live
    again = _question(client, auth_headers, match_id, PLAYER_A)
    assert again["round_id"] == served["round_id"]
    assert main.in_memory_rounds[served["round_id"]].get("winner_id") in (None,)

    _backdate_round(served["round_id"], 301)  # just over: question voids it
    voided = _question(client, auth_headers, match_id, PLAYER_B)
    assert voided["round_id"] != served["round_id"]
    assert main.in_memory_rounds[served["round_id"]]["winner_id"] == "tie"

    # The freshly created round is equally answerable; backdate it past the
    # cutoff and the answer path still scores it, unlike get_question.
    _backdate_round(voided["round_id"], 301)
    late = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert late["round_winner"] == PLAYER_A
    assert late["player1_score"] == 1


# ===========================================================================
# Bug 38 x ELO interaction: a stale round should not pay ELO at match point
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(answer-ignores-round-expiry) x ELO: at match point a correct "
        "answer on an expired round completes the ranked match and moves "
        "real ELO. Because the round is past the 300s cutoff get_question "
        "would have tied, no ELO should change hands; a voided deciding "
        "round must not settle a ranked match."
    ),
)
def test_late_answer_at_match_point_should_not_pay_elo(
    client, auth_headers, fixed_question, elo_writes
):
    match_id = _ranked_match(client, auth_headers)
    for _ in range(2):
        _question(client, auth_headers, match_id, PLAYER_A)
        _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    served = _question(client, auth_headers, match_id, PLAYER_A)
    _backdate_round(served["round_id"], 3600)
    assert elo_writes == []

    _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert elo_writes == [], "an expired deciding round must not pay ELO"


def test_current_behavior_late_answer_at_match_point_pays_elo(
    client, auth_headers, fixed_question, elo_writes
):
    # CURRENT BEHAVIOR pin: the stale deciding round completes the match and
    # pays a full +/-20 ELO swing on both accounts.
    match_id = _ranked_match(client, auth_headers)
    for _ in range(2):
        _question(client, auth_headers, match_id, PLAYER_A)
        _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    served = _question(client, auth_headers, match_id, PLAYER_A)
    _backdate_round(served["round_id"], 3600)
    assert elo_writes == []

    body = _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    assert body["match_winner"] == PLAYER_A
    assert body["elo_change"] == 20
    assert main.in_memory_matches[match_id]["status"] == "completed"
    incs = [inc for _query, inc in elo_writes]
    assert {"elo": 20, "wins": 1} in incs
    assert {"elo": -20, "losses": 1} in incs


# ===========================================================================
# Bug 39 (deepened): sweep the numeric fallback's absolute 1e-6 cliff
# ===========================================================================


# Constant offsets: their difference from 2*x is the same at every possible
# sample point, so acceptance is deterministic and depends only on whether
# the offset is below the fallback's 1e-6 tolerance.
_BELOW_TOLERANCE = ["2*x + 1e-7", "2*x + 5e-7", "2*x + 9e-7", "2*x + 9.9e-7"]
_ABOVE_TOLERANCE = ["2*x + 1.1e-6", "2*x + 2e-6", "2*x + 1e-5", "2*x + 1e-3"]


@pytest.mark.parametrize("near_miss", _BELOW_TOLERANCE)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(numeric-fallback-epsilon): a constant offset below 1e-6 makes "
        "the answer mathematically wrong, and simplify()/equals() both "
        "reject it, yet the numeric fallback (absolute 1e-6 tolerance) "
        "overrides them and accepts it. A symbolically-refuted answer should "
        "stay wrong regardless of how tiny the offset is."
    ),
)
def test_sub_tolerance_offsets_should_be_rejected(
    client, auth_headers, fixed_question, near_miss
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, near_miss)
    assert body["correct"] is False, near_miss
    assert body["player1_score"] == 0


@pytest.mark.parametrize("near_miss", _BELOW_TOLERANCE)
def test_current_behavior_sub_tolerance_offsets_win(
    client, auth_headers, fixed_question, near_miss
):
    # CURRENT BEHAVIOR pin (below the cliff): every offset < 1e-6 grades
    # correct and takes the round.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, near_miss)
    assert body["correct"] is True, near_miss
    assert body["round_winner"] == PLAYER_A


@pytest.mark.parametrize("clear_miss", _ABOVE_TOLERANCE)
def test_current_behavior_above_tolerance_offsets_rejected(
    client, auth_headers, fixed_question, clear_miss
):
    # CURRENT BEHAVIOR pin (above the cliff): every offset > 1e-6 exceeds the
    # tolerance at every sample point and is correctly rejected.  Together
    # with the pin above this brackets the 1e-6 cliff tightly from both sides.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, clear_miss)
    assert body["correct"] is False, clear_miss
    assert body["player1_score"] == 0


# ===========================================================================
# Bug 39 (deepened): symbolic reject then numeric accept for polynomials
# ===========================================================================


# Relative near-misses: the difference from 2*x scales with x but stays below
# 1e-6 across the whole (1, 10) sample window, so simplify() refutes them
# (sym_eq is False) while the numeric fallback accepts them deterministically.
_POLY_NEAR_MISSES = ["(2+1e-8)*x", "2*x + 1e-8*x", "2.0000001*x", "(2+1e-9)*x"]


@pytest.mark.parametrize("poly", _POLY_NEAR_MISSES)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(numeric-fallback-epsilon) for polynomials: these forms are not "
        "the derivative of x^2 -- simplify(user - 2*x) != 0 -- but the "
        "difference is a tiny multiple of x that stays under 1e-6 across the "
        "(1, 10) sample window, so the numeric fallback overrides the "
        "symbolic refutation and accepts them. They should be rejected."
    ),
)
def test_polynomial_near_misses_should_be_rejected(
    client, auth_headers, fixed_question, poly
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, poly)
    assert body["correct"] is False, poly
    assert body["player1_score"] == 0


@pytest.mark.parametrize("poly", _POLY_NEAR_MISSES)
def test_current_behavior_polynomial_near_misses_win(
    client, auth_headers, fixed_question, poly
):
    # CURRENT BEHAVIOR pin: each symbolically-distinct polynomial grades
    # correct via the numeric fallback and wins the round.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, poly)
    assert body["correct"] is True, poly
    assert body["round_winner"] == PLAYER_A


# ===========================================================================
# Bug 40 (deepened): positive-axis lookalikes beyond abs / sqrt
# ===========================================================================


# Each of these equals 2*x for x > 0 (so it survives the positive-only
# sampling) but differs from 2*x for some x < 0, hence is not the derivative
# of x^2.  x + Abs(x) is 2*x for x > 0 and 0 for x < 0; the others are 2|x|.
_POSITIVE_LOOKALIKES = ["Abs(2*x)", "sqrt(4*x^2)", "x + Abs(x)", "2*Max(x, -x)"]


@pytest.mark.parametrize("lookalike", _POSITIVE_LOOKALIKES)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(numeric-fallback-positive-domain): the numeric fallback only "
        "samples x in (1, 10), so any expression that agrees with 2*x on the "
        "positive axis is accepted even when it is wrong for x < 0. These "
        "forms (2|x| spellings and x+|x|) are not the derivative of x^2 and "
        "simplify()/equals() refuse them, but they grade correct. Sampling "
        "should cover both signs (or not override a symbolic refutation)."
    ),
)
def test_positive_axis_lookalikes_should_be_rejected(
    client, auth_headers, fixed_question, lookalike
):
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, lookalike)
    assert body["correct"] is False, lookalike
    assert body["player1_score"] == 0


@pytest.mark.parametrize("lookalike", _POSITIVE_LOOKALIKES)
def test_current_behavior_positive_axis_lookalikes_win(
    client, auth_headers, fixed_question, lookalike
):
    # CURRENT BEHAVIOR pin: every positive-axis lookalike grades correct for
    # "2·x" and wins the round.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, lookalike)
    assert body["correct"] is True, lookalike
    assert body["round_winner"] == PLAYER_A


def test_current_behavior_negative_only_disagreement_still_rejected(
    client, auth_headers, fixed_question
):
    # Contrast pin: an expression wrong ON the sampled interval ('-2*x') is
    # caught by the very same fallback, isolating the hole to x < 0 never
    # being sampled rather than the fallback being toothless.
    match_id = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    body = _answer(client, auth_headers, match_id, PLAYER_A, "-2*x")
    assert body["correct"] is False


# ===========================================================================
# Bug 41 (deepened): full ghost-match exploit ends in a real ELO transfer
# ===========================================================================


def _queue_then_get_fresh_friend_match(client, auth_headers):
    """PLAYER_A queues for ranked, then a friend match of theirs goes active."""
    assert _start(client, auth_headers, PLAYER_A)["status"] == "searching"
    assert PLAYER_A in main.matchmaking_queue

    friend_match_id = _friend_match(client, auth_headers, PLAYER_A, PLAYER_B)

    # A's ranked poll now hits the <5s reconnect branch and is handed the
    # freshly-activated FRIEND match (the bug-7 hijack window) WITHOUT its
    # matchmaking_queue entry being popped.
    body = _start(client, auth_headers, PLAYER_A)
    assert body["status"] == "matched"
    assert body["match_id"] == friend_match_id
    return friend_match_id


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(reconnect-leaves-queue-entry): the reconnect branch returns "
        "'matched' without popping the caller's queue entry, so the next "
        "ranked searcher is deterministically paired against the busy caller "
        "into a ghost match. After the fix the leftover entry would be gone "
        "and the next searcher should simply keep searching."
    ),
)
def test_next_searcher_should_stay_searching_not_ghost_paired(
    client, auth_headers, fixed_question
):
    _queue_then_get_fresh_friend_match(client, auth_headers)
    third = _start(client, auth_headers, PLAYER_C)
    assert third["status"] == "searching"


def test_current_behavior_ghost_completion_pays_real_elo_to_absent_player(
    client, auth_headers, fixed_question, elo_writes
):
    # CURRENT BEHAVIOR pin, the full exploit end to end: the leftover queue
    # entry pairs C into a ranked ghost against the busy A, C solo-plays it to
    # 3-0, and the match completes paying a real +/-20 ELO swing -- charging
    # A a ranked LOSS on a match A was never even shown by /api/game/active.
    friend_match_id = _queue_then_get_fresh_friend_match(client, auth_headers)
    assert PLAYER_A in main.matchmaking_queue  # the leftover entry

    ghost = _start(client, auth_headers, PLAYER_C)
    assert ghost["status"] == "matched"
    ghost_id = ghost["match_id"]
    assert ghost_id != friend_match_id

    ghost_doc = main.in_memory_matches[ghost_id]
    assert ghost_doc["match_type"] == "ranked"
    assert {str(ghost_doc["player1_id"]), str(ghost_doc["player2_id"])} == {
        PLAYER_C,
        PLAYER_A,
    }

    # A cannot even find the ghost: /api/game/active surfaces only the earlier
    # friend match (insertion-order scan).
    active = client.get("/api/game/active", headers=auth_headers(PLAYER_A))
    assert active.json()["match_id"] == friend_match_id

    # C plays the ghost solo to completion.
    for _ in range(3):
        _question(client, auth_headers, ghost_id, PLAYER_C)
        final = _answer(client, auth_headers, ghost_id, PLAYER_C, CORRECT)

    assert final["match_winner"] == PLAYER_C
    assert final["elo_change"] == 20
    assert main.in_memory_matches[ghost_id]["status"] == "completed"

    # The damaging part: real ELO moved, and the absent A was charged a loss.
    incs = [inc for _query, inc in elo_writes]
    assert {"elo": 20, "wins": 1} in incs  # C, who played alone
    assert {"elo": -20, "losses": 1} in incs  # A, who never saw the match
    charged = [q for q, inc in elo_writes if inc == {"elo": -20, "losses": 1}]
    assert any(str(q.get("_id")) == PLAYER_A for q in charged)
