# Match Edge Case Report

## Ranked matchmaking (`/api/game/start`, `/api/game/cancel`, `/api/game/active`)

Findings from `tests/test_ranked_matchmaking_edge_cases.py` (44 tests: 42 pass,
2 `xfail` documenting real bugs). All state is process-local
(`matchmaking_queue`, `cancelled_users`, `in_memory_matches`); time-based
branches were exercised by backdating the naive `datetime.utcnow()` timestamps
the production code compares against.

### Bugs (xfail tests)

1. **Stale cancel flag poisons the next pairing** —
   `test_requeued_user_after_cancel_is_matchable_again` (xfail).
   `/api/game/cancel` adds the user to `cancelled_users`, but re-queueing via
   `/api/game/start` never clears that flag. When an opponent later pairs with
   the re-queued user, `start_match` sees the stale flag, pops **both** players
   from the queue, and returns `{"status": "cancelled"}` to the opponent — who
   never cancelled anything. The re-queued user is silently dropped from the
   queue. Current behavior is pinned by
   `test_current_behavior_stale_cancel_flag_aborts_pairing`.

2. **Double-pairing race for ObjectId opponents** —
   `test_concurrent_joiners_with_objectid_queued_player_no_double_pairing`
   (xfail). For registered (ObjectId) opponents, `start_match` performs
   `await users_collection.find_one(...)` **between** selecting the opponent
   from the queue and popping them. Two concurrent callers can both select the
   same queued player at that await point, and each creates its own match with
   that player, leaving them in two simultaneously "active" matches. Guest
   (string-id) opponents skip the await, so the guest path serializes and is
   not affected (covered by the passing concurrency tests).

### Quirks / current behavior worth knowing (asserted in passing tests)

- **Late cancel never consumes the flag on the reconnect path.** If a user
  cancels *after* a match was already created for them, their next
  `/api/game/start` reconnects them to the match (correct), but the
  `cancelled_users` entry is never removed on that path, feeding bug 1 later.
- **`continue_existing=True` does not reconnect.** It only skips marking the
  stale (>5s old) match `abandoned`, then falls through to normal matchmaking.
  The caller is told `"searching"` while the old match silently stays active;
  if an opponent is queued, the user ends up with **two active matches**, and
  `/api/game/active` reports the *older* one (insertion order).
- **`MatchStart.mode` is ignored.** `{"mode": "friend"}` (or any string) still
  enters the ranked queue and produces a `match_type: "ranked"` match.
- **No-auth and invalid-JWT requests collapse into one shared identity**
  (`guest-user-id`, demo mode). Two anonymous browsers share a queue slot and
  can never match each other, but an anonymous user *can* match an explicit
  `guest-xxx` user. An explicit `Bearer guest-user-id` token is the same
  identity as no auth at all.
- **ELO snapshot fallback for guests.** The queue entry stores the queued
  user's ELO, but for non-ObjectId (guest) opponents `start_match` ignores the
  queue snapshot and hardcodes `{"elo": 1000}` when building the match doc.
- **Bot fallback ordering.** The human-opponent scan runs before the 10s
  timeout check, so two users who both waited past 10s still pair with each
  other rather than each getting a bot. Bot matches use
  `match_type: "random"`, `player2_id: "bot-opponent"`, and a bot ELO 50–150
  below the player.
- **Reconnect window works as designed**: an active match created <5s ago is
  returned to a re-polling player without a duplicate match; >5s old it is
  marked `abandoned` (unless `continue_existing`) and the user re-enters the
  queue, after which the same two players can legitimately re-match.
- **Completed matches** neither trigger the reconnect window nor get
  abandoned; a user can queue and match again immediately after finishing.
- **Self-match is impossible** via double-polling: the queue scan skips the
  caller's own entry, and queue entries are fully removed after a successful
  match.

## Friend matches (`/api/game/friend/create|join|status`, `/api/game/match/{code}`)

Findings from `tests/test_friend_match_edge_cases.py` (45 tests: 43 pass,
2 strict `xfail` documenting real bugs).

### Bugs (xfail or explicitly demonstrated)

1. **Match-code uniqueness ignores in-memory matches** —
   `test_match_codes_unique_even_when_rng_repeats` (xfail).
   `create_friend_match` only loops on `matches_collection.find_one` to check
   for a code collision. Matches held in `in_memory_matches` are never
   consulted, so when the DB is empty or unavailable two live matches can
   share the same 6-char code. The companion test
   `test_colliding_codes_shadow_the_second_match_on_join` shows the fallout:
   the join scan resolves the code to whichever match was created first, so
   the second match becomes permanently unreachable by code.

2. **Concurrent-join race (no per-match lock)** —
   `test_concurrent_joins_with_db_latency_both_succeed`.
   `join_friend_match` does check-then-set with no `get_match_lock` (unlike
   `get_question`, which was already fixed for the round-fork race). When the
   match document is served from the DB with any latency, two players can
   both read `status == "waiting"`, both get a 200, and the second writer
   silently overwrites `player2_id` — the first acknowledged joiner is kicked
   out of the match. The purely in-memory path happens to be atomic on the
   event loop (`test_sequential_like_concurrent_joins_without_db_reject_second`),
   so the race only bites when the DB lookup is involved.

3. **Code lookup case-sensitivity is inconsistent** —
   `test_get_match_by_code_accepts_lowercase_code` (xfail).
   `/api/game/friend/join` and `/api/game/friend/status/{code}` normalize the
   code with `.upper()`, but `/api/game/match/{match_code}` compares
   case-sensitively, so a lowercase code that works everywhere else 404s
   there.

### Quirks / current behavior worth knowing (asserted in passing tests)

- **Join is not idempotent.** Once player 2 has joined, a retried join by the
  *same* player gets `400 "Match already started"`; clients must not retry.
- **Misleading error for finished matches.** Joining a `completed` or
  `abandoned` match also returns `"Match already started"`.
- **Unknown `opponent_username` degrades silently.** If the username does not
  resolve, the creator gets a plain `waiting` code match with **no error**,
  and the bogus name is still stored in `player2_username`. A later join by
  code updates `player2_id`/`player2_elo`/`status` but never refreshes
  `player2_username`, so the stale name survives into the active match.
- **Challenge username lookup is case-sensitive** (exact Mongo `find_one`):
  `"beekeeper"` does not match `"BeeKeeper"` and silently falls back to an
  open waiting match instead of a pending challenge.
- **Pending challenges cannot be joined by code** — not even by the invited
  player, who must use `/api/challenges/accept`; the code path answers
  `400 "Match already started"`.
- **Codes are upper-cased but never trimmed**: a pasted code with surrounding
  whitespace 404s.
- **The unauthenticated poller** `/api/game/friend/status/{code}` exposes
  exactly `{match_id, status, player1_ready, player2_ready}` for any code,
  waiting or active; for a pending challenge both ready flags are `True`
  while status is still `pending`.
- **`/api/game/match/{code}` on a waiting match returns the string `"None"`**
  for `player2_id` (`str(None)`), which clients must special-case. Guest
  opponents are labelled `"Guest"`; only players in the match may call it
  (outsiders get 403).

## Challenges (`/api/challenges/pending|accept/{id}|cancel/{id}`)

Findings from `tests/test_challenge_match_edge_cases.py` (30 tests: 29 pass,
1 strict `xfail`). Because all three challenge endpoints read only from the
DB, most tests run against an in-process fake of `matches_collection`;
dedicated tests also pin what happens when the DB misses.

### Bugs (xfail or explicitly demonstrated)

1. **Self-challenge is allowed** — `test_challenge_to_self_should_be_rejected`
   (xfail). Creating a match with your own username yields a pending
   challenge where `player1_id == player2_id`; it appears in your own pending
   list and you can accept it (`test_challenge_to_self_is_allowed_and_self_acceptable`).
   The join path explicitly rejects joining your own match; create does not.

2. **Pending challenges are playable without accepting** —
   `test_unaccepted_challenge_is_already_playable`. Gameplay routes
   (`/api/game/question`, `/api/game/answer`) only reject `completed`
   matches, never `pending` ones, so both parties can fetch questions and
   score points on a challenge that was never accepted, bypassing the accept
   step entirely.

3. **No in-memory fallback (inconsistent with the friend endpoints)** —
   `test_pending_listing_misses_memory_only_challenges` and
   `test_accept_misses_memory_only_challenge`. `join_friend_match` and
   `get_match_status` fall back to `in_memory_matches` on a DB miss, but
   `get_pending_challenges` / `accept_challenge` / `cancel_challenge` query
   the DB only. With the DB unavailable, a pending challenge that exists in
   memory is invisible to the invitee and 404s on accept — even though the
   same match *can* still be played (bug 2) and polled by code.

### Quirks / current behavior worth knowing (asserted in passing tests)

- **Accept/cancel authorization is correct**: only the invitee (exact
  `player2_id`) may accept (403 otherwise, including the creator), and only
  the creator may cancel (403 for the invitee and outsiders).
- **Cancel deletes the document**, so a late accept — or a second cancel —
  gets `404 "Challenge not found"` rather than a "was cancelled" message.
- **Cancel doubles as "delete my unshared friend match"**: it accepts status
  `waiting` as well as `pending`, and the code is dead (404) afterwards.
- **Accepting twice** returns `400 "Challenge already accepted or expired"`;
  the same 400 covers `abandoned` and `completed` challenges.
- **Open (code-only) matches cannot be hijacked via accept**: `player2_id` is
  `None`, so everyone gets 403.
- **Pending list is hard-capped at 10** (`to_list(length=10)`); an invitee
  with 12 pending challenges silently sees only 10, and there is **no
  dedupe** — the same challenger can stack unlimited identical challenges
  (spam vector).
- **Challenger display name** falls back to the guest display name
  (`"Guest <id-suffix>"`) because guest identities have no `username`.
- **`tests/conftest.py` does not stub `delete_one`**, so any test that
  reaches `cancel_challenge` with the stock mocks would hit a real Motor
  call; the fake collection in the challenge test file covers it.

## Answer submission, scoring and win conditions (`/api/game/answer`, `/api/game/give-up`, first-to-3)

Findings from `tests/test_match_answer_and_scoring_edge_cases.py` (67 tests:
66 pass, 1 strict `xfail` documenting a real bug). Friend matches are used
for controlled 1v1; the ranked queue and the bot fallback are exercised where
the code paths differ.

### Bugs (xfail or explicitly demonstrated)

1. **Double-score race when the round is re-read from the DB** —
   `test_concurrent_correct_answers_via_db_reload_only_one_scores` (xfail).
   `submit_answer` takes no match lock (unlike `get_question`). When the
   round doc is in `in_memory_rounds` the winner check and winner write run
   with no await between them, so concurrent correct answers serialize and
   only one player scores (pinned by
   `test_concurrent_correct_answers_in_memory_only_one_scores`). But on a
   memory miss the round is loaded via `await rounds_collection.find_one`,
   and each request gets its own copy of the doc. Two players answering
   correctly at the same time both pass the `winner_id` check on their
   private copies, both are declared round winner, and **one round pays out
   a point to each player (1-1)**, with both responses claiming
   `correct: true` and `round_winner: <self>`. Current behavior is pinned by
   `test_current_behavior_db_reload_race_double_scores_one_round`. Any
   multi-worker deployment (or cache eviction/restart) hits this path.

### Quirks / current behavior worth knowing (asserted in passing tests)

- **`already_won` responses report `correct: false` unconditionally**, even
  when the late submission is mathematically right — the answer is never
  graded once the round has a winner. Clients must key off `already_won`,
  not `correct`.
- **Giving up does not lock a player out of the round.** Until the opponent
  also gives up, the quitter can still submit a correct answer and win the
  round (`test_player_who_gave_up_can_still_answer_and_win_round`). A lone
  give-up returns `{"status": "gave_up", "waiting_for_opponent": true}` and
  the round stays open; both giving up ties the round (`winner_id: "tie"`,
  no score change) and the next `/question` starts a fresh round. If the
  opponent's presence heartbeat is stale (>12s), a lone give-up auto-ties.
- **A finished round stays "current" until someone GETs `/question` again**;
  answers in that window get `already_won` echoes rather than starting the
  next round. Round ids progress deterministically
  (`round-<match>-1,2,3…`) with `round_number` incrementing.
- **Boolean answers slip through validation.** `AnswerSubmit.answer` is
  `Union[str, float]`, so JSON `true` is coerced to `1.0` and graded as an
  answer (wrong), while `null`, arrays and objects are 422s. Empty and
  whitespace-only strings reach SymPy, raise inside the try/except, and are
  graded wrong (no 500). Same for 5000-char garbage and exotic unicode
  (fullwidth `２ｘ`, math-alphanumeric `𝟐𝐱`, `٢x`).
- **Unicode operator handling is inconsistent with the daily-challenge
  checker.** `submit_answer`'s inline preprocess maps `·` but not `×`, while
  the standalone `check_math_equivalence` (used elsewhere) maps both — so
  `2×x` is wrong in PvP but would pass a daily challenge.
- **Equivalence grading is generous**: `2*x`, `2x`, `x+x`, `2 x`, `x*2`,
  `2·x`, `2.0*x`, `4*x/2`, `2*x + 0` and `(2)(x)` are all accepted for
  `2·x`; near-misses (`2`, `x`, `-2*x`, `2*x + 1`, `x^2`) are rejected. The
  numeric-expected branch compares with `abs(diff) < 0.1` tolerance.
- **Waiting (unjoined) matches don't block answers by status** — the creator
  just gets `404 "No active round"`, and the same 404 covers an active match
  where nobody requested a question yet. Unknown match ids 404, outsiders
  403 (their "correct" answers never score), completed matches 400 on both
  `/answer` and `/question`.
- **Win conditions behave**: first to 3 completes the match at exactly 3-0,
  3-1 or 3-2 (2-2 keeps it active), symmetrically for player1 and player2,
  freezing scores and setting `status: "completed"` + `winner_id`, which the
  status poller reflects.
- **ELO only moves for `match_type` in `{"random", "ranked"}`**: friend-match
  completion writes `elo_change: 0` and touches no user docs; ranked
  completion applies `calculate_elo_change` symmetrically (+elo/+1 win,
  −elo/+1 loss). Mid-match round wins never move ELO.
- **Bot rounds are timed; human rounds are not.** Exceeding the bot round's
  `time_limit` forfeits the round to the bot even if the submitted answer is
  correct (`already_won: true`, `message: "Time limit exceeded"`); three
  timeouts lose the match, set `winner_id: "bot-opponent"`, and deduct ELO
  from the human (loss recorded), after which further answers are 400.
