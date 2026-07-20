"""
Edge-case tests for ELO math and match completion in people-vs-people matches
(friend + ranked).

Covers:
- calculate_elo_change unit behavior: even matchups, upsets, favorites,
  extreme rating gaps, zero/negative/huge inputs, the minimum-change floor,
  and the dynamic K-factor boundaries (1200 / 1800)
- match completion via /api/game/answer: first-to-3 rule, elo_change on the
  match document and in the /api/game/status + /match/{id}/details payloads
- ranked completion updates users_collection ($inc elo/wins/losses) exactly
  once, using the ELO *snapshots* stored on the match rather than the live
  user documents
- friend matches never touch ELO or win/loss counters
- ELO never applied mid-match, no double application after completion
- negative user ELO is reachable (documented via xfail + pinning test)

Conventions (same as the sibling edge-case files):
- Guest identities via "Bearer guest-xxx" tokens; a fake users collection
  records every update so the ELO writes can be asserted precisely.
- Known bugs are documented with strict xfail markers plus sibling tests that
  pin the CURRENT behavior.  See MATCH_EDGE_CASE_REPORT.md.
"""

import copy

import pytest

import main
from main import calculate_elo_change


PLAYER_A = "guest-elo-aaa"
PLAYER_B = "guest-elo-bbb"
OUTSIDER = "guest-elo-zzz"


# ---------------------------------------------------------------------------
# fake users collection (records + applies $inc/$set)
# ---------------------------------------------------------------------------


class FakeUpdateResult:
    modified_count = 1
    matched_count = 1
    upserted_id = None


class FakeUsersCollection:
    """In-process users collection that applies $inc/$set and logs calls."""

    def __init__(self):
        self.docs = {}
        self.update_calls = []

    def add_user(self, user_id, elo=1000, wins=0, losses=0, **extra):
        doc = {"_id": user_id, "elo": elo, "wins": wins, "losses": losses}
        doc.update(extra)
        self.docs[str(user_id)] = doc
        return doc

    async def find_one(self, query, *args, **kwargs):
        user_id = query.get("_id")
        if user_id is not None:
            doc = self.docs.get(str(user_id))
            return copy.deepcopy(doc) if doc else None
        username = query.get("username")
        if isinstance(username, str):
            for doc in self.docs.values():
                if doc.get("username") == username:
                    return copy.deepcopy(doc)
        return None

    async def update_one(self, filt, update, upsert=False):
        self.update_calls.append(
            {"filter": copy.deepcopy(filt), "update": copy.deepcopy(update)}
        )
        doc = self.docs.get(str(filt.get("_id")))
        if doc is not None:
            for key, delta in update.get("$inc", {}).items():
                doc[key] = doc.get(key, 0) + delta
            for key, value in update.get("$set", {}).items():
                doc[key] = value
        return FakeUpdateResult()

    def inc_calls(self):
        return [c for c in self.update_calls if "$inc" in c["update"]]


@pytest.fixture
def user_store(mock_mongo, monkeypatch):
    store = FakeUsersCollection()
    monkeypatch.setattr(main.users_collection, "find_one", store.find_one)
    monkeypatch.setattr(main.users_collection, "update_one", store.update_one)
    return store


# ---------------------------------------------------------------------------
# gameplay helpers
# ---------------------------------------------------------------------------


def _start(client, auth_headers, player):
    response = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _ranked_match(client, auth_headers, player1=PLAYER_A, player2=PLAYER_B):
    """
    Create a ranked match with `player1` in the player1 slot.

    start_match builds the match doc from the *joining* caller's perspective
    (the joiner becomes player1), so queue player2 first and join as player1.
    """
    assert _start(client, auth_headers, player2)["status"] == "searching"
    body = _start(client, auth_headers, player1)
    assert body["status"] == "matched", body
    return body["match_id"]


def _friend_match(client, auth_headers, player1=PLAYER_A, player2=PLAYER_B):
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(player1)
    )
    assert created.status_code == 200, created.text
    body = created.json()
    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": body["match_code"]},
        headers=auth_headers(player2),
    )
    assert joined.status_code == 200, joined.text
    return body["match_id"]


def _win_round(client, auth_headers, match_id, player):
    q = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert q.status_code == 200, q.text
    r = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(player),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["correct"] is True, body
    return body


def _set_score(match_id, player, score):
    match = main.in_memory_matches[match_id]
    key = "player1_score" if str(match["player1_id"]) == str(player) else "player2_score"
    match[key] = score


def _status(client, auth_headers, match_id, player):
    response = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# calculate_elo_change: even matchups and K-factor boundaries
# ---------------------------------------------------------------------------


def test_even_matchup_beginner_bracket():
    # Equal ELO -> expected 0.5 -> change = K/2. Winner < 1200 -> K = 40.
    assert calculate_elo_change(1000, 1000) == 20


def test_even_matchup_intermediate_bracket():
    assert calculate_elo_change(1500, 1500) == 16  # K = 32


def test_even_matchup_advanced_bracket():
    assert calculate_elo_change(2000, 2000) == 12  # K = 24


def test_k_factor_boundary_just_below_1200():
    assert calculate_elo_change(1199, 1199) == 20  # still K = 40


def test_k_factor_boundary_at_1200():
    assert calculate_elo_change(1200, 1200) == 16  # drops to K = 32


def test_k_factor_boundary_just_below_1800():
    assert calculate_elo_change(1799, 1799) == 16  # still K = 32


def test_k_factor_boundary_at_1800():
    assert calculate_elo_change(1800, 1800) == 12  # drops to K = 24


def test_k_factor_depends_only_on_winner_elo():
    # Quirk: the K bracket is chosen from the WINNER's rating only, so a
    # low-rated winner moves an elite loser by up to 40 points even though
    # the loser is in the "stable" K=24 bracket.
    assert calculate_elo_change(1000, 1900) == 40
    # ...while the reverse pairing uses the elite winner's K=24.
    assert calculate_elo_change(1900, 1000) == 1


# ---------------------------------------------------------------------------
# calculate_elo_change: upsets, favorites, extreme gaps
# ---------------------------------------------------------------------------


def test_upset_beats_400_points_higher():
    # expected = 1/(1 + 10^1) = 1/11; change = round(40 * 10/11) = 36
    assert calculate_elo_change(1000, 1400) == 36


def test_favorite_beats_400_points_lower():
    # expected = 10/11; change = round(32 * 1/11) = 3
    assert calculate_elo_change(1400, 1000) == 3


def test_upset_always_pays_more_than_favorite_within_bracket():
    even = calculate_elo_change(1000, 1000)
    upset = calculate_elo_change(1000, 1100)
    favorite = calculate_elo_change(1000, 900)
    assert favorite < even < upset


def test_extreme_upset_caps_at_k():
    # 2000-point underdog: expected ~ 1e-5 -> change rounds to the full K.
    assert calculate_elo_change(1000, 3000) == 40
    assert calculate_elo_change(1500, 3500) == 32
    assert calculate_elo_change(2000, 4000) == 24


def test_extreme_favorite_floors_at_one():
    assert calculate_elo_change(3000, 1000) == 1
    assert calculate_elo_change(1000, 100) == 1


def test_minimum_change_is_one_never_zero_or_negative():
    for winner, loser in [(2400, 800), (1999, 400), (10_000, 0)]:
        assert calculate_elo_change(winner, loser) == 1


def test_change_always_within_one_and_k():
    for winner in range(0, 4001, 250):
        k = 40 if winner < 1200 else 32 if winner < 1800 else 24
        for loser in range(0, 4001, 250):
            change = calculate_elo_change(winner, loser)
            assert 1 <= change <= k, (winner, loser, change)


# ---------------------------------------------------------------------------
# calculate_elo_change: zero / negative / huge inputs
# ---------------------------------------------------------------------------


def test_zero_elo_players():
    assert calculate_elo_change(0, 0) == 20  # K = 40, even


def test_zero_elo_winner_beats_positive_loser():
    assert calculate_elo_change(0, 400) == 36


def test_negative_elo_inputs_still_produce_valid_change():
    # Negative ratings are representable (see the negative-elo bug below);
    # the formula itself keeps working.
    assert calculate_elo_change(-400, 0) == 36  # negative-rated underdog wins
    assert calculate_elo_change(0, -400) == 4  # K=40 favorite: round(40/11)
    assert calculate_elo_change(-1000, -1000) == 20  # K=40 even matchup


def test_huge_favorite_gap_underflows_to_minimum_change():
    # winner >> loser: 10^((loser-winner)/400) underflows to 0.0, expected
    # becomes exactly 1.0, and the max(1, ...) floor kicks in. No exception.
    assert calculate_elo_change(10**9, 1000) == 1


@pytest.mark.xfail(
    strict=True,
    raises=OverflowError,
    reason=(
        "BUG(elo-overflow): calculate_elo_change computes "
        "10 ** ((loser_elo - winner_elo) / 400) with floats, which raises "
        "OverflowError once the underdog gap exceeds ~123,600 points "
        "(exponent > 308). Unreachable through normal play, but the function "
        "has no input guard, so corrupted/synthetic ratings crash the "
        "answer-submission path with a 500 instead of capping at K."
    ),
)
def test_extreme_underdog_gap_should_cap_at_k_not_crash():
    assert calculate_elo_change(1000, 10**9) == 40  # currently: OverflowError


def test_current_behavior_extreme_underdog_gap_raises_overflow():
    # BUG: pins the current (crashing) behavior of the xfail above.
    with pytest.raises(OverflowError):
        calculate_elo_change(1000, 10**9)
    # The largest safe exponent is ~308: 400 * 308 = 123,200 still works.
    assert calculate_elo_change(1000, 1000 + 123_200) == 40


def test_elo_change_returns_int():
    for winner, loser in [(1000, 1000), (1234, 987), (2500, 2600)]:
        assert isinstance(calculate_elo_change(winner, loser), int)


# ---------------------------------------------------------------------------
# ranked completion: first-to-3 and elo_change on the match
# ---------------------------------------------------------------------------


def test_ranked_match_completes_at_three_wins(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    for expected_score in (1, 2):
        body = _win_round(client, auth_headers, match_id, PLAYER_A)
        assert body["player1_score"] == expected_score
        assert body["match_winner"] is None

    final = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert final["player1_score"] == 3
    assert final["match_winner"] == PLAYER_A
    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["winner_id"] == PLAYER_A


def test_completion_sets_elo_change_field_on_match(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)

    # Guest snapshots are 1000 vs 1000 -> K=40 even matchup -> 20.
    assert body["elo_change"] == 20
    assert main.in_memory_matches[match_id]["elo_change"] == 20


def test_status_endpoint_reports_elo_change_to_both_players(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_B, 2)
    _win_round(client, auth_headers, match_id, PLAYER_B)

    for player in (PLAYER_A, PLAYER_B):
        body = _status(client, auth_headers, match_id, player)
        assert body["status"] == "completed"
        assert body["winner_id"] == PLAYER_B
        assert body["elo_change"] == 20


def test_details_endpoint_reports_elo_change_after_completion(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    body = client.get(
        f"/match/{match_id}/details", headers=auth_headers(PLAYER_A)
    ).json()
    assert body["status"] == "completed"
    assert body["winner"] == PLAYER_A
    assert body["elo_change"] == 20
    assert body["score"] == "3-0"


def test_player2_win_completes_and_pays_player2(
    client, auth_headers, fixed_question, user_store
):
    user_store.add_user(PLAYER_A, elo=1000)
    user_store.add_user(PLAYER_B, elo=1000)
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_B, 2)
    body = _win_round(client, auth_headers, match_id, PLAYER_B)

    assert body["match_winner"] == PLAYER_B
    assert user_store.docs[PLAYER_B]["elo"] == 1020
    assert user_store.docs[PLAYER_B]["wins"] == 1
    assert user_store.docs[PLAYER_A]["elo"] == 980
    assert user_store.docs[PLAYER_A]["losses"] == 1


# ---------------------------------------------------------------------------
# ranked completion: user documents updated exactly once and correctly
# ---------------------------------------------------------------------------


def test_completion_issues_exactly_two_inc_updates(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    incs = user_store.inc_calls()
    assert len(incs) == 2
    assert incs[0]["filter"] == {"_id": PLAYER_A}
    assert incs[0]["update"] == {"$inc": {"elo": 20, "wins": 1}}
    assert incs[1]["filter"] == {"_id": PLAYER_B}
    assert incs[1]["update"] == {"$inc": {"elo": -20, "losses": 1}}


def test_no_elo_writes_before_match_point(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert body["elo_change"] == 0
    assert body["match_winner"] is None

    body = _win_round(client, auth_headers, match_id, PLAYER_B)
    assert body["elo_change"] == 0

    assert user_store.inc_calls() == []
    assert main.in_memory_matches[match_id]["elo_change"] == 0


def test_wrong_answers_never_touch_elo(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)  # even at match point
    client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "totally wrong"},
        headers=auth_headers(PLAYER_A),
    )
    body = response.json()
    assert body["correct"] is False
    assert body["elo_change"] == 0
    assert body["match_winner"] is None
    assert user_store.inc_calls() == []


def test_tie_rounds_never_touch_elo_even_at_match_point(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _set_score(match_id, PLAYER_B, 2)
    client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    )
    for player in (PLAYER_A, PLAYER_B):
        client.post(
            "/api/game/give-up",
            params={"match_id": match_id},
            headers=auth_headers(player),
        )

    match = main.in_memory_matches[match_id]
    assert match["status"] == "active"
    assert match["winner_id"] is None
    assert match["player1_score"] == 2 and match["player2_score"] == 2
    assert user_store.inc_calls() == []


def test_no_double_elo_after_completion(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)
    assert len(user_store.inc_calls()) == 2

    # Any further answer (even by the loser) is rejected and writes nothing.
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": "2*x"},
        headers=auth_headers(PLAYER_B),
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Match is already completed"
    assert len(user_store.inc_calls()) == 2


def test_completion_applies_deltas_to_stored_users(
    client, auth_headers, fixed_question, user_store
):
    user_store.add_user(PLAYER_A, elo=1000, wins=7, losses=3)
    user_store.add_user(PLAYER_B, elo=1000, wins=2, losses=9)
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    winner = user_store.docs[PLAYER_A]
    loser = user_store.docs[PLAYER_B]
    assert winner["elo"] == 1020 and winner["wins"] == 8 and winner["losses"] == 3
    assert loser["elo"] == 980 and loser["wins"] == 2 and loser["losses"] == 10


def test_completion_uses_match_snapshots_not_live_user_elo(
    client, auth_headers, fixed_question, user_store
):
    # The live user doc says 5000 but the match snapshotted 1000 at creation;
    # completion must compute from the snapshot (K=40 even -> 20), not the
    # live rating (which would be a K=24 blowout worth 1 point).
    user_store.add_user(PLAYER_A, elo=5000)
    user_store.add_user(PLAYER_B, elo=1000)
    match_id = _ranked_match(client, auth_headers)
    match = main.in_memory_matches[match_id]
    assert match["player1_elo"] == 1000 and match["player2_elo"] == 1000

    _set_score(match_id, PLAYER_A, 2)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert body["elo_change"] == 20  # snapshot math, not live-rating math
    # The delta is then applied on top of the (diverged) live rating.
    assert user_store.docs[PLAYER_A]["elo"] == 5020
    assert user_store.docs[PLAYER_B]["elo"] == 980


def test_completion_respects_mutated_snapshot_bracket(
    client, auth_headers, fixed_question, user_store
):
    # Push both snapshots into the advanced bracket: K=24 even -> 12.
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = 2000
    main.in_memory_matches[match_id]["player2_elo"] = 2000

    _set_score(match_id, PLAYER_A, 2)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert body["elo_change"] == 12


def test_completion_upset_uses_winner_snapshot_for_k(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = 1000  # underdog
    main.in_memory_matches[match_id]["player2_elo"] = 1400  # favorite

    _set_score(match_id, PLAYER_A, 2)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert body["elo_change"] == 36  # K=40 upset payout


def test_loser_decrement_mirrors_winner_change_exactly(
    client, auth_headers, fixed_question, user_store
):
    user_store.add_user(PLAYER_A, elo=1000)
    user_store.add_user(PLAYER_B, elo=1000)
    match_id = _ranked_match(client, auth_headers)
    main.in_memory_matches[match_id]["player1_elo"] = 1000
    main.in_memory_matches[match_id]["player2_elo"] = 1400

    _set_score(match_id, PLAYER_A, 2)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert body["elo_change"] == 36
    # Zero-sum: the favorite loses exactly what the underdog gains.
    assert user_store.docs[PLAYER_A]["elo"] == 1036
    assert user_store.docs[PLAYER_B]["elo"] == 964


# ---------------------------------------------------------------------------
# negative ELO
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(negative-elo): the loser update is a raw $inc with no floor, so "
        "a low-rated loser's stored ELO goes below zero.  Nothing anywhere "
        "clamps ratings to >= 0."
    ),
)
def test_user_elo_should_not_go_negative(
    client, auth_headers, fixed_question, user_store
):
    user_store.add_user(PLAYER_B, elo=5)
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    assert user_store.docs[PLAYER_B]["elo"] >= 0  # currently: -15


def test_current_behavior_user_elo_goes_negative(
    client, auth_headers, fixed_question, user_store
):
    # BUG: pins the current (wrong) behavior of the xfail above.
    user_store.add_user(PLAYER_B, elo=5)
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    # Snapshots were 1000/1000 -> change 20; the live 5 becomes -15.
    assert user_store.docs[PLAYER_B]["elo"] == -15


# ---------------------------------------------------------------------------
# friend match ELO behavior
# ---------------------------------------------------------------------------


def test_friend_match_completion_has_zero_elo_change(
    client, auth_headers, fixed_question, user_store
):
    match_id = _friend_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)

    assert body["match_winner"] == PLAYER_A
    assert body["elo_change"] == 0
    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["elo_change"] == 0


def test_friend_match_completion_writes_no_user_updates(
    client, auth_headers, fixed_question, user_store
):
    # Quirk: friend matches are fully unranked - not even wins/losses are
    # counted, because the whole $inc block is skipped for match_type
    # "friend" (only "random" and "ranked" qualify).
    user_store.add_user(PLAYER_A, elo=1000, wins=0, losses=0)
    user_store.add_user(PLAYER_B, elo=1000, wins=0, losses=0)
    match_id = _friend_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    assert user_store.inc_calls() == []
    assert user_store.docs[PLAYER_A] == {
        "_id": PLAYER_A,
        "elo": 1000,
        "wins": 0,
        "losses": 0,
    }
    assert user_store.docs[PLAYER_B]["elo"] == 1000


def test_friend_match_status_shows_zero_elo_change_after_completion(
    client, auth_headers, fixed_question, user_store
):
    match_id = _friend_match(client, auth_headers)
    _set_score(match_id, PLAYER_B, 2)
    _win_round(client, auth_headers, match_id, PLAYER_B)

    for player in (PLAYER_A, PLAYER_B):
        body = _status(client, auth_headers, match_id, player)
        assert body["status"] == "completed"
        assert body["winner_id"] == PLAYER_B
        assert body["elo_change"] == 0


def test_friend_match_snapshots_join_time_elo(client, auth_headers, user_store):
    # player2_elo starts as the default 1000 and is overwritten with the
    # joiner's ELO at join time; guests always join at 1000.
    match_id = _friend_match(client, auth_headers)
    match = main.in_memory_matches[match_id]
    assert match["player1_elo"] == 1000
    assert match["player2_elo"] == 1000
    assert match["elo_change"] == 0


# ---------------------------------------------------------------------------
# completion odds and ends
# ---------------------------------------------------------------------------


def test_scores_beyond_three_still_complete(client, auth_headers, fixed_question, user_store):
    # Defensive: a corrupted score above match point still ends the match on
    # the next won round (>= 3 check, not == 3).
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 5)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert body["player1_score"] == 6
    assert body["match_winner"] == PLAYER_A


def test_alternating_rounds_only_final_win_pays_elo(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    # A, B, A, B, A -> 3-2 for A; only the fifth round triggers ELO.
    for winner in (PLAYER_A, PLAYER_B, PLAYER_A, PLAYER_B):
        body = _win_round(client, auth_headers, match_id, winner)
        assert body["match_winner"] is None
        assert user_store.inc_calls() == []

    final = _win_round(client, auth_headers, match_id, PLAYER_A)
    assert final["player1_score"] == 3
    assert final["player2_score"] == 2
    assert final["match_winner"] == PLAYER_A
    assert final["elo_change"] == 20
    assert len(user_store.inc_calls()) == 2


def test_loser_of_completed_match_sees_result_via_status(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    body = _status(client, auth_headers, match_id, PLAYER_B)
    assert body["status"] == "completed"
    assert body["winner_id"] == PLAYER_A
    assert body["elo_change"] == 20
    assert body["player1_score"] == 3


def test_completed_match_keeps_winner_and_elo_on_repeated_polls(
    client, auth_headers, fixed_question, user_store
):
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    _win_round(client, auth_headers, match_id, PLAYER_A)

    for _ in range(3):
        body = _status(client, auth_headers, match_id, PLAYER_A)
        assert body["winner_id"] == PLAYER_A
        assert body["elo_change"] == 20
    # Polling status never re-touches user docs.
    assert len(user_store.inc_calls()) == 2


def test_guest_ranked_completion_incs_nonexistent_user_docs_silently(
    client, auth_headers, fixed_question, user_store
):
    # Quirk: guests have no users_collection documents, but completion still
    # issues both $inc updates; they match nothing and the ELO "change" the
    # players were shown is never persisted anywhere except the match doc.
    match_id = _ranked_match(client, auth_headers)
    _set_score(match_id, PLAYER_A, 2)
    body = _win_round(client, auth_headers, match_id, PLAYER_A)

    assert body["elo_change"] == 20
    assert len(user_store.inc_calls()) == 2
    assert user_store.docs == {}  # nothing was ever created


def test_ranked_snapshot_ignores_queue_elo_for_guests(
    client, auth_headers, fixed_question, user_store
):
    # Quirk: the queued player's ELO snapshot comes from a hardcoded
    # {"elo": 1000} fallback for non-ObjectId ids, even if the queue entry
    # recorded something else.
    _start(client, auth_headers, PLAYER_A)
    main.matchmaking_queue[PLAYER_A]["elo"] = 1777
    body = _start(client, auth_headers, PLAYER_B)
    match = main.in_memory_matches[body["match_id"]]
    assert match["player1_elo"] == 1000  # joiner (current_user elo)
    assert match["player2_elo"] == 1000  # queued guest: hardcoded fallback
