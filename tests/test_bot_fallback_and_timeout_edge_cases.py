"""
Edge-case tests for the bot fallback path of ranked matchmaking and for bot
round timing/timeout mechanics (main.py: start_match ~1087 bot branch,
_create_next_round ~1420 time_limit branch, submit_answer ~1610 timeout +
bot-simulation branches, give_up_round ~1509 bot auto-give-up).

Scope (deliberately deeper than tests/test_ranked_matchmaking_edge_cases.py
and the timeout tests in tests/test_match_answer_and_scoring_edge_cases.py):
- the full bot match document created after 10s in queue, and what happens
  to the bot "user" object (it is thrown away)
- bot name drawn from the fixed 7-name roster; the three different names the
  same bot has across endpoints (start response / status / by-code)
- bot ELO offset window -150..-50 (spied bounds + unpatched sampling)
- time_limit recomputation per round, and the match_type+player2_id
  conjunction that gates every bot branch
- timeout forfeits driven by monkeypatching main.utc_now (frozen clock),
  including the exact ">" boundary and wrong-vs-correct answers after the
  deadline
- three timeout losses completing the match with EXACT pinned ELO values,
  and the loser-only DB write asymmetry of the timeout path
- deterministic bot races: bot wrong => user wins, bot slow => user wins,
  bot fast => bot wins even though the response says correct: True, exact
  time tie => bot wins
- the bot only "responds" when the user answers correctly (no dice roll on
  wrong answers)
- human preferred over bot even when everyone is past the 10s deadline
- cancel interactions with the bot-creation gate (strict xfail for the
  stale-cancel-flag bug hitting the BOT path after a re-queue)
- bot presence: give-up against a bot instantly ties because the bot "gives
  up too", and the bot is always reported connected

Known bugs are documented with strict xfail plus a companion test pinning
current behavior.  See MATCH_EDGE_CASE_REPORT.md for the summary.
"""

from datetime import datetime, timedelta

import pytest

import main


PLAYER = "guest-fbk-aaa"
OTHER = "guest-fbk-bbb"
THIRD = "guest-fbk-ccc"

CORRECT = "2*x"  # matches fixed_question's answer "2·x"
WRONG = "999"

BOT_ROSTER = [
    "James (bot)",
    "Alex (bot)",
    "Sam (bot)",
    "Taylor (bot)",
    "Jordan (bot)",
    "Casey (bot)",
    "Morgan (bot)",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _start(client, auth_headers, player):
    response = client.post(
        "/api/game/start", json={"mode": "random"}, headers=auth_headers(player)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _backdate_queue(player, seconds):
    main.matchmaking_queue[player]["joined_at"] = datetime.utcnow() - timedelta(
        seconds=seconds
    )


def _bot_match(client, auth_headers, player=PLAYER):
    """Queue `player`, expire the 10s window, poll again -> bot match."""
    assert _start(client, auth_headers, player)["status"] == "searching"
    _backdate_queue(player, 11)
    body = _start(client, auth_headers, player)
    assert body["status"] == "matched", body
    match = main.in_memory_matches[body["match_id"]]
    assert match["player2_id"] == "bot-opponent"
    return body


def _question(client, auth_headers, match_id, player=PLAYER):
    response = client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _answer(client, auth_headers, match_id, answer, player=PLAYER):
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _round_start(match_id):
    parsed = main.parse_round_start(
        main.in_memory_matches[match_id]["round_start_time"]
    )
    assert parsed is not None
    return parsed


def _freeze_clock(monkeypatch, instant):
    monkeypatch.setattr(main, "utc_now", lambda: instant)


@pytest.fixture
def pinned_bot_offset(monkeypatch):
    """Pin the bot ELO offset to -100 (guest 1000 -> bot 900) and record the
    randint bounds start_match asked for."""
    seen_bounds = []

    def fake_randint(a, b):
        seen_bounds.append((a, b))
        return -100

    monkeypatch.setattr(main.random, "randint", fake_randint)
    return seen_bounds


@pytest.fixture
def bot_always_wrong(monkeypatch):
    """random.random() -> 0.99 so bot_correct is always False (user wins the
    race whenever their answer is correct)."""
    monkeypatch.setattr(main.random, "random", lambda: 0.99)


@pytest.fixture
def elo_writes(monkeypatch):
    """Spy on users_collection.update_one to capture ELO/wins/losses $inc."""
    calls = []

    async def spy_update_one(query, update, *args, **kwargs):
        calls.append((query, update))

    monkeypatch.setattr(main.users_collection, "update_one", spy_update_one)
    return calls


@pytest.fixture
def time_travel(monkeypatch):
    """Shift main.utc_now() forward by an accumulating offset, so multi-round
    timeout scenarios can 'wait out' each round without real sleeps."""
    state = {"offset": 0.0}
    real_utc_now = main.utc_now

    monkeypatch.setattr(
        main,
        "utc_now",
        lambda: real_utc_now() + timedelta(seconds=state["offset"]),
    )

    def travel(seconds):
        state["offset"] += seconds

    return travel


# ---------------------------------------------------------------------------
# bot match creation details after 10s in the queue
# ---------------------------------------------------------------------------


def test_bot_match_full_document_and_response_shape(
    client, auth_headers, pinned_bot_offset
):
    body = _bot_match(client, auth_headers)

    assert set(body.keys()) == {"status", "match_id", "match_code", "opponent"}
    assert body["opponent"] in BOT_ROSTER

    match = main.in_memory_matches[body["match_id"]]
    # Counter-based id like ranked human matches (shared counter/namespace).
    assert body["match_id"] == "match-1"
    assert len(match["match_code"]) == 11  # secrets.token_urlsafe(8)
    assert match["match_type"] == "random"
    assert str(match["player1_id"]) == PLAYER
    assert match["player2_id"] == "bot-opponent"
    assert match["player1_score"] == 0 and match["player2_score"] == 0
    assert match["player1_elo"] == 1000
    assert match["player2_elo"] == 900  # 1000 + pinned -100 offset
    assert match["status"] == "active"
    assert match["winner_id"] is None
    assert match["elo_change"] == 0
    assert match["rounds"] == []
    # Same naive-utcnow timestamp regime as human matches.
    assert isinstance(match["created_at"], datetime)
    assert match["created_at"].tzinfo is None
    # Queue entry fully consumed.
    assert PLAYER not in main.matchmaking_queue


def test_bot_user_object_is_never_persisted(client, auth_headers):
    # start_match builds a full bot "user" dict (email, name, wins, losses)
    # and then throws it away: only the ELO makes it into the match doc.
    body = _bot_match(client, auth_headers)
    match = main.in_memory_matches[body["match_id"]]

    assert main.in_memory_users == {}
    assert "name" not in match and "email" not in match
    flattened = str(match)
    assert "(bot)" not in flattened
    assert "bot@derivative-duel.com" not in flattened


def test_queue_deadline_boundary_searching_at_9_5s_bot_at_10s(
    client, auth_headers
):
    assert _start(client, auth_headers, PLAYER)["status"] == "searching"

    # 9.5s in queue: still searching, and int() truncation already reports
    # 0 seconds remaining while refusing to create the bot.
    _backdate_queue(PLAYER, 9.5)
    poll = _start(client, auth_headers, PLAYER)
    assert poll == {"status": "searching", "time_remaining": 0}
    assert main.in_memory_matches == {}

    # 10s in queue (strict `< 10` check fails): bot match on this poll.
    _backdate_queue(PLAYER, 10)
    body = _start(client, auth_headers, PLAYER)
    assert body["status"] == "matched"
    assert body["opponent"].endswith("(bot)")


# ---------------------------------------------------------------------------
# bot name roster and the three names one bot answers to
# ---------------------------------------------------------------------------


def test_bot_name_is_drawn_from_the_fixed_roster_via_random_choice(
    client, auth_headers, monkeypatch
):
    seen_rosters = []

    def spy_choice(seq):
        seen_rosters.append(list(seq))
        return seq[3]  # "Taylor (bot)"

    monkeypatch.setattr(main.random, "choice", spy_choice)

    body = _bot_match(client, auth_headers)
    assert seen_rosters == [BOT_ROSTER]
    assert body["opponent"] == "Taylor (bot)"


def test_unpatched_bot_names_and_offsets_stay_in_documented_ranges(
    client, auth_headers
):
    # Sample several real (unpatched RNG) bot matches under distinct guests.
    for i in range(8):
        player = f"guest-fbk-sample-{i}"
        body = _bot_match(client, auth_headers, player)
        match = main.in_memory_matches[body["match_id"]]

        assert body["opponent"] in BOT_ROSTER
        offset = match["player2_elo"] - match["player1_elo"]
        assert -150 <= offset <= -50


def test_bot_offset_randint_bounds_are_minus150_to_minus50(
    client, auth_headers, pinned_bot_offset
):
    _bot_match(client, auth_headers)
    assert pinned_bot_offset == [(-150, -50)]


def test_same_bot_has_three_different_names_across_endpoints(
    client, auth_headers
):
    # QUIRK: the roster name only ever exists in the start_match response.
    # The status endpoint invents "AI Opponent" and the by-code endpoint
    # invents "Bot"; nothing stored on the match can reproduce the roster
    # name after the first response is gone.
    body = _bot_match(client, auth_headers)
    roster_name = body["opponent"]
    assert roster_name in BOT_ROSTER

    status = client.get(
        f"/api/game/status/{body['match_id']}", headers=auth_headers(PLAYER)
    )
    assert status.status_code == 200
    assert status.json()["player2_name"] == "AI Opponent"

    by_code = client.get(
        f"/api/game/match/{body['match_code']}", headers=auth_headers(PLAYER)
    )
    assert by_code.status_code == 200
    assert by_code.json()["opponent_name"] == "Bot"
    assert by_code.json()["is_opponent_bot"] is True


# ---------------------------------------------------------------------------
# time_limit mechanics beyond the static brackets
# ---------------------------------------------------------------------------


def test_time_limit_is_recomputed_for_every_round(
    client, auth_headers, fixed_question, bot_always_wrong
):
    # Each round re-reads player1_elo, so a mid-match ELO edit moves the
    # NEXT round's limit while the current round keeps its own.
    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]

    first = _question(client, auth_headers, match_id)
    assert first["time_limit"] == 16  # elo 1000 -> base 15 + difficulty 1

    win = _answer(client, auth_headers, match_id, CORRECT)
    assert win["correct"] is True and win["player1_score"] == 1

    main.in_memory_matches[match_id]["player1_elo"] = 2000
    second = _question(client, auth_headers, match_id)
    assert second["round_id"] != first["round_id"]
    assert second["time_limit"] == 9  # elo 2000 -> base 8 + difficulty 1
    # The finished round keeps the limit it was created with.
    assert main.in_memory_rounds[first["round_id"]]["time_limit"] == 16


def test_friend_match_with_bot_player2_id_is_not_a_bot_match(
    client, auth_headers, fixed_question
):
    # Every bot branch requires match_type == "random" AND player2_id ==
    # "bot-opponent".  A friend match whose player2_id is forced to the bot
    # sentinel gets NO time limit and NO bot answer simulation.
    created = client.post(
        "/api/game/friend/create", json={}, headers=auth_headers(PLAYER)
    ).json()
    match_id = created["match_id"]
    main.in_memory_matches[match_id]["player2_id"] = "bot-opponent"
    main.in_memory_matches[match_id]["status"] = "active"

    question = _question(client, auth_headers, match_id)
    assert "time_limit" not in question

    body = _answer(client, auth_headers, match_id, CORRECT)
    assert body["correct"] is True
    assert body["round_winner"] == PLAYER  # instant win, no bot race


def test_random_type_match_with_human_player2_is_not_a_bot_match(
    client, auth_headers, fixed_question
):
    # The other half of the conjunction: match_type "random" with a human
    # in the player2 seat behaves like a plain human match.
    match_id = "match-legacy-random"
    main.in_memory_matches[match_id] = {
        "_id": match_id,
        "match_code": "LEGACYRAND1",
        "match_type": "random",
        "player1_id": PLAYER,
        "player2_id": OTHER,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": 1000,
        "player2_elo": 1000,
        "status": "active",
        "winner_id": None,
        "elo_change": 0,
        "rounds": [],
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    question = _question(client, auth_headers, match_id)
    assert "time_limit" not in question

    body = _answer(client, auth_headers, match_id, CORRECT, player=OTHER)
    assert body["correct"] is True
    assert body["round_winner"] == OTHER  # first correct answer wins outright


# ---------------------------------------------------------------------------
# timeout losses via a frozen clock (monkeypatched main.utc_now)
# ---------------------------------------------------------------------------


def test_slow_player_forfeits_round_when_clock_passes_time_limit(
    client, auth_headers, fixed_question, monkeypatch
):
    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    question = _question(client, auth_headers, match_id)

    start = _round_start(match_id)
    _freeze_clock(monkeypatch, start + timedelta(seconds=question["time_limit"] + 1))

    late = _answer(client, auth_headers, match_id, CORRECT)
    assert late["correct"] is False  # a CORRECT answer still forfeits
    assert late["already_won"] is True
    assert late["round_winner"] == "bot-opponent"
    assert late["player2_score"] == 1
    assert late["match_winner"] is None
    assert late["message"] == "Time limit exceeded"


def test_answer_exactly_at_time_limit_is_not_a_timeout(
    client, auth_headers, fixed_question, monkeypatch, bot_always_wrong
):
    # The check is strict `elapsed > time_limit`: landing exactly ON the
    # limit still gets the answer evaluated (and wins, bot forced wrong).
    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    question = _question(client, auth_headers, match_id)

    start = _round_start(match_id)
    _freeze_clock(monkeypatch, start + timedelta(seconds=question["time_limit"]))

    on_time = _answer(client, auth_headers, match_id, CORRECT)
    assert on_time["correct"] is True
    assert on_time["round_winner"] == PLAYER
    assert on_time["player1_score"] == 1


def test_wrong_answer_after_deadline_also_hands_bot_the_round(
    client, auth_headers, fixed_question, monkeypatch
):
    # The timeout check runs before the answer is even parsed, so a wrong
    # answer after the deadline scores the bot exactly like a correct one.
    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    question = _question(client, auth_headers, match_id)

    start = _round_start(match_id)
    _freeze_clock(monkeypatch, start + timedelta(seconds=question["time_limit"] + 5))

    late = _answer(client, auth_headers, match_id, WRONG)
    assert late["round_winner"] == "bot-opponent"
    assert late["player2_score"] == 1
    assert late["message"] == "Time limit exceeded"


def test_three_timeouts_complete_match_with_exact_elo_and_loser_only_write(
    client, auth_headers, fixed_question, pinned_bot_offset, elo_writes, time_travel
):
    # Bot pinned at 900 vs player 1000: winner elo 900 < 1200 -> K=40,
    # expected = 1/(1+10^0.25), change = round(40*(1-expected)) = 26.
    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]

    responses = []
    for _ in range(3):
        question = _question(client, auth_headers, match_id)
        # Round start is ~3s out; jump past start + time_limit.
        time_travel(question["time_limit"] + 5)
        responses.append(_answer(client, auth_headers, match_id, CORRECT))

    assert [r["player2_score"] for r in responses] == [1, 2, 3]
    final = responses[-1]
    assert final["match_winner"] == "bot-opponent"
    assert final["elo_change"] == 26
    assert final["elo_change"] == main.calculate_elo_change(900, 1000)

    match = main.in_memory_matches[match_id]
    assert match["status"] == "completed"
    assert match["winner_id"] == "bot-opponent"
    assert match["elo_change"] == 26

    # ASYMMETRY: the timeout completion path writes ONLY the human loser
    # (-elo, +1 loss).  Unlike the answer path, the winner (bot) never gets
    # a wins increment anywhere.
    assert len(elo_writes) == 1
    query, update = elo_writes[0]
    assert str(query["_id"]) == PLAYER
    assert update["$inc"] == {"elo": -26, "losses": 1}


def test_beating_the_bot_pays_exact_elo_and_writes_both_sides(
    client, auth_headers, fixed_question, pinned_bot_offset, bot_always_wrong,
    elo_writes,
):
    # Winner elo 1000 < 1200 -> K=40, expected = 1/(1+10^-0.25),
    # change = round(40*(1-expected)) = 14.  Note the asymmetry with the
    # loss case (26): the ELO at stake depends on who wins.
    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]

    for expected_score in (1, 2, 3):
        _question(client, auth_headers, match_id)
        win = _answer(client, auth_headers, match_id, CORRECT)
        assert win["correct"] is True
        assert win["player1_score"] == expected_score

    assert win["match_winner"] == PLAYER
    assert win["elo_change"] == 14
    assert win["elo_change"] == main.calculate_elo_change(1000, 900)

    # The win path writes BOTH sides -- including a $inc against the
    # nonexistent "bot-opponent" user document (a no-op in real Mongo).
    incs = {str(query["_id"]): update["$inc"] for query, update in elo_writes}
    assert incs[PLAYER] == {"elo": 14, "wins": 1}
    assert incs["bot-opponent"] == {"elo": -14, "losses": 1}


# ---------------------------------------------------------------------------
# deterministic bot races on correct answers
# ---------------------------------------------------------------------------


def test_correct_answer_beats_bot_that_rolled_wrong(
    client, auth_headers, fixed_question, bot_always_wrong
):
    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    _question(client, auth_headers, match_id)

    win = _answer(client, auth_headers, match_id, CORRECT)
    assert win["correct"] is True
    assert win["round_winner"] == PLAYER
    assert win["player1_score"] == 1 and win["player2_score"] == 0


def test_fast_correct_answer_beats_slow_correct_bot(
    client, auth_headers, fixed_question, monkeypatch
):
    # Bot rolls correct (random -> 0.0) but is very slow (uniform -> 999):
    # the user answered "before the bot" and takes the round.
    monkeypatch.setattr(main.random, "random", lambda: 0.0)
    monkeypatch.setattr(main.random, "uniform", lambda a, b: 999.0)

    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    _question(client, auth_headers, match_id)

    win = _answer(client, auth_headers, match_id, CORRECT)
    assert win["correct"] is True
    assert win["round_winner"] == PLAYER


def test_slow_correct_answer_loses_to_fast_bot_despite_correct_true(
    client, auth_headers, fixed_question, monkeypatch
):
    # QUIRK: when the bot wins the race the response still says
    # correct: True -- "you were right, but the bot was faster" is only
    # visible through round_winner/player2_score.
    monkeypatch.setattr(main.random, "random", lambda: 0.0)  # bot correct
    monkeypatch.setattr(main.random, "uniform", lambda a, b: 5.0)  # bot at 5s

    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    _question(client, auth_headers, match_id)

    start = _round_start(match_id)
    _freeze_clock(monkeypatch, start + timedelta(seconds=10))  # user at 10s

    lost = _answer(client, auth_headers, match_id, CORRECT)
    assert lost["correct"] is True
    assert lost["round_winner"] == "bot-opponent"
    assert lost["player1_score"] == 0 and lost["player2_score"] == 1


def test_exact_time_tie_goes_to_the_bot(
    client, auth_headers, fixed_question, monkeypatch
):
    # user_time < bot_time is strict, so an exact tie is a bot round.
    monkeypatch.setattr(main.random, "random", lambda: 0.0)
    monkeypatch.setattr(main.random, "uniform", lambda a, b: 5.0)

    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    _question(client, auth_headers, match_id)

    start = _round_start(match_id)
    _freeze_clock(monkeypatch, start + timedelta(seconds=5))  # user also at 5s

    tie = _answer(client, auth_headers, match_id, CORRECT)
    assert tie["round_winner"] == "bot-opponent"


def test_answer_before_synced_round_start_clamps_user_time_to_zero(
    client, auth_headers, fixed_question, monkeypatch
):
    # round_start_time is scheduled ~3s in the future; answering during the
    # countdown yields a negative elapsed time that is clamped to 0.0, which
    # beats any positive bot_time.
    monkeypatch.setattr(main.random, "random", lambda: 0.0)  # bot correct
    monkeypatch.setattr(main.random, "uniform", lambda a, b: 0.001)  # bot fast

    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    _question(client, auth_headers, match_id)
    # Real clock: we are still inside the 3s countdown, so user_time == 0.0.

    win = _answer(client, auth_headers, match_id, CORRECT)
    assert win["round_winner"] == PLAYER


def test_bot_only_responds_when_the_user_answers_correctly(
    client, auth_headers, fixed_question, monkeypatch
):
    # The bot's dice roll happens inside the correct-answer branch.  Wrong
    # answers never trigger it: the bot cannot win a round off a user's
    # wrong answer (only off the time limit).
    rolls = []
    real_random = main.random.random

    def counting_random():
        rolls.append(1)
        return real_random()

    monkeypatch.setattr(main.random, "random", counting_random)

    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    _question(client, auth_headers, match_id)

    wrong = _answer(client, auth_headers, match_id, WRONG)
    assert wrong["correct"] is False
    assert wrong["round_winner"] is None
    assert wrong["player2_score"] == 0
    assert rolls == []  # no bot simulation on a wrong answer

    monkeypatch.setattr(main.random, "random", lambda: 0.99)
    win = _answer(client, auth_headers, match_id, CORRECT)
    assert win["round_winner"] == PLAYER


# ---------------------------------------------------------------------------
# humans beat bots to the punch; third wheel gets the bot
# ---------------------------------------------------------------------------


def test_two_stale_humans_pair_while_the_third_gets_a_bot(
    client, auth_headers
):
    # All three users are past the 10s bot deadline.  The queue scan runs
    # before the deadline check, so the first poller pairs with a human and
    # only the leftover user falls back to a bot.
    stale = datetime.utcnow() - timedelta(seconds=15)
    for player in (PLAYER, OTHER, THIRD):
        main.matchmaking_queue[player] = {"elo": 1000, "joined_at": stale}

    paired = _start(client, auth_headers, PLAYER)
    assert paired["status"] == "matched"
    human_match = main.in_memory_matches[paired["match_id"]]
    assert human_match["match_type"] == "ranked"
    assert human_match["player2_id"] != "bot-opponent"
    assert {str(human_match["player1_id"]), str(human_match["player2_id"])} == {
        PLAYER,
        OTHER,
    }

    # THIRD is alone now; their (stale) poll goes straight to the bot.
    leftover = _start(client, auth_headers, THIRD)
    assert leftover["status"] == "matched"
    bot_match = main.in_memory_matches[leftover["match_id"]]
    assert bot_match["match_type"] == "random"
    assert bot_match["player2_id"] == "bot-opponent"


# ---------------------------------------------------------------------------
# cancel vs the bot-creation gate
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG(stale-cancel-flag, bot path): a user who cancels and then "
        "re-queues keeps the cancelled_users flag.  When their NEW 10s wait "
        "expires, the bot-creation gate consumes the stale flag and answers "
        "{'status': 'cancelled'} instead of creating the bot match they "
        "were promised."
    ),
)
def test_requeued_user_should_get_bot_after_second_ten_second_wait(
    client, auth_headers
):
    assert _start(client, auth_headers, PLAYER)["status"] == "searching"
    client.post("/api/game/cancel", headers=auth_headers(PLAYER))

    # User changes their mind and searches again: a fresh queue entry.
    assert _start(client, auth_headers, PLAYER)["status"] == "searching"
    _backdate_queue(PLAYER, 11)

    body = _start(client, auth_headers, PLAYER)
    assert body["status"] == "matched"  # currently: "cancelled"


def test_current_behavior_cancel_then_requeue_saga_needs_three_waits(
    client, auth_headers
):
    # BUG pin for the xfail above: the full endpoint saga a real client
    # would live through.  cancel -> re-queue -> first deadline eats the
    # stale flag ("cancelled", no match) -> silently re-queued again ->
    # only the SECOND post-cancel deadline finally yields the bot.
    assert _start(client, auth_headers, PLAYER)["status"] == "searching"
    client.post("/api/game/cancel", headers=auth_headers(PLAYER))
    assert PLAYER not in main.matchmaking_queue
    assert PLAYER in main.cancelled_users

    # Re-queue: fresh timer, stale flag survives.
    requeued = _start(client, auth_headers, PLAYER)
    assert requeued == {"status": "searching", "time_remaining": 10}
    assert PLAYER in main.cancelled_users

    _backdate_queue(PLAYER, 11)
    swallowed = _start(client, auth_headers, PLAYER)
    assert swallowed == {"status": "cancelled"}  # bogus: user wanted a match
    assert main.in_memory_matches == {}
    assert PLAYER not in main.cancelled_users  # flag consumed here

    # Poll again: queued once more; after another 10s the bot finally comes.
    assert _start(client, auth_headers, PLAYER)["status"] == "searching"
    _backdate_queue(PLAYER, 11)
    body = _start(client, auth_headers, PLAYER)
    assert body["status"] == "matched"
    assert body["opponent"].endswith("(bot)")


# ---------------------------------------------------------------------------
# bot presence and give-up
# ---------------------------------------------------------------------------


def test_bot_reported_connected_and_named_ai_opponent_forever(
    client, auth_headers
):
    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]

    # The bot never polls, and even a poisoned ancient heartbeat is ignored:
    # "bot-opponent" short-circuits to connected before any bookkeeping.
    main.in_memory_matches[match_id]["player_last_seen"] = {
        "bot-opponent": main.utc_now() - timedelta(days=30)
    }

    status = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(PLAYER)
    ).json()
    assert status["opponent_connected"] is True
    assert status["player2_name"] == "AI Opponent"


def test_give_up_against_bot_instantly_ties_and_advances(
    client, auth_headers, fixed_question
):
    # The bot "gives up too": a single give-up immediately resolves the
    # round as a tie (no waiting_for_opponent limbo like human matches).
    body = _bot_match(client, auth_headers)
    match_id = body["match_id"]
    first = _question(client, auth_headers, match_id)

    gave_up = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER),
    )
    assert gave_up.status_code == 200
    assert gave_up.json()["status"] == "both_gave_up"
    assert gave_up.json()["round_winner"] == "tie"

    # Nobody scored, and the next question is a brand-new round.
    match = main.in_memory_matches[match_id]
    assert match["player1_score"] == 0 and match["player2_score"] == 0
    second = _question(client, auth_headers, match_id)
    assert second["round_id"] != first["round_id"]


# ---------------------------------------------------------------------------
# match_type: bot "random" vs human "ranked"
# ---------------------------------------------------------------------------


def test_bot_and_human_matches_use_different_match_types(client, auth_headers):
    bot_body = _bot_match(client, auth_headers)
    assert main.in_memory_matches[bot_body["match_id"]]["match_type"] == "random"

    assert _start(client, auth_headers, OTHER)["status"] == "searching"
    human_body = _start(client, auth_headers, THIRD)
    assert human_body["status"] == "matched"
    human_match = main.in_memory_matches[human_body["match_id"]]
    assert human_match["match_type"] == "ranked"

    # by-code agrees on who is a bot and who is not.
    bot_view = client.get(
        f"/api/game/match/{bot_body['match_code']}", headers=auth_headers(PLAYER)
    ).json()
    human_view = client.get(
        f"/api/game/match/{human_body['match_code']}", headers=auth_headers(THIRD)
    ).json()
    assert bot_view["is_opponent_bot"] is True
    assert human_view["is_opponent_bot"] is False
    assert human_view["opponent_name"] == "Guest"


def test_human_guest_with_bot_substring_in_id_is_mislabeled_as_bot(
    client, auth_headers
):
    # QUIRK/BUG (documented): the by-code endpoint decides "is this a bot?"
    # with `"bot" in str(opponent_id)`.  A perfectly human guest whose id
    # merely CONTAINS "bot" (abbot, botany, robot...) is reported as a bot
    # opponent to the other player -- while every actual bot branch in the
    # game (time limits, auto give-up, answer simulation) correctly ignores
    # them because those compare against the exact "bot-opponent" sentinel.
    abbot = "guest-abbot-1234"
    assert _start(client, auth_headers, abbot)["status"] == "searching"
    body = _start(client, auth_headers, OTHER)
    assert body["status"] == "matched"
    assert main.in_memory_matches[body["match_id"]]["match_type"] == "ranked"

    view = client.get(
        f"/api/game/match/{body['match_code']}", headers=auth_headers(OTHER)
    ).json()
    assert view["is_opponent_bot"] is True  # human labeled as bot
    # The "guest" check runs first, so at least the display name says Guest.
    assert view["opponent_name"] == "Guest"
