# Match Edge Case Campaign Report

## Executive summary

### What was tested

Every people-vs-people match flow in the backend: ranked matchmaking
(queue, cancel, reconnect, bot fallback), friend matches (create/join by
code, challenges to a named user), the challenge accept/cancel endpoints,
presence tracking and give-up resolution, question serving and round
progression, answer grading and first-to-3 win conditions, ELO calculation
and payout, datetime/timezone handling, in-memory-vs-Mongo state
divergence, and cross-match isolation / access control, plus dedicated
suites for the bot fallback / bot round timing path, the cancel-flag /
queue lifecycle, the HTTP API contract (response-key shapes, status-code
matrix, content-type/method negotiation, injection/long-input hardening),
and PvP math-equivalence grading (equivalent/inequivalent answer forms,
unicode operators, numeric tolerance, and SymPy crash/DoS inputs).
A dedicated multiplayer stress / sequential-chaos suite simulates many
people matching at once: 10-guest queue waves, parallel start spam from one
user, cancel/start churn, friend-match create spam with random joins, full
first-to-3 matches played end-to-end, parallel ranked matches, immediate
rematch cycles, challenge spam against the pending-list cap, give-up
storms, status-polling storms, friend+ranked overlap for one pair, and
hour-stale queue entries. Concurrency races, DB-unavailable fallbacks,
cache eviction/restart scenarios, and malformed input were exercised
throughout.

### Test inventory

25 dedicated edge-case suites, **1124 edge-case tests collected**.
Full repository suite: **1160 collected → 1097 passed, 63 xfailed**.
Every xfail is strict and pins a real bug documented below.

| File | Tests | xfail markers |
|---|---|---|
| `tests/test_bot_fallback_and_timeout_edge_cases.py` | 28 | 1 |
| `tests/test_challenge_match_edge_cases.py` | 31 | 1 |
| `tests/test_elo_and_match_completion_edge_cases.py` | 49 | 2 |
| `tests/test_friend_match_edge_cases.py` | 44 | 2 |
| `tests/test_match_answer_and_scoring_edge_cases.py` | 67 | 1 |
| `tests/test_match_api_contract_edge_cases.py` | 80 | 1 |
| `tests/test_match_audit_pass_2_edge_cases.py` | 16 | 4 |
| `tests/test_match_auth_identity_edge_cases.py` | 37 | 0 |
| `tests/test_match_cancel_and_queue_lifecycle_edge_cases.py` | 25 | 4 |
| `tests/test_match_datetime_and_memory_edge_cases.py` | 46 | 3 |
| `tests/test_match_giveup_status_edge_cases.py` | 56 | 2 |
| `tests/test_match_history_and_listing_edge_cases.py` | 33 | 6 |
| `tests/test_match_isolation_and_access_edge_cases.py` | 38 | 2 |
| `tests/test_match_math_equivalence_in_pvp_edge_cases.py` | 117 | 3 |
| `tests/test_match_misc_people_edge_cases.py` | 24 | 0 |
| `tests/test_match_mongo_hydrate_edge_cases.py` | 26 | 2 |
| `tests/test_match_multiplayer_stress_edge_cases.py` | 54 | 4 |
| `tests/test_match_newly_found_bugs_edge_cases.py` | 12 | 6 |
| `tests/test_match_objectid_guest_mixing_edge_cases.py` | 35 | 2 |
| `tests/test_match_presence_and_lifecycle_edge_cases.py` | 68 | 1 |
| `tests/test_match_question_and_round_edge_cases.py` | 68 | 2 |
| `tests/test_match_reconnect_abandon_edge_cases.py` | 35 | 1 |
| `tests/test_match_status_gate_edge_cases.py` | 58 | 3 |
| `tests/test_match_win_completion_edge_cases.py` | 33 | 1 |
| `tests/test_ranked_matchmaking_edge_cases.py` | 44 | 2 |
| **Edge-case total** | **1124** | **56** |

The full repository suite collects 1160 tests and runs as 1097 passed, 63 xfailed.

### Severity-ranked bug list

Consolidated from the detailed sections below. "xfail" bugs are pinned by
a strict expected-failure test; the rest are explicitly demonstrated by
passing tests that assert the broken behavior.

**P0 — security or data integrity, exploitable/likely in production**

1. **`/match/{match_id}/details` is a live answer oracle with no
   authentication or authorization** (xfail, isolation suite). Anyone with
   a match id — including the opponent in a second tab — can read the
   correct answer of the still-unresolved round mid-game. The response
   also leaks the match code, unlocking anonymous status polling.
2. **Double-score race when a round is re-read from the DB** (xfail,
   answer/scoring suite). `submit_answer` takes no match lock; on a
   round-cache miss two concurrent correct answers both win the round and
   one round pays a point to each player. Any multi-worker deployment or
   cache eviction hits this path. The win-completion suite pins the
   worst case (xfail `BUG(match-point-db-race-double-elo)`): at 2-2 the
   same race double-completes the match — 3-3 scoreline, ELO `$inc`'d
   twice for both players, `completed` written twice, and both
   completions credit player1.
3. **`match_counter` restart collision overwrites live matches** (xfail,
   datetime/memory suite). Ranked ids are `match-{counter}`; after a
   process restart the counter resets, `match-1` is re-issued, and the
   still-live original match is replaced — its players get 403 on their
   own match.

**P1 — gameplay correctness bugs users will hit**

4. **Concurrent friend-match join race** (friend suite). No per-match
   lock in `join_friend_match`: with any DB latency two players both get
   a 200 and the second silently overwrites the first joiner.
5. **Double-pairing race for registered ranked opponents** (xfail, ranked
   suite). An `await` between selecting and popping a queued ObjectId
   player lets two concurrent callers each match the same person.
6. **Stale cancel flag poisons the next pairing** (xfail, ranked suite).
   Re-queueing after `/api/game/cancel` never clears `cancelled_users`;
   the next opponent to pair with that user gets a spurious
   `"cancelled"` and the user is silently dropped from the queue. The bot
   and cancel suites pin three more victims of the same flag lifecycle
   (each its own xfail): the flag also **eats the 10-second bot fallback**
   (bot suite), it is planted by a **stray cancel from a user who was
   never queued** and aborts their first-ever pairing (cancel suite), and
   it **survives an entire completed match** to abort the user's next
   pairing in a fresh session (cancel suite). The multiplayer stress suite
   adds a population-level xfail: after rapid cancel/start churn by 5
   users, a full mass re-queue forms **zero** matches — every pairing
   attempt is eaten pairwise by leftover flags, and it takes two more mass
   passes before anyone actually matches.
7. **Queueing for ranked abandons or hijacks an active friend match**
   (xfails, isolation and multiplayer stress suites). The stale-match scan
   ignores `match_type`, so "play ranked" reconnects into (or abandons) a
   live friend match. The stress suite pins the pair-level fallout: when
   **both** players of a fresh friend match tap "play ranked", both are
   hijacked back into the friend match, neither enters the queue, and no
   ranked match can ever form for that pair.
8. **Abandoned "zombie" matches remain fully playable** (xfail, presence
   suite). Gameplay routes only reject `completed`, so abandoned matches
   keep serving rounds and accepting scores.
9. **Pending challenges are playable without being accepted** (challenge
   suite). Both parties can fetch questions and score on a challenge
   nobody accepted, bypassing the accept step.
10. **Tie rounds desync the Mongo rounds-array numbering** (xfail,
    question/round suite). Round docs count rounds; array summaries count
    scores. Any tie makes the numbering diverge permanently, and
    subsequent positional updates hit the wrong (or no) array entry.
11. **Round ids are reused after round-cache loss / memory wipe** (two
    xfails, question/round and datetime/memory suites — one root cause).
    Round numbering derives from `in_memory_rounds` only, so a restart or
    eviction re-issues `round-<match>-1`, silently diverging memory and
    Mongo under one round id.
12. **Aware (or ISO-string) `created_at` turns `/api/game/start` into a
    500** (xfail, datetime/memory suite). The reconnect window subtracts
    naive `datetime.utcnow()` with no `ensure_utc`, so any tz-aware
    migration breaks the start endpoint for that player.
13. **Friend match-code uniqueness ignores in-memory matches** (xfail,
    friend suite). With the DB empty or down, two live matches can share
    a code and the second becomes unreachable.
14. **Racing `cancel_challenge` vs `join` lets both succeed** (xfail,
    cancel suite). Neither endpoint takes the per-match lock; with any DB
    read latency the join returns 200 and the cancel then deletes the
    now-active match from DB and memory, leaving the acknowledged joiner
    holding a match id that 404s on every subsequent request.
15. **Queue entries never expire without polling** (cancel suite; now also
    xfail in the multiplayer stress suite). The 10s bot deadline is only
    evaluated when the queued user polls, so an hour-gone user is still
    matchable into a **ghost match**; because the ghost never polled,
    presence reports them connected forever and the live player can't even
    get a give-up auto-tie. Pinned as a strict xfail by
    `test_hour_stale_queue_entry_should_not_be_matchable`, plus stress
    tests showing the first of five fresh arrivals is the one sacrificed
    to the ghost.

**P1 — gameplay correctness bugs users will hit** (continued)

24. **Unbounded integer power hangs answer grading (DoS)** (two xfails, math
    suite). `submit_answer` maps `^`→`**` and calls `parse_expr(...,
    evaluate=True)` with no timeout or size guard. A single answer of
    `9**9**9` forces Python to materialize `9**387420489` — an integer with
    ~370 million digits — so the request never returns and pins the worker's
    CPU/memory. Any player can wedge a match (and, on a single worker, the
    whole server) with one POST. Pinned by
    `test_unbounded_integer_power_answer_does_not_hang_grading`, which runs
    grading in a watchdog subprocess and fails because it never finishes.
    The same wedge has a **second entry point**: the grader's numeric
    fallback substitutes a random point from `uniform(1, 10)` into the
    user's expression and evaluates it with `N()`, so a symbolic power
    tower like `x**x**x**x**x` — which sails through the whole symbolic
    cascade — hangs mpmath whenever the sample point lands above ~2
    (roughly 85% of requests; the rest grade wrong normally). Pinned by
    `test_power_tower_should_not_hang_numeric_fallback_at_large_sample_points`
    (xfail, sample point pinned to 9.0) with a deterministic passing
    companion at a pinned small sample point. This probabilistic variant
    was discovered when the tower input intermittently hung the full-suite
    run itself.

**P2 — robustness, consistency and policy gaps**

16. **User ELO can go negative** (xfail, ELO suite) — the loser `$inc`
    has no floor.
17. **`calculate_elo_change` crashes (`OverflowError`) on extreme rating
    gaps** (xfail, ELO suite) — no input guard; corrupted ratings turn
    answer submission into a 500.
18. **Match-code case-sensitivity is inconsistent** (xfail, friend
    suite). `/api/game/match/{code}` compares case-sensitively while the
    friend endpoints upper-case; the two code namespaces disagree.
19. **Self-challenge is allowed** (xfail, challenge suite) — a user can
    create and accept a challenge against themselves.
20. **Challenge endpoints have no in-memory fallback** (challenge suite;
    now also xfail in the cancel suite for `cancel_challenge`). With the
    DB down, a memory-held pending challenge is invisible to
    `pending`/`accept`/`cancel` yet still playable (see bug 9), and a
    memory-only waiting friend match is joinable by code but its creator
    cannot cancel it (404).
21. **Broad unauthenticated exposure**: `/matches/all` returns the last
    50 matches of all players to any caller, and
    `/api/game/friend/status/{code}` polls any match anonymously.
22. **Unbounded in-memory leaks around cancel** (cancel suite).
    `cancelled_users` grows forever (no TTL/cap; entries are only removed
    by a later pairing involving that user), and cancelling a waiting
    match whose creator already fetched a question orphans the round in
    `in_memory_rounds` and the lock in `match_locks` permanently.
23. **`is_opponent_bot` uses a substring check** (bot suite).
    `/match/{code}` labels any opponent whose id contains `"bot"` (e.g.
    `guest-abbot-1234`) as a bot, while gameplay correctly treats them as
    human; the real sentinel is `player2_id == "bot-opponent"`.
25. **Unicode-operator handling diverges between the two graders** (xfail,
    math suite). `submit_answer`'s inline preprocess maps only the middle
    dot `·` to `*`, so the multiplication sign `2×x` (and the asterisk
    operator `2∗x`) is graded **wrong** in PvP — yet the standalone
    `check_math_equivalence` (used by the daily-challenge path) maps both
    `·` and `×`, so the identical answer passes there. Same keystroke, two
    verdicts. Pinned by `test_times_sign_should_be_accepted_in_pvp` plus a
    passing test showing `check_math_equivalence("2·x", "2×x")` is `True`.
26. **No 401 on the people-match routes** (xfail, API-contract suite; same
    root cause as bug 1 / bug 21). `get_current_user` silently falls back
    to a shared guest identity for missing, empty, malformed or wrong-scheme
    credentials, so every state-changing route (`friend/create`, `start`,
    `answer`, …) returns 200 to a fully anonymous caller instead of
    challenging with 401. Pinned by `test_anonymous_state_change_should_be_401`.
27. **Pending-challenge list silently truncates at 10 with no dedupe or
    cap on creation** (xfail, multiplayer stress suite). One challenger
    can stack unlimited identical pending challenges (12 verified, all
    stored), but `get_pending_challenges` is a bare `to_list(length=10)`
    with no paging, count or dedupe — the invitee sees exactly the 10
    oldest and has no way to know (or clear) the hidden ones, which stay
    live in the DB and playable (bug 9). Pinned by
    `test_pending_list_should_expose_all_twelve_spam_challenges`, with
    passing pins for the spam storage, the cap, and hidden challenges
    scrolling into view as older ones are accepted/cancelled.
28. **A corrupted Mongo doc poisons the memory cache before it is ever
    validated** (hydrate suite, demonstrated by passing pins). Every
    hydrate path caches the DB doc into `in_memory_matches` /
    `in_memory_rounds` *first* and reads its fields afterwards, so a doc
    missing a load-bearing field (`player2_id`, `player1_elo`, scores,
    `status`, a round's `answer`) 500s the request **and** leaves the
    broken doc cached — every later request re-crashes off the cached
    copy and the DB (which may have been repaired) is never re-read
    until a restart.
29. **A mid-`question` crash strands a half-created round** (hydrate
    suite, demonstrated by a passing pin). `_create_next_round` stores
    the round in memory and inserts it into `rounds_collection` *before*
    reading the match's scores for the summary `$push`; on a match doc
    missing `player1_score` the request 500s after the insert, leaving a
    fully persisted round that the match doc never references.

### Recommended fix order

1. **Lock down `/match/{id}/details`** (bug 1): require auth + participant
   check, and strip unresolved-round answers from the payload. Smallest
   change, biggest exploit closed.
2. **Serialize writes with the existing per-match lock** (bugs 2, 4, 5,
   14): `submit_answer`, `join_friend_match` and `cancel_challenge`
   should take `get_match_lock` like `get_question` already does; move
   the ranked opponent pop before the `find_one` await (or re-check
   after it).
3. **Fix id generation** (bugs 3, 11): replace `match_counter` with a
   collision-free id (UUID/ObjectId) and derive round numbers from the
   match doc or Mongo, not from `in_memory_rounds`.
4. **Fix lifecycle/status gating** (bugs 6, 7, 8, 9, 15): clear the
   user's `cancelled_users` flag whenever they (re-)queue or a match is
   created for them, only accept cancels from actually-queued users,
   expire stale queue entries, filter the stale-match scan by
   `match_type`, and have gameplay routes reject `abandoned` and
   `pending` matches, not just `completed`.
5. **Fix round-array numbering** (bug 10): number the `$push`ed summary
   with the same `round_count + 1` used for the round doc.
6. **Harden datetime handling** (bug 12): run `created_at` through
   `ensure_utc`/`parse_round_start` in the reconnect window.
7. **Bound the answer grader** (bug 24): before `parse_expr`, reject or
   cap unevaluated integer powers (or grade with `evaluate=False` /
   `Pow(..., evaluate=False)` + a numeric comparison, or run grading with a
   wall-clock timeout). The bound must also cover the numeric fallback's
   `subs`/`N()` evaluation — a symbolic power tower reaches the same hang
   through that path. This is a one-request DoS and should jump the queue.
8. **Unify the two graders** (bug 25): have `submit_answer` reuse
   `check_math_equivalence` (or at least share the same unicode-normalization
   table) so `×`/`∗`/`·` behave identically in PvP and daily challenges.
9. **Sweep the rest** (bugs 13, 16–23, 26, 27): in-memory code-collision
   check, ELO floor and overflow guard, code-case normalization,
   self-challenge rejection, challenge memory fallback (including
   `cancel_challenge`), an access review of `/matches/all` and the
   anonymous status poller, TTL/cleanup for `cancelled_users` and
   cancel-orphaned rounds/locks, replacing the `is_opponent_bot` substring
   check with the `"bot-opponent"` sentinel, adding real authentication so
   the people-match routes return 401 instead of guest-fallback 200, and
   deduping / paging the pending-challenge list (plus a per-user cap on
   open friend matches and pending challenges).

### How to run the tests

```bash
# Full suite (1160 tests: 1097 pass, 63 xfail)
python3 -m pytest tests/ -q

# Edge-case campaign only (1124 tests: 1061 pass, 63 xfail)
python3 -m pytest tests/test_*edge_cases*.py -q

# Verify the inventory
python3 -m pytest --collect-only -q tests/
```

Note: `test_match_math_equivalence_in_pvp_edge_cases.py` includes two
DoS-detection tests that grade an answer inside a watchdog **subprocess**
(spawn context) so a hang in the SymPy grader cannot wedge the whole run;
they add ~25s (two 12s watchdog timeouts plus the harness sanity check) to
that file. The rest of the suite is sub-second.

All xfails are `strict`, so a fixed bug will surface as `XPASS` and fail
the run — flip the corresponding test to a plain assertion when fixing.

The detailed per-area findings follow.

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

## Presence & match lifecycle (`/api/game/status|active|give-up`, `mark_player_seen`, `is_player_connected`)

Findings from `tests/test_match_presence_and_lifecycle_edge_cases.py`
(68 tests: 67 pass, 1 strict `xfail`). Presence boundaries were tested by
freezing `main.utc_now`; endpoint-level tests backdate `player_last_seen`
entries directly on the in-memory match doc.

### Bugs (xfail or explicitly demonstrated)

1. **Abandoned matches remain fully playable ("zombie" matches)** —
   `test_abandoned_match_should_not_serve_questions` (xfail).
   Gameplay routes (`/api/game/question`, `/api/game/answer`) only reject
   status `completed`. A match marked `abandoned` (e.g. by the reconnect
   window in `start_match`) keeps serving new rounds, accepting answers and
   incrementing scores, even though `/api/game/active` correctly hides it.
   Current behavior pinned by
   `test_current_behavior_abandoned_match_still_serves_questions`.

### Presence semantics (asserted in passing tests)

- **Boundary is inclusive at exactly 12s**: `is_player_connected` uses
  `<= PRESENCE_TIMEOUT_SECONDS`, so 11.9s ago (and 12.000s exactly) is
  connected; 12.000001s / 12.1s ago is disconnected.
- **Bot is always connected** (`"bot-opponent"` short-circuits before any
  timestamp lookup) — even a poisoned 9999s-stale heartbeat entry for the
  bot is ignored.
- **Never-seen players count as connected** (`last_seen is None -> True`).
  Consequence: on a `waiting` friend match the *nonexistent* opponent is
  reported `opponent_connected: true` (and `player2_id` is the string
  `"None"`), and a give-up against an opponent who never polled waits
  forever instead of auto-resolving.
- **Every gameplay call is a heartbeat**: `status`, `question`, `answer` and
  `give-up` all call `mark_player_seen` for the caller only. Keys are
  stringified, so ObjectId and string forms resolve to the same entry. Naive
  (Mongo round-trip) timestamps are treated as UTC by `ensure_utc`.
- **Presence is per-player and one-directional**: a player whose own
  heartbeat is stale still sees a fresh opponent as connected, while the
  opponent sees them as gone.
- **Presence never reaches the DB.** `mark_player_seen` mutates only the
  in-memory doc; if the match is evicted and reloaded from Mongo
  (`get_game_status` caches it back), all heartbeat history is lost and a
  long-gone opponent flips back to "connected".
- **Outsiders (403) do not pollute the presence map** — the membership check
  runs before `mark_player_seen` on all routes.

### Give-up / stale-opponent resolution

- With a **connected** (or never-seen) opponent, a solo give-up returns
  `{"status": "gave_up", "waiting_for_opponent": true}` and the round stays
  open.
- With a **stale** opponent (>12s since last poll), the give-up is
  auto-mirrored: both `*_gave_up` flags are set, the round resolves to
  `winner_id: "tie"`, and `both_gave_up` is returned — in both friend and
  ranked matches.
- **A stale-opponent tie awards no points**, so a walked-away opponent can
  never be beaten this way: the remaining player can only burn rounds to
  ties; the match stays `active` indefinitely (no abandonment from presence).
- After a tie, the next `/api/game/question` creates a fresh round
  (deterministic id `round-{match_id}-{n}`); a returning opponent shares
  that round and can win it normally.
- `give-up` on a round that already has a winner returns `already_ended`;
  with no round at all it 404s (`"No active round"`). It never checks match
  status, so it still answers `already_ended` on a **completed** match.

### Lifecycle / status endpoint

- `/api/game/status/{id}` returns identical board state for both players —
  player1/player2 slots never flip per caller; `opponent_connected` is the
  only per-caller field. Note the ranked slot assignment: the *joining*
  poller becomes `player1`, the queued player becomes `player2`.
- Completed matches: status shows `completed` + `winner_id` to both players;
  `question`/`answer` are rejected with 400; presence is still tracked and
  reported on the completed match.
- `/api/game/active` skips `waiting`, `abandoned` and `completed` matches
  for both players (only `active` counts), and labels friend matches with
  `match_type: "friend"`.
- Reconnect window vs presence: reconnection through `<5s /api/game/start`
  preserves the `player_last_seen` map, and a stale opponent heartbeat does
  **not** cause abandonment — the two mechanisms are fully independent.
  After the window, abandonment flips the status while keeping presence
  history; the other player only learns via the status poll.
- `cancel_challenge` works as "delete my waiting friend match": the document
  is deleted, the code 404s on join/status polls afterwards, non-creators
  get 403, and an `active` match can no longer be cancelled (400).
- `/matches/all` reads exclusively from the DB — in-memory matches are
  invisible to it (returns `[]` with the DB mocked empty), unlike
  `/match/{id}/details` which falls back to memory.

## ELO & match completion (`calculate_elo_change`, `/api/game/answer` completion path)

Findings from `tests/test_elo_and_match_completion_edge_cases.py`
(49 tests: 47 pass, 2 strict `xfail`). User-document writes were captured
with an in-process fake `users_collection` that applies `$inc`/`$set` and
logs every call.

### Bugs (xfail tests)

1. **User ELO can go negative** — `test_user_elo_should_not_go_negative`
   (xfail). The loser update is a raw `$inc {"elo": -elo_change}` with no
   floor, and nothing anywhere clamps ratings, so a user whose live rating
   is lower than the computed change (e.g. 5 - 20) ends up with negative
   ELO. Pinned by `test_current_behavior_user_elo_goes_negative`.

2. **`calculate_elo_change` crashes on extreme underdog gaps** —
   `test_extreme_underdog_gap_should_cap_at_k_not_crash` (xfail, raises
   `OverflowError`). `10 ** ((loser_elo - winner_elo) / 400)` overflows the
   float range once the gap exceeds ~123,600 points (exponent > 308).
   Unreachable through normal play, but there is no input guard, so
   corrupted/synthetic ratings turn answer submission into a 500. The
   favorite direction is safe (underflows to 0.0 → change 1). Pinned by
   `test_current_behavior_extreme_underdog_gap_raises_overflow`.

### `calculate_elo_change` facts (asserted in passing tests)

- Even matchups pay exactly K/2: 20 / 16 / 12 for the three brackets.
- K-factor boundaries are winner-only and exclusive: 1199→K40, 1200→K32,
  1799→K32, 1800→K24. Because only the **winner's** rating picks K, a
  1000-rated winner moves a 1900-rated loser by 40 while the reverse pairing
  pays 1 — asymmetric transfers by design (or accident).
- Upsets pay more, favorites less: +400 upset = 36 (K40), −400 favorite = 3
  (K32); extreme upsets cap at exactly K; extreme favorites floor at
  `max(1, ...)` — the change is always an int in `[1, K]` (verified over a
  0–4000 grid).
- Zero and negative inputs are handled by the formula itself:
  `(0,0) → 20`, `(0,400) → 36`, `(-400,0) → 36`, `(-1000,-1000) → 20`.

### Completion behavior (asserted in passing tests)

- **First-to-3 ends the match** (`>= 3`, so corrupted scores past 3 also
  complete); the winning answer response carries
  `match_winner` + `elo_change`, the match doc flips to `completed` with
  `winner_id`/`elo_change` set, and both `/api/game/status` and
  `/match/{id}/details` report the same `elo_change` to both players.
- **ELO is never applied mid-match**: round wins, wrong answers and tie
  rounds (even at 2–2 match point) produce `elo_change: 0` and zero
  `users_collection` writes; only the completing answer triggers exactly two
  `$inc` calls — winner `{elo: +c, wins: +1}`, loser `{elo: -c, losses: +1}`
  (zero-sum, mirrored exactly).
- **No double application**: further answers after completion get
  `400 "Match is already completed"` and write nothing; status polls never
  re-touch user docs.
- **Snapshots, not live ratings**: the change is computed from
  `player1_elo`/`player2_elo` captured at match creation. A live user doc
  that diverged to 5000 still pays the 1000-vs-1000 snapshot amount (20),
  which is then `$inc`-applied on top of the diverged live rating. Mutating
  the snapshots moves the payout bracket (2000/2000 → 12; 1000-vs-1400
  upset → 36).
- **Friend matches are completely unranked**: the `$inc` block only runs for
  `match_type` in `{"random", "ranked"}`, so friend completion pays
  `elo_change: 0` and does not even count wins/losses.
- **Guest ranked matches "pay" phantom ELO**: completion issues both `$inc`
  updates against user ids that have no documents, so the change shown to
  the players is persisted nowhere except the match doc. Relatedly, the
  queued guest's snapshot comes from the hardcoded `{"elo": 1000}` fallback,
  ignoring the queue entry's recorded ELO.

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

## Question serving & round progression (`/api/game/question`, `_create_next_round`, `_question_response`)

Findings from `tests/test_match_question_and_round_edge_cases.py` (68 tests:
66 pass, 2 strict `xfail` documenting real bugs). Difficulty selection was
observed with a `generate_question` spy that records the ELO argument; the
Mongo `rounds` array bookkeeping was observed by recording every
`matches_collection.update_one` call. Concurrency was exercised by calling
`get_question` directly under `asyncio.gather` (with a fresh per-match lock
for the test's event loop).

### Bugs (xfail tests)

1. **Tie rounds desync the Mongo rounds-array numbering** —
   `test_round_summary_number_should_match_round_doc_after_tie` (xfail).
   `_create_next_round` numbers the round *doc* by counting existing rounds
   (`round_count + 1`) but numbers the summary it `$push`es into the match's
   `rounds` array by `player1_score + player2_score + 1`. Wins keep the two
   in sync; **any tie** (double give-up or 5-minute timeout) leaves the
   scores unchanged, so the next round doc is numbered N+1 while its Mongo
   summary repeats number N. From then on the array holds duplicate
   `round_number` entries, and every winner/tie update that filters on
   `{"rounds.round_number": ...}` positionally hits the wrong entry — or,
   for the freshly created round's real number, **no entry at all** (pinned
   by `test_current_behavior_tie_desyncs_mongo_round_numbers`: after one tie
   the array is `[1, 1]` while the live round is number 2, and round 2's tie
   update targets a `round_number: 2` that no array element has).

2. **Round ids are reused after round-cache loss** —
   `test_round_ids_should_stay_unique_after_round_cache_loss` (xfail).
   The "deterministic" id `round-{match_id}-{n}` derives `n` from the number
   of that match's rounds currently in `in_memory_rounds`. If the rounds
   cache is lost while the match survives (restart, eviction, another
   worker), the count restarts at zero and the next question reissues
   `round-<match>-1`: the original round's history is overwritten in memory
   (its `winner_id` is forgotten, though the score it paid survives on the
   match), and because a round with that `_id` already exists in Mongo the
   insert is skipped — the DB keeps the OLD question while players are shown
   the new one. Pinned by
   `test_current_behavior_round_cache_loss_reuses_round_one_id`.

### Core behavior verified (passing tests)

- **Difficulty always uses the lower of the two ELO snapshots**
  (`min(player1_elo, player2_elo)`), whichever side is weaker and however
  wide the gap (2500 vs 800 → questions for 800), so both players see the
  same question at the weaker player's level. With the real generator,
  1000/1000 pairs get difficulty 1–2 (`elo < 1200` branch) and 2000+ pairs
  get difficulty 3.
- **Resume semantics**: while the current round has no `winner_id`, every
  `/question` call from either player returns the identical payload
  (`round_id`, `expression`, `evaluate_at`, `round_start_time`); wrong
  answers don't advance anything. A new round is created only after a
  winner or tie, with `round_number` incrementing 1, 2, 3… (ties included)
  and per-match numbering fully independent across concurrent matches.
- **First-to-3 lifecycle**: three won rounds produce three unique
  sequential round ids, flip the match to `completed`, and the fourth
  `/question` is rejected with `400 "Match is already completed"`.
- **Concurrency is safe on one worker**: simultaneous first-question
  requests, simultaneous next-question requests right after a win, and
  duplicate requests from the same player all resolve to a single shared
  round under the per-match lock (exactly one round created, both players
  on the same `round_id`).
- **Response shape**: exactly
  `{round_id, expression, evaluate_at, ask_for_derivative_only,
  round_start_time}` (+ `time_limit` for bot rounds); the answer/derivative
  never leak. `ask_for_derivative_only` is always present — defaulted to
  `True` if the generator omits it, passed through when `False`.
  `round_start_time` is a timezone-aware UTC ISO string aimed ~3s into the
  future; the resume path echoes the exact same string.
- **Stale-round timeout**: a round older than 5 minutes is marked
  `winner_id: "tie"` (no points) and a fresh round is created on the next
  poll; 299s-old rounds are still served; ISO-string `created_at` (Mongo
  round-trip) is parsed correctly; an unparseable `created_at` never times
  out (round is resumed, no crash).

### Bot vs human differences (case-by-case)

- Only bot matches (`match_type == "random"` **and**
  `player2_id == "bot-opponent"`) get `time_limit`; friend and ranked
  human rounds omit the key entirely (absent, not `null`).
- `time_limit = base(player1_elo) + difficulty`, brackets inclusive:
  ≤1000 → 15, ≤1400 → 12, ≤1800 → 10, >1800 → 8 (+1s per difficulty
  level). The resume path carries the same `time_limit`.
- **Two different ELOs feed one bot round**: difficulty uses
  `min(elo)` = the bot's (spawned 50–150 below the user), while
  `time_limit` uses the *user's* ELO. Otherwise the bot path shares the
  exact creation path (same deterministic ids, same fields modulo
  `time_limit`).

### Error paths & failure handling

- Unknown, case-mismatched, whitespace-padded, unicode, injection-ish and
  empty match ids are all clean 404s (`"Match not found"`); a missing
  `match_id` query param is a 422. Outsiders get `403 "Not your match"`
  before anything else (even on completed matches) and never pollute
  presence or create rounds. A match known only to Mongo is loaded, cached
  into memory and served normally.
- **Completed matches cannot get new questions** (task-list case 11
  resolved: 400, checked explicitly). **Abandoned matches still can** —
  the zombie-match bug already xfailed in the presence suite; this suite
  pins the round-creation side (an abandoned match serves round 1, accepts
  the win and serves round 2).
- `generate_question` failures (exception, missing keys, `None` return)
  surface as the generic 500 from the global handler with no internals
  leaked and **no half-created round state** (`current_round_id` stays
  unset, zero rounds stored) — except the quirk that `round_start_time` is
  stamped on the match before the round doc is built, so it survives the
  crash. The match fully recovers: the next poll with a healthy generator
  creates round 1 normally.

## Datetime handling & in-memory vs Mongo state (`ensure_utc`, `parse_round_start`, hydrate paths, `match_counter`)

Findings from `tests/test_match_datetime_and_memory_edge_cases.py` (46 tests:
43 pass, 3 strict `xfail` documenting real bugs). DB hydrate paths were
exercised with in-process fakes of `matches_collection.find_one` /
`rounds_collection.find_one` that return Mongo-shaped documents (naive
datetimes, no `player_last_seen`, deep copy per call).

### Bugs (xfail tests)

1. **Aware `created_at` turns `/api/game/start` into a 500** —
   `test_aware_created_at_should_still_reconnect` (xfail). The reconnect
   window computes `datetime.utcnow() - match["created_at"]` with no
   `ensure_utc`, so an aware timestamp (a doc migrated to `utc_now()`, or a
   Mongo client configured with `tz_aware=True`) raises `TypeError`, which
   the global handler converts to a generic 500 for that player on every
   subsequent start poll. An ISO-**string** `created_at` fails the same way.
   Pinned by `test_current_behavior_aware_created_at_500s_the_start_endpoint`
   and `..._string_created_at_also_500s...`. A *missing* `created_at` is
   fine (defaults to "now" → age 0 → reconnect).

2. **Memory wipe restarts round numbering and replays round ids** —
   `test_rehydrated_match_should_not_reuse_historical_round_ids` (xfail).
   On a DB hit for a match that fell out of memory, `get_question` hydrates
   the match doc but never its current round; `_create_next_round` counts
   rounds from `in_memory_rounds` only, ignoring both the match doc's
   `current_round_id` and everything persisted in Mongo. The resumed match
   therefore issues `round-…-1` again; the insert is skipped because that id
   already exists in the DB, leaving memory (new question) and Mongo (old
   question, old winner) permanently diverged under one round id. Scores
   survive (they live on the match doc). Pinned by
   `test_current_behavior_memory_wipe_restarts_round_numbering`.

3. **`match_counter` restart collision overwrites live matches** —
   `test_match_ids_should_survive_counter_restart` (xfail). Ranked ids are
   `match-{process_counter}`; after a restart the counter resets and the
   next ranked pairing re-issues `match-1`, replacing the still-live match
   in memory (and in Mongo via the update-instead-of-insert branch). The
   original players then get `403 "Not your match"` on their own match id.
   Pinned by `test_current_behavior_counter_restart_reuses_live_match_id`.

### Timestamp helpers (asserted in passing tests)

- `ensure_utc`: naive → same wall clock re-tagged UTC; aware input returned
  **unchanged**, including non-UTC offsets (despite the name, `+05:00` stays
  `+05:00`; math still works because aware-aware arithmetic normalizes).
  Idempotent.
- `parse_round_start`: `None`/garbage/empty-string → `None`; naive datetime
  or naive ISO string → aware UTC; offsets preserved with the correct
  instant. A trailing `"Z"` parses only on Python ≥3.11 (older versions
  would return `None` and disable the timeout for JS-produced timestamps).
  Non-str/non-datetime input (e.g. a unix timestamp float) raises
  `AttributeError` in `ensure_utc` instead of parsing or returning `None`.
- **Split timestamp regime**: match docs (`created_at`, `updated_at`) are
  naive `datetime.utcnow()`, while round docs and `player_last_seen` are
  aware `utc_now()`. The reconnect window only works because both sides of
  its subtraction happen to be naive — see bug 1.
- `round_start_time` is stored and served as an **aware ISO string** ending
  `+00:00`, scheduled ~3s out, byte-identical for both players on resume and
  echoed verbatim by `/api/game/status`; each new round gets a fresh, later
  anchor.

### 5-minute round timeout across `created_at` representations

- Strict `> 300s`: exactly 300s still resumes; 300.001s ties the round and
  advances. Aware datetimes, naive datetimes (Mongo round-trips) and ISO
  strings all work through `parse_round_start`/`ensure_utc`.
- **Unparseable `created_at` disables the timeout entirely**: `None` from
  the parser short-circuits `timed_out` to `False`, so a corrupted
  timestamp wedges the match on one question until somebody answers it.

### In-memory vs DB visibility, hydrate paths

- With the DB missing everything, the full lifecycle (create, join, 3
  rounds, completion) runs from process memory. Evicting the match mid-game
  (restart with DB down) 404s every gameplay route and `by-code`, and
  `/api/game/active` flips to `false` — while the round doc and the match
  lock stay orphaned in `in_memory_rounds` / `match_locks` forever (leak).
- The same match is simultaneously invisible to `/matches/all` (DB-only)
  and fully served by `/match/{id}/details` (memory fallback).
- Hydrate paths: `status` caches the DB doc back into memory (presence
  tracking then works from scratch); `question` creates round 1 for a
  hydrated match; `answer` hydrates match **and** round and scores
  normally; `give-up` hydrates both too. Membership (403) is enforced on
  hydrated matches on all four routes.
- **Presence history does not survive a wipe**: the rehydrated doc has no
  `player_last_seen`, never-seen counts as connected, so an opponent who
  walked away 999s ago flips back to "connected", and a give-up that would
  have auto-tied against a stale opponent waits forever instead
  (`test_stale_opponent_give_up_autotie_lost_after_memory_wipe`).

## Cross-match isolation & access control (parallel matches, outsiders, `/matches/all`, `/match/{id}/details`)

Findings from `tests/test_match_isolation_and_access_edge_cases.py`
(38 tests: 36 pass, 2 strict `xfail` documenting real bugs). Ranked match
codes were made deterministic by stubbing `secrets.token_urlsafe`.

### Bugs (xfail tests)

1. **Queueing for ranked abandons/hijacks your active friend match** —
   `test_ranked_queueing_should_not_abandon_active_friend_match` (xfail).
   The stale-match scan in `start_match` does not filter by `match_type`.
   A user with an active friend match who taps "play ranked" is silently
   "reconnected" **into the friend match** if it is <5s old (pinned by
   `test_current_behavior_ranked_start_reconnects_into_fresh_friend_match`),
   or has the friend match marked `abandoned` as a side effect if it is
   older (pinned by
   `test_current_behavior_ranked_start_abandons_older_friend_match`) — the
   friend opponent is never told except via status polls.

2. **`/match/{match_id}/details` is a live answer oracle with no authz** —
   `test_match_details_should_reject_non_participants` (xfail). The details
   endpoint never checks that the caller is a participant (nor that there
   is a caller at all — no auth header works), and its response embeds the
   persisted `rounds` array, which includes the **correct answer of the
   still-unresolved current round** plus both players' submitted answers.
   Anyone with the match_id — including the opponent in a second tab — can
   read the answer mid-round. Pinned by
   `test_current_behavior_match_details_leak_round_answers_to_outsiders`
   and `test_match_details_needs_no_auth_at_all`.

### Isolation between parallel matches (asserted in passing tests)

- Two simultaneous friend matches keep fully disjoint state: deterministic
  round ids embed the owning match (`round-{match_id}-1`), round wins,
  give-up ties and even full completion in one match move nothing in the
  other. The **same pair** can run two matches at once and each behaves
  independently. `get_match_lock` hands out a distinct, stable lock object
  per match id.
- One user can hold a ranked and a friend match simultaneously (create the
  ranked one first — see bug 1) and score in each without cross-credit;
  `/api/game/active` then reports only the **oldest** active match
  (insertion order), hiding the friend match entirely.
- A player of match A acting on match B is an outsider there: correct
  answers are 403 `"Not your match"` and score neither match, question
  fetches 403 before any round is created, give-ups set no flags.

### Outsider / spectator surface

- On ranked matches, outsiders get 403 on `question`, `answer`, `give-up`,
  `status` and `by-code` (`"Not authorized to access this match"`), leaving
  zero trace: no round created, no score, no presence heartbeat.
- But a **leaked match_id alone** still buys a spectator: `/match/{id}/details`
  works as a live scoreboard (memory fallback, DB down included), and its
  response reveals the `match_code`, which unlocks the unauthenticated
  `/api/game/friend/status/{code}` poller — id → code → anonymous polling.
- `/matches/all` returns the last 50 matches of **all** players (scores,
  statuses) to any caller, with or without an Authorization header.

### Ranked match codes vs the upper-casing friend endpoints

- `/api/game/match/{code}` compares case-sensitively: the exact
  `token_urlsafe` ranked code works for a member; the same code upper-cased
  404s (the friend endpoints, conversely, normalize with `.upper()` — the
  two code namespaces disagree about case).
- A mixed-case ranked code is unreachable via `friend/join` and the
  unauthenticated `friend/status/{code}` poller (both upper-case the input
  first). But when `token_urlsafe` happens to emit **no lowercase letters**,
  the ranked match becomes visible to both: join answers
  `400 "Match already started"` (confirming existence) and the tokenless
  status poller serves `match_id` + live status to anyone. Case mismatch is
  the only thing keeping ranked matches out of the friend lookups.

### Abandoned vs completed access differences

- `completed`: `question`/`answer` are 400 (`"Match is already completed"`);
  `give-up` is **not** blocked by status and answers `already_ended` off the
  final round instead. `abandoned`: the same player performing the same
  actions gets full service — new rounds, scoring, everything (zombie-match
  bug, xfailed in the presence suite; pinned again here from the
  access-difference angle).
- Both terminal states are served by `/api/game/status` (verbatim status,
  `winner_id` only for completed) and by `/api/game/match/{code}` for
  members; both are hidden from `/api/game/active` for all four players.

## Bot fallback & bot round timing (`start_match` bot branch, `_create_next_round` time_limit, `submit_answer` timeout + simulation)

Findings from `tests/test_bot_fallback_and_timeout_edge_cases.py` (28 tests:
27 pass, 1 strict `xfail`). Timeouts were exercised by monkeypatching
`main.utc_now` (frozen or offset clock) rather than backdating documents, so
the exact comparison operators are pinned. Bot RNG (`random.random` for the
dice roll, `random.uniform` for the response time, `random.randint` for the
ELO offset, `random.choice` for the name) was patched per-test for
deterministic races.

### Bug (xfail test)

1. **Stale cancel flag also eats the bot fallback** —
   `test_requeued_user_should_get_bot_after_second_ten_second_wait` (xfail).
   The known stale-cancel-flag bug has a second victim beyond human
   pairings: a user who cancels, re-queues and then waits out the full 10s
   is answered `{"status": "cancelled"}` by the bot-creation gate instead
   of getting the bot match they were promised — and is silently dropped
   from the queue on top. The full client-visible saga (cancel → re-queue →
   bogus "cancelled" at the first deadline → re-queue again → bot only after
   a *third* 10s wait) is pinned by
   `test_current_behavior_cancel_then_requeue_saga_needs_three_waits`.

### Bot match creation (asserted in passing tests)

- After 10s in queue (strict `< 10`: exactly 10.0s already creates the bot;
  9.5s still reports `searching` with `time_remaining: 0` via `int()`
  truncation), the poll returns the standard matched shape and the match
  doc is a normal counter-id/`token_urlsafe` document with
  `match_type: "random"`, `player2_id: "bot-opponent"`, naive `created_at`,
  and `player2_elo = player1_elo + randint(-150, -50)` (bounds spied;
  unpatched sampling stays in range).
- **The bot "user" object is discarded.** `start_match` builds a full bot
  dict (email, roster name, wins/losses) and only the ELO survives into the
  match. Consequently the same bot answers to **three different names**:
  the roster name (e.g. `"Taylor (bot)"`, drawn via `random.choice` from a
  fixed 7-name list) exists only in the one `start` response; `/status`
  invents `"AI Opponent"`; `/match/{code}` invents `"Bot"`.
- **Every bot branch is gated on the conjunction** `match_type == "random"`
  **and** `player2_id == "bot-opponent"`. A friend match with the bot
  sentinel forced into `player2_id` gets no time limit and no bot
  simulation (instant human win); a `"random"`-type match with a human in
  the player2 seat likewise behaves as a plain human match.
- `is_opponent_bot` in `/match/{code}` uses `"bot" in str(opponent_id)`
  instead of the sentinel: a human guest whose id merely contains "bot"
  (`guest-abbot-1234`) is labeled a bot to their opponent
  (`test_human_guest_with_bot_substring_in_id_is_mislabeled_as_bot`), while
  all gameplay branches correctly treat them as human.

### Bot round timing & timeouts

- `time_limit` is recomputed from the **current** `player1_elo` for every
  round (a mid-match ELO edit moves the next round's limit; finished rounds
  keep theirs). Question difficulty, meanwhile, follows the *bot's* lower
  ELO — two different ELOs feed one round.
- The timeout check is strict `elapsed > time_limit` against the synced
  `round_start_time`: landing exactly **on** the limit still gets the
  answer graded. Past the limit the check runs *before* answer parsing, so
  wrong and correct answers forfeit identically
  (`"Time limit exceeded"`, `correct: false`, `already_won: true`).
- Three timeout forfeits complete the match. With the offset pinned to
  -100 (bot 900 vs 1000): losing to the bot costs **26** ELO, beating it
  pays only **14** — the stake depends on who wins. The timeout completion
  path writes **only the human loser** (`$inc {elo: -26, losses: 1}`; the
  bot never gets a `wins` increment anywhere), while the answer-path win
  writes **both** sides, including a `$inc` against the nonexistent
  `"bot-opponent"` user document (a no-op in real Mongo).
- Bot race semantics on a correct user answer: the dice roll happens only
  inside the correct-answer branch (a wrong answer never triggers the bot —
  the bot can only score via the time limit); bot-rolled-wrong or
  bot-slower ⇒ user wins; bot faster ⇒ bot wins **while the response still
  says `correct: true`** (the loss is only visible in
  `round_winner`/`player2_score`); an exact time tie goes to the **bot**
  (`user_time < bot_time` is strict); answering during the 3s countdown
  clamps `user_time` to 0.0, which beats any positive bot time.
- Presence/give-up: `"bot-opponent"` short-circuits to connected before any
  bookkeeping (even a poisoned 30-day-old heartbeat is ignored), and a
  single give-up in a bot match immediately resolves `both_gave_up`/tie
  (the bot "gives up too") with no waiting limbo.
- Human-first ordering holds even at scale: with three users all past the
  10s deadline, the first poller pairs with a human (`ranked`) and only the
  leftover third user falls back to a bot (`random`).

## Cancel & queue lifecycle (`cancel_matchmaking`, queue state, `cancel_challenge` vs join races)

Findings from `tests/test_match_cancel_and_queue_lifecycle_edge_cases.py`
(25 tests: 21 pass, 4 strict `xfail`). Endpoint-level flows use guest
tokens; race tests call the route coroutines directly under
`asyncio.gather` with a laggy in-process matches DB (read snapshot, then
yield) to force deterministic interleavings.

### Bugs (xfail tests)

1. **Stray cancel poisons a first-ever pairing** —
   `test_cancel_before_ever_queueing_should_not_poison_first_pairing`
   (xfail). `cancel_matchmaking` is a blind `pop` + `set.add` that never
   checks queue membership, so a cancel from a user who was **never
   queued** (UI misfire, stale tab) still plants the flag. Their very
   first pairing afterwards is aborted: the opponent gets the bogus
   `{"status": "cancelled"}` and both users are silently unqueued. Pinned
   by `test_current_behavior_stray_cancel_aborts_first_pairing`.

2. **The cancel flag survives an entire completed match** —
   `test_late_cancel_flag_should_not_survive_a_completed_match` (xfail).
   A cancel landing just after a match was created is never consumed: the
   reconnect path ignores `cancelled_users`, playing the match doesn't
   touch it, and completion doesn't either. The flag sits through the
   whole game and then aborts the user's **next** pairing in a fresh
   session. Every intermediate state is pinned by
   `test_current_behavior_late_cancel_flag_survives_completed_match`.

3. **`cancel_challenge` has no in-memory fallback** —
   `test_memory_only_waiting_match_should_be_cancellable` (xfail). Like
   the other challenge endpoints it reads only `matches_collection`, so a
   waiting match that exists only in `in_memory_matches` (DB down/empty)
   is fully **joinable** by code but 404s on cancel — the creator cannot
   kill their own match. Pinned by
   `test_current_behavior_memory_only_match_uncancellable_but_joinable`.

4. **Racing `cancel_challenge` vs `join` lets both succeed** —
   `test_racing_cancel_challenge_vs_join_must_not_both_succeed` (xfail).
   Neither endpoint takes the per-match lock and both do check-then-act
   around an awaited DB read. With any read latency, the join reads
   `waiting`, the cancel reads `waiting`, the join activates the match and
   returns 200 — and the cancel then deletes the *active* match from DB
   **and** memory. The acknowledged joiner is left holding a match id that
   404s on every subsequent request, as pinned by
   `test_current_behavior_cancel_challenge_vs_join_race_deletes_joined_match`.
   (Sequentially the guard works: join-then-cancel is 400.)

### Cancel semantics & flag bookkeeping (asserted in passing tests)

- Cancel is idempotent and unconditional: not-queued and double cancels
  both return `{"status": "cancelled"}`; the set can't double-count; other
  queued users are untouched.
- Flags are consumed strictly **pairwise**: a pairing between other users
  consumes nothing, and an aborted pairing consumes exactly the two ids it
  popped. There is no global sweep.
- **`cancelled_users` is an unbounded leak**: 40 distinct cancel-and-leave
  users leave 40 permanent set entries that survive any amount of
  unrelated matchmaking (no TTL, no cap; removal only ever happens via a
  later pairing/bot attempt involving that user).

### Queue lifecycle without polling

- **Queue entries never expire on their own** — the 10s bot deadline is
  only evaluated when the queued user polls. An hour-stale entry is still
  present and still matchable.
- A later arrival is paired against the hour-gone user into a **ghost
  match** the absent player will never know about. Because the ghost never
  polls, they have no `player_last_seen` entry and the never-seen rule
  reports them **connected forever**: the live player can't get a give-up
  auto-tie (`waiting_for_opponent: true`) and is stuck until the 5-minute
  round timeout. Had the ghost polled even once, presence would flip to
  disconnected 12s later and give-up would auto-tie as designed.

### Abandon ↔ cancel interactions

- Re-searching from a >5s-old match abandons it; a follow-up cancel
  empties the queue and plants the flag but does not resurrect or complete
  the abandoned match, and `/api/game/active` stays false for both.
- Full round trip after mutual abandonment: both users cancel, both
  re-queue — the first pairing attempt is eaten by the stale flags (as a
  pair), and only the second attempt re-matches them.

### `cancel_challenge` on waiting matches

- With the DB reachable, cancelling a waiting (code-only) friend match
  wipes it from DB **and** memory and kills the code on every surface
  (join and the unauthenticated status poller both 404).
- **Orphan leak**: because gameplay routes serve waiting matches, the
  creator can fetch a question before anyone joins; cancel then deletes
  only the match doc, leaving the round in `in_memory_rounds` and the lock
  in `match_locks` forever.

### Racing queue cancel vs pairing

- The dangerous window (`await users_collection.find_one` between
  selecting and popping an ObjectId opponent) is *safe* against cancels:
  a cancel landing inside it makes the pairing consume the fresh flag and
  return `{"status": "cancelled"}` to the joiner — spurious for them, but
  no ghost match is created and all flags are consumed. A cancel landing
  *before* the scan simply leaves the joiner searching, with the
  canceller's flag lingering (feeding bugs 1/2).

## API contract (`tests/test_match_api_contract_edge_cases.py`)

80 tests (79 pass, 1 strict `xfail`). This suite nails down the *shape* of
every people-match endpoint — exact response-key sets, the status-code
matrix for misuse, content-type/body handling, pydantic extra-field and
coercion behavior, path-injection/long-input hardening, and method
negotiation — independent of gameplay semantics. Friend matches back the
member-only checks; a small in-memory `matches_collection` fake backs the
challenge accept/cancel/pending contracts.

### Bug (xfail)

- **No 401 path (bug 26).** `test_anonymous_state_change_should_be_401`
  (xfail): `POST /api/game/friend/create` with **no** `Authorization`
  header returns 200 (creating a match owned by the shared guest identity)
  instead of 401. Same root cause as the unauthenticated-exposure findings
  (bugs 1/21). Current behavior is pinned by
  `test_missing_or_bad_credentials_never_401_current_behavior`, which shows
  a missing header, a garbage bearer, an empty bearer and a `Basic` scheme
  all yield `200 {"has_active_match": false}`.

### Response-key contracts (asserted in passing tests)

Exact key sets are locked for every endpoint, so a silent field
add/rename/drop fails a test:

- `friend/create` → `{match_id, match_code, link, status}` (6-char code,
  `status: "waiting"`, code embedded in the link).
- `friend/join` → `{match_id, status}` (`active`).
- `friend/status/{code}` → `{match_id, status, player1_ready,
  player2_ready}`.
- `game/active` → `{has_active_match}` when idle, and adds
  `{match_id, match_type, opponent}` when a match is live.
- `game/cancel` → `{status}`.
- `game/question` → `{round_id, expression, evaluate_at,
  ask_for_derivative_only, round_start_time}` (human matches have **no**
  `time_limit`; bot matches add it — see the bot suite).
- `game/answer` → `{correct, round_winner, player1_score, player2_score,
  match_winner, elo_change}` in progress, plus `already_won` once the round
  has a winner.
- `give-up` → `{status, waiting_for_opponent}` (lone), `{status,
  round_winner, player1_score, player2_score}` (both), `{status,
  round_winner}` (already ended).
- `game/status/{id}` → the 16-key polling payload including
  `opponent_connected`, `round_start_time`, both `*_gave_up` flags and
  `winner_id`.
- `game/match/{code}` → `{match_id, status, player1_id, player2_id,
  player1_score, player2_score, current_round, is_player1, opponent_name,
  is_opponent_bot}`.
- `challenges/pending` → list of `{match_id, match_code, challenger,
  created_at}`; `accept` → `{match_id, match_code, status}`; `cancel` →
  `{status}`.

### Status-code matrix (asserted in passing tests)

- **404** — unknown `match_id` on question/answer/give-up/status; unknown
  code on join/friend-status/match-by-code; unknown challenge id on
  accept/cancel.
- **403** — a non-participant on question/answer/give-up/status/match-by-code;
  the wrong actor on challenge accept (only player2) / cancel (only
  player1). Note the 403-vs-404 split is an existence oracle: an outsider
  gets 403 for a real match id but 404 for a fake one.
- **400** — question/answer on a completed match; joining an
  already-started match or your own match; and `answer`/`give-up` before
  anyone has fetched a question return 404 `"No active round"`.
- **422** — missing body; missing/`null`/int `match_id`; missing/`null`/
  list/dict `answer`; missing/non-string `mode`; missing query `match_id`
  on question/give-up.
- **No 401 anywhere** (see the bug above).

### Content-type, extra fields, coercion (asserted in passing tests)

- Missing JSON body, form-encoded body, a JSON payload sent as
  `text/plain`, and malformed JSON are all **422** (pydantic never sees a
  valid model).
- **Extra unknown fields are silently ignored** (the models don't set
  `extra="forbid"`): `start` and `answer` accept and drop bogus keys.
- **Coercion follows pydantic**: `continue_existing: "true"` is accepted,
  `"maybe"` is 422; `answer` is `Union[str, float]` so a JSON number is
  valid input (graded via the numeric branch).

### Injection / long input / methods (asserted in passing tests)

- Weird `match_id` path segments (spaces, encoded null bytes, traversal-ish
  `..`, `;`/`|`/`<script>`/SQL-ish), unicode ids, encoded slashes, and
  injection-looking match codes on join all return **404/422, never 500**.
- Very long `match_id` (up to 50k) and `match_code` (up to 50k on join, 5k
  on the status path) all 404 without crashing.
- Wrong HTTP methods return **405** with a populated `Allow` header
  (GET on `/answer`, PUT on `/start`, DELETE on `friend/create`, POST on
  `/active` and `/question`, GET on the challenge action routes, etc.).

## PvP math equivalence (`tests/test_match_math_equivalence_in_pvp_edge_cases.py`)

117 tests (114 pass, 3 strict `xfail`). Every answer is graded through the
real `/api/game/answer` path (the inline SymPy cascade in `submit_answer`),
not the standalone `check_math_equivalence`. A `derivative_question` fixture
fixes the stored answer to the server form `2·x`; an `evaluate_at_question`
fixture (monkeypatching `generate_question` with `ask_for_derivative_only:
False` and an integer answer) drives the numeric-tolerance branch.

### Bugs (xfail)

1. **Unbounded integer power hangs grading — DoS (bug 24).**
   `test_unbounded_integer_power_answer_does_not_hang_grading` (xfail).
   The grader replaces `^`→`**` and calls `parse_expr(..., evaluate=True)`,
   so `9**9**9` makes Python evaluate `9**387420489` (a ~370-million-digit
   integer) with no timeout — the request never returns. The test grades in
   a **spawn subprocess** with a 12s watchdog and asserts it finished; it
   doesn't, so the xfail holds without wedging the run. A companion,
   `test_grade_with_timeout_harness_reports_finish_for_normal_answer`,
   proves the watchdog reports 200 for a normal answer.

2. **Power-tower answers hang the numeric fallback — DoS, second entry
   point (bug 24).**
   `test_power_tower_should_not_hang_numeric_fallback_at_large_sample_points`
   (xfail). `x**x**x**x**x` passes the entire symbolic cascade quickly
   (every step returns False), but the last-resort numeric fallback
   substitutes `random.uniform(1, 10)` for `x` and calls `N()` on the
   result. The tower is astronomically large for sample points above ~2 —
   mpmath grinds forever at 2.5 and everywhere in [7, 10], raises
   `MemoryError`/`OverflowError` in between, and only completes below ~2 —
   so roughly 85% of such submissions wedge the worker exactly like
   `9**9**9`. The xfail pins `uniform` to 9.0 inside a spawn-subprocess
   watchdog for determinism;
   `test_power_tower_answer_graded_wrong_when_sampled_at_small_point`
   (passing) pins 1.5 to show the benign outcome. (Found the hard way:
   this input previously sat in the "pathological but safe" parametrize
   list and intermittently hung the whole suite run.)

3. **`×`/`∗` rejected in PvP but accepted by the daily-challenge checker
   (bug 25).** `test_times_sign_should_be_accepted_in_pvp` (xfail): `2×x`
   is graded wrong because `submit_answer` normalizes only `·`. Current
   behavior is pinned by
   `test_alternate_unicode_multiplication_is_rejected_current_behavior`
   (`2×x`, `2∗x`, `2✕x` all wrong) and
   `test_check_math_equivalence_accepts_times_sign_unlike_pvp` (the helper
   returns `True` for the same string).

### Grading behavior (asserted in passing tests)

- **Accepted equivalents** for `2·x`: `2*x`, `2x`, `2 * x`, padded `  2x  `,
  `x*2`, `x 2`, `x2`, `2·x`, `(2)(x)`/`(x)(2)`/`2(x)`, deep nesting
  `((((2x))))`, `+2x`, `2x*1`, `2x/1`, `x*x/x*2`, `2*x + 0`; fractions
  `4x/2`, `6x/3`, `x/(1/2)`, `10*x/5`, `(4/2)*x`; and generous rewrites
  `sqrt(4)*x`, `√4*x`, `2.0x`, `2ex/e`, `x+x`.
- **The grader evaluates arbitrary SymPy calls** in answers: `diff(x^2,
  x)`, `Derivative(x^2, x)`, `diff(x**2)`, `integrate(2, x)`,
  `exp(log(2x))`, the trig identity `2*x*sin(x)**2 + 2*x*cos(x)**2`,
  `cancel((2x**2)/x)`, `simplify(4*x/2)` are all accepted — worth knowing
  the answer box is a small CAS, not an algebra-only field. Relatedly,
  SymPy treats `#` as a comment, so `2x #comment` grades as `2x`.
- **Rejected near-misses / wrong math**: `2`, `x`, `-2*x`, `2*x + 1`,
  `x^2`, `2*x^2`, `2/x`, `3*x`, `x/2`; python-but-wrong `2**x`, `x**2`,
  `0x2` (hex → 2), `2y`, `2*X` (capital `X` is a distinct symbol),
  `idiff(x^2, x)`, `e**log(2x)` and `ln(e^(2x))` (SymPy's `e` is a plain
  symbol, not Euler's number, so these don't reduce to `2x`).
- **Junk around the answer** (`2x;`, `answer is 2x`, `2x!`, `d/dx(x^2)`,
  `= 2x`, `2x)))))`, free-text) is graded wrong, never 500.
- **Code-injection-looking answers** (`__import__('os').system('id')`,
  `open('/etc/passwd').read()`, `eval(...)`, `exec(...)`, `lambda: 2*x`,
  `Symbol('x')*2`, `[].__class__...`) are graded wrong **without
  executing** — no RCE, no crash.
- **Numeric (evaluate_at) branch**: with stored answer `6`, `abs(diff) <
  0.1` accepts `6`, `6.0`, `"6"`, `"6.0"`, `" 6 "`, `"6\n"`, `"0006"`,
  `"6e0"`, `6.05`, `5.95`; rejects `6.2`, `6.5`, `5.5`, `-6`, `0`, `"six"`,
  `"2*3"` (the numeric branch does **not** evaluate expressions —
  `float("2*3")` raises), `"inf"`, `"nan"`, empty/whitespace, and JSON
  `true` (coerced to `1.0`). The question payload correctly advertises
  `ask_for_derivative_only: false` and `evaluate_at: 3`.
- **Pathological SymPy inputs return 200 graded-wrong** (never 500):
  unbalanced/bare operators (`(`, `*`, `**`), `x..2`, quoted answers,
  `1/0`/`x/0`, `factorial(50000)`, complex `sqrt(-4)*x*I/1`, large
  symbolic exponents `x**(10**6)`, and the infinities `oo`/`zoo`/`nan`.
  The broad `except (SympifyError, Exception)` swallows every failure —
  which is exactly why the inputs that *hang* rather than *raise*
  (`9**9**9` in the parser, `x**x**x**x**x` in the numeric fallback,
  bug 24) are so dangerous.

## Multiplayer stress & sequential chaos (`tests/test_match_multiplayer_stress_edge_cases.py`)

54 tests (50 pass, 4 strict `xfail`). Where the other suites isolate one
endpoint, this one simulates *populations*: waves of guests hitting the
queue at once, one user spamming parallel requests, chaotic cancel/start
churn, spam of friend matches and challenges, full matches played
end-to-end, and long-timeline flows (complete → requeue → rematch).
Simultaneity is driven by `asyncio.gather` over the route coroutines
(single-worker semantics — the guest-id pairing path has no await between
queue check and pop, so the loop serializes it exactly as one uvicorn
worker would) and by rapid sequential HTTP calls for the endpoint-level
flows.

### Bugs (xfail tests)

1. **Churn makes a whole cohort unmatchable for two extra rounds of
   polling** — `test_churned_users_should_all_be_matchable_on_first_mass_start`
   (xfail; population-level face of the stale-cancel-flag bug 6). After 5
   users each rapidly start+cancel a few times, a full mass re-queue forms
   **zero** matches: the leftover flags eat every pairing pairwise, users
   who never cancelled *last* still receive `{"status": "cancelled"}`, and
   the queue is left holding one user. Pinned step by step by
   `test_current_behavior_first_mass_start_after_churn_pairs_nobody`
   (pass 1: `searching/cancelled/searching/cancelled/searching`, zero
   matches) and
   `test_current_behavior_second_mass_start_finally_forms_two_matches`
   (pass 2 finally pairs (u3,u2) and (u5,u4), while u1 eats one more bogus
   "cancelled" and ends up matchless).

2. **Pending-challenge list truncates spam at 10 with no dedupe** (bug 27)
   — `test_pending_list_should_expose_all_twelve_spam_challenges` (xfail).
   12 identical challenges from one challenger are all stored (no cap, no
   dedupe — `test_twelve_identical_challenges_all_stored_no_dedupe`), but
   the invitee's list is a bare `to_list(length=10)`: exactly the 10
   oldest, no paging, no total. The hidden two only scroll into view as
   older ones are accepted or cancelled
   (`test_cancelling_spam_challenges_uncovers_the_hidden_ones`).

3. **An overlapping pair cannot queue for ranked** —
   `test_overlapping_pair_should_be_able_to_queue_ranked_from_friend_match`
   (xfail; pair-level face of the match_type-blind reconnect, bug 7). With
   a fresh (<5s) active friend match, *both* players tapping "play ranked"
   are answered `matched` with the **friend** match id; neither enters the
   queue and no ranked match can form. Pinned by
   `test_current_behavior_ranked_start_hijacks_fresh_friend_match_for_both`;
   the >5s flavor
   (`test_current_behavior_ranked_start_abandons_older_friend_match_then_pairs`)
   shows the friend match being silently abandoned and the pair dropped
   into a fresh ranked match instead.

4. **Hour-stale queue entries are still matchable** —
   `test_hour_stale_queue_entry_should_not_be_matchable` (xfail; bug 15
   promoted to a strict xfail). With one user queued 3600s ago and five
   fresh arrivals, the first arrival is sacrificed into a ghost match with
   the long-gone user
   (`test_hour_stale_waiter_is_paired_first_then_arrivals_pair_among_themselves`),
   the never-polling ghost is reported `opponent_connected: true`
   (`test_ghost_match_reports_never_polling_opponent_as_connected`), and
   the live player's give-up cannot auto-tie
   (`test_ghost_opponent_blocks_the_give_up_auto_tie`). If the "ghost" was
   merely slow and polls within 5s, the reconnect window does route them
   into the ghost match
   (`test_long_waiter_polling_right_after_ghost_creation_reconnects_into_it`).

### Simultaneous-arrival semantics (asserted in passing tests)

- **10 guests at once → exactly 5 matches, empty queue, no cross-pairing.**
  Arrivals alternate searching/matched; every user lands in exactly one
  match, every match is a distinct 2-player pair (no triple-matching, no
  bot, no self-match), and the five users initially answered "searching"
  are all reconnected into their match on the next poll with no duplicate
  matches created. An odd cohort (9) leaves exactly the last arrival
  genuinely queued. The same invariants hold over rapid sequential HTTP
  polls.
- **One user spamming 8 parallel starts** holds exactly one queue slot,
  never self-matches, and re-polls do not reset the queue timer
  (`joined_at` is written once). Once matched, 5 parallel start calls all
  reconnect into the same single match.

### Create/join spam (passing tests)

- 20 friend matches from 20 creators get 20 unique codes/ids; **a single
  creator can also stack 20 waiting matches** (no per-user cap — memory/DB
  spam vector, cross-referenced with the cancel-suite leak findings).
  Random joins activate exactly the targeted matches (each joiner becomes
  player2 of their own match only), a second joiner on a taken code gets
  `400 "Match already started"` and does not evict the first, and
  untouched matches remain waiting and joinable afterwards.

### Full-match and rematch flows (passing tests)

- A first-to-3 friend match with alternating winners lands at 3-2 with the
  scores stepping exactly (1,0),(1,1),(2,1),(2,2),(3,2); both players
  always see the same round id before answering; round ids run
  `round-<match>-1..5`; wrong-answer flurries neither score nor advance
  the round; completion pays `elo_change: 0` (friend matches are
  unranked), and both players get 400 on any further question/answer.
- Two ranked matches in parallel never share state: distinct
  deterministic round ids per match, a win in one moves nothing in the
  other, rounds progress independently (match 1 on round 3 while match 2
  still sits on round 1), and both complete independently with the
  standard (phantom-for-guests) 20 ELO.
- After a completed ranked match, the <5s reconnect window correctly
  ignores the completed match; both players immediately requeue and
  rematch into a fresh match (new id, new code, 0-0), the old match stays
  frozen, and `/api/game/active` points both at the rematch.

### Storm behavior (passing tests)

- **Give-up storms**: solo spam keeps returning
  `{"status": "gave_up", "waiting_for_opponent": true}` without resolving
  the round; the opposite player's give-up then ties it at 0-0; post-tie
  spam from both sides returns `already_ended`; three full rounds of
  mutual give-up storms leave the match active at 0-0 (ties can never
  finish a first-to-3) with round ids advancing 1→2→3; and a quitter can
  still snipe the round with a correct answer after their own give-up
  storm (quirk shared with the answer suite).
- **Status-poll storms**: 15 alternating polls with wrong answers mixed in
  never mutate scores, never fork the round (`in_memory_rounds` stays at
  1), and echo a byte-identical `round_start_time` every time; presence
  stays `opponent_connected: true` in both directions throughout; after a
  round win, both pollers see the same winner/score state on every
  subsequent poll; 20 polls between rounds change nothing until the next
  question advances to round 2 exactly once.
- **Friend + ranked overlap** (in the safe order, ranked first): one pair
  can hold both an active ranked and an active friend match, score in
  each without cross-credit, while `/api/game/active` reports only the
  older (ranked) match.

## Auth identity & match ownership (`get_current_user`, `tests/test_match_auth_identity_edge_cases.py`)

37 tests (all passing — this suite documents *current behavior* rather than
adding new xfails; the defects it exercises are already pinned by the 401
xfail in the API-contract suite and the exposure findings in the isolation
suite). Everything turns on the demo-mode resolver `get_current_user`
(main.py ~242-310), whose exact contract is: (1) a `Bearer` token starting
with `"guest-"` becomes that guest verbatim, unverified; (2) no credentials
at all become the single shared fallback `"guest-user-id"`; (3) otherwise a
JWT is decoded, and **every** failure — missing `sub`, unknown email, bad
signature, expired, wrong scheme, empty token — silently falls back to that
same `"guest-user-id"`. Only a valid JWT whose `sub` email resolves in
`users_collection` yields a real (ObjectId) identity.

The suite is organized around ten identity quirks that break people-matching:

1. **Anonymous callers all share one identity.** With no `Authorization`
   header every caller is `"guest-user-id"`, so two anonymous browsers
   occupy the *same* matchmaking-queue slot and can never pair (a self-match
   is impossible); no match is ever created for the shared id. Pinned by
   `test_two_anonymous_callers_share_one_queue_slot_and_never_match` and
   `test_anonymous_pair_cannot_form_a_ranked_match`.
2. **Two distinct explicit guest tokens are two identities** that pair into
   a normal ranked match — `test_two_distinct_guests_pair_into_a_ranked_match`.
3. **`Bearer guest-user-id` collides with the no-auth fallback.** The
   string `"guest-user-id"` itself starts with `"guest-"`, so the explicit
   token and the tokenless path resolve to one identity and cannot match
   each other, though either can still match a *different* guest. Pinned by
   `test_anonymous_then_explicit_guest_user_id_is_one_queue_slot` and
   `test_explicit_guest_user_id_can_match_a_different_guest`.
4. **Every invalid-JWT form collapses to the shared guest.** A garbage
   bearer, two *different* garbage bearers, a `Basic` scheme, an empty
   `Bearer `, a wrong-secret forgery and an expired token all resolve to
   `"guest-user-id"` — so two different malformed tokens still can't match
   each other, but each can match a real guest. Pinned by
   `test_two_different_bad_tokens_collapse_to_one_identity_and_cannot_match`
   and `test_wrong_scheme_and_empty_bearer_also_fall_back_to_guest`.
5. **Username challenges require a registered user in the DB.** `friend/create`
   with `opponent_username` does an exact `users_collection.find_one`; a
   registered username yields a `pending` challenge pinned to that user's
   ObjectId, while an unknown name (or a guest's on-screen `Guest xxxx`
   label, which is *not* a username document) silently degrades to a
   `waiting` open match. Pinned by
   `test_challenge_to_registered_username_creates_pending`,
   `test_challenge_to_unknown_username_degrades_to_waiting` and
   `test_guest_display_names_are_not_registered_usernames`.
6. **ObjectId-vs-string comparison differs by route.** An ObjectId equals
   its own hex string *only after `str()`*
   (`test_objectid_equals_its_hex_string_only_after_str`). Gameplay routes
   (`question/answer/give-up/status`) `str()`-compare both sides, so an
   ObjectId owner is admitted via their JWT; the ownership checks in
   `join_friend_match` ("cannot join your own match") and `accept_challenge`
   compare **raw**, where ObjectId==ObjectId still holds but no guest string
   can ever collide with an ObjectId — so a registered invitee can accept an
   ObjectId challenge while a guest cannot. Pinned by
   `test_registered_user_cannot_join_their_own_waiting_match`,
   `test_registered_invitee_can_accept_objectid_challenge`,
   `test_guest_cannot_accept_an_objectid_invitee_challenge` and
   `test_gameplay_str_compare_admits_the_objectid_owner`.
   *Latent risk:* because `get_current_user` never mints a bare-hex-string
   identity (guest ids always start with `"guest-"`), the raw-vs-`str()`
   divergence cannot currently be *exploited* to confuse ownership — but any
   future code path that produces a hex-string `_id` would immediately make
   the two comparison styles disagree.
7. **Mixed ObjectId/guest matches compare correctly via `str()`.** A match
   with an ObjectId player1 and a guest-string player2 admits both real
   participants on `status` (ids stringified in the payload) and 403s an
   outsider guest; an anonymous caller is an outsider unless the shared
   `"guest-user-id"` is literally a participant. Pinned by
   `test_status_admits_the_objectid_player_via_jwt`,
   `test_outsider_guest_is_403_on_a_mixed_id_match` and
   `test_anonymous_caller_is_outsider_unless_the_guest_is_shared_id`.
8. **Changing the token mid-match locks you out of your own match.**
   Ownership is bound to the exact token, so after a storage wipe / re-login
   that mints a new guest id, `question/answer/give-up/status` all 403 the
   new token while the original identity retains full ownership and can
   still score. Dropping the token entirely (→ `guest-user-id`) is likewise
   an outsider. Pinned by
   `test_changing_token_mid_match_locks_the_player_out`,
   `test_original_token_still_owns_the_match_after_the_switch` and
   `test_switching_to_no_auth_mid_match_is_also_an_outsider`.
9. **One person with two tokens can match against themselves.** Two tabs
   mint two distinct guest ids that the backend treats as two players, so
   they pair and can play a self-match to completion — a matchmaking
   integrity gap (self-play farming). Pinned by
   `test_same_person_two_tokens_match_against_themselves` and
   `test_same_person_can_play_a_full_self_match_to_completion`.
10. **`get_current_user` resolution decides ownership, not the token's
    email.** A valid JWT resolves to the DB user (ObjectId identity); a
    token with no `sub`, an email absent from the DB, an expired signature,
    or a wrong-secret forgery all fall back to `"guest-user-id"`. A
    registered user's own *expired* token is therefore an outsider on the
    match they created, and anything created anonymously is owned by the
    shared guest — so a *second* anonymous caller inherits it (there is no
    per-session isolation for tokenless users). Pinned by
    `test_valid_jwt_resolves_to_the_db_user_identity`,
    `test_jwt_without_sub_falls_back_to_shared_guest`,
    `test_expired_jwt_falls_back_to_shared_guest`,
    `test_registered_owner_and_expired_token_do_not_share_a_match` and
    `test_no_credentials_shares_ownership_across_all_anonymous_callers`.

### Cross-references to already-pinned bugs

- The complete absence of a 401 path (all missing/malformed/wrong-scheme
  credentials return a 200 guest identity) is bug 26, pinned by the
  API-contract suite's `test_anonymous_state_change_should_be_401` (xfail).
- The shared-guest-identity collision for anonymous callers is the same root
  cause behind the unauthenticated-exposure findings (bugs 1/21) — anyone
  tokenless is admitted as `"guest-user-id"`.

No new bug is filed here: the self-match gap (case 9) and the
change-token-lockout (case 8) are inherent to demo-mode auth and are
documented as current behavior rather than defects to fix in isolation;
they resolve once real authentication (bug 26 / the fix-order §9 item) lands.

## Newly found (audit pass)

A second bug-hunting pass over the match code, diffed against all fifteen
existing suites and this report, surfaced **six previously untested bugs**.
Each is pinned in `tests/test_match_newly_found_bugs_edge_cases.py`
(12 tests: 6 strict xfail + 6 passing current-behavior pins). With this
file the full repository suite collects 844 tests and runs as
**809 passed, 35 xfailed**.

### New bugs (severity-ranked)

28. **P1 — Hydrated give-up flags erase the opponent's persisted give-up**
    (xfail `test_hydrated_opponent_give_up_should_survive_and_tie_the_round`).
    `give_up_round`'s initialization block resets **both** `*_gave_up`
    flags to `False` whenever the caller's own key is missing from the
    round doc. Because a give-up only ever `$set`s the giver's single
    field to Mongo (the `False` pair for the other flag lives in memory
    only), any round hydrated from the DB carries exactly one flag — so
    after a restart/eviction/worker switch, the *other* player's give-up
    wipes the persisted one instead of completing the both-gave-up tie.
    The round that should tie stays open, Mongo ends up saying "both gave
    up" while memory says only one did, and the first giver must give up
    a **second** time to get the tie they already earned. Pinned by
    `test_current_behavior_hydrated_give_up_erases_opponent_flag`.

29. **P1 — `get_game_status` never hydrates the current round, so a
    resolved round's result vanishes from the poll after a memory wipe**
    (xfail `test_status_should_report_round_winner_persisted_in_mongo`).
    Unlike `submit_answer` and `give_up_round`, which both fall back to
    `rounds_collection` on a cache miss, the status poller reads round
    state only via `current_round_id in in_memory_rounds`. After a wipe,
    the current round's `winner_id` (and both gave-up flags) are in Mongo
    but the poll reports `round_winner: None` / `False` — a client
    waiting on the status poll for the round result never sees it (while
    the score, which lives on the match doc, *does* survive, leaving the
    client on a board that says 1-0 with apparently nobody having won a
    round). Pinned by
    `test_current_behavior_round_result_vanishes_from_status_after_wipe`.

30. **P1 — A creator can solo-play and solo-complete a `waiting`
    (unjoined) friend match**
    (xfail `test_answer_on_unjoined_waiting_match_should_not_score`).
    Gameplay routes only reject status `completed`, and the friend-match
    branch of `submit_answer` awards the round to whoever answers first —
    which, on a match nobody has joined, is always the creator playing
    alone. Three correct answers complete the never-joined match 3-0
    with `winner_id` set and `player2_id` still `None`, after which the
    invited friend's join is bounced with the misleading
    `400 "Match already started"`. This is the `waiting`-status sibling
    of bug 9 (pending challenges playable without accepting), but worse:
    it reaches full completion with a winner against nobody. Pinned
    end-to-end by
    `test_current_behavior_creator_solo_completes_waiting_match`.

31. **P2 — `/api/game/match/{code}` has no DB fallback**
    (xfail `test_match_by_code_should_hydrate_from_db_like_other_routes`).
    Every other gameplay route (`question`, `answer`, `give-up`,
    `status`) hydrates a match from Mongo on a memory miss; the by-code
    lookup scans `in_memory_matches` only. After a restart with a
    perfectly healthy DB, the by-code route 404s a live match until some
    *other* endpoint happens to cache it back into memory — pinned by
    `test_current_behavior_by_code_404s_until_another_route_hydrates`,
    which shows the same code flipping 404 → 200 after one status poll.
    (The previously documented eviction finding covered only the DB-*down*
    case; the inconsistency with a healthy DB was untested.)

32. **P2 — The by-code response's `current_round` is hardwired to 0**
    (xfail `test_by_code_current_round_should_track_round_progression`).
    `get_match_by_code` returns `match.get("current_round", 0)`, but no
    writer anywhere sets a `current_round` key — rounds are tracked via
    `current_round_id`. The field is 0 forever, however deep into the
    match the players are; any client trusting it (e.g. for a "Round N"
    header or reconnect UI) renders round 0 mid-game. Pinned by
    `test_current_behavior_by_code_current_round_stuck_at_zero` (round 2
    live, score 1-0, field still 0).

33. **P2 — `/api/game/active` labels every human guest opponent
    "AI Opponent"**
    (xfail `test_active_match_should_not_label_human_guest_as_ai`).
    The opponent lookup `users_collection.find_one` misses for guest ids
    and the fallback string assumes the opponent is a bot. A friend match
    between two humans tells each of them they are playing an AI, while
    `/api/game/status` labels the very same opponent "Player 2" in the
    same match — pinned (with that inconsistency) by
    `test_current_behavior_active_calls_human_guest_ai_opponent`. Same
    family as bug 23 (`is_opponent_bot` substring check): bot-ness should
    key on the `"bot-opponent"` sentinel, never on lookup failure.

### Suggested fixes

- **Bug 28**: initialize only the *caller's* missing flag (or default
  both flags with `round_doc.setdefault(...)`) instead of resetting both;
  better, persist both flags on first write so hydrated docs are complete.
- **Bug 29**: give `get_game_status` the same `rounds_collection`
  fallback (and cache-back) that `submit_answer`/`give_up_round` already
  have.
- **Bug 30**: have `get_question`/`submit_answer` reject non-`active`
  matches (`waiting`/`pending`/`abandoned`) — this also closes bugs 8
  and 9, which share the status-gating root cause.
- **Bug 31**: add a `matches_collection.find_one({"match_code": ...})`
  fallback (plus memory cache-back) to `get_match_by_code`, mirroring
  `join_friend_match`.
- **Bug 32**: either derive `current_round` from the live round doc's
  `round_number` or drop the field from the response.
- **Bug 33**: label opponents by the `"bot-opponent"` sentinel and fall
  back to a neutral `"Player"`/`"Guest"` for unknown human ids (one fix
  alongside bug 23).

### How to run

```bash
# Newly-found-bugs suite only (12 tests: 6 pass, 6 xfail)
python3 -m pytest tests/test_match_newly_found_bugs_edge_cases.py -q

# Full repository suite (844 tests: 809 pass, 35 xfail)
python3 -m pytest tests/ -q
```

As everywhere else in this campaign, the xfails are `strict`, and each has
a sibling test pinning the current broken behavior, so a fix (or a
regression of the pin) surfaces immediately.

## Give-up & status polling (`/api/game/give-up`, `/api/game/status/{id}`, `tests/test_match_giveup_status_edge_cases.py`)

56 tests (54 pass, 2 strict `xfail`). A dedicated pass over the niche
give-up and status-polling edges in people matches, complementing the
presence/lifecycle suite: give-up before any round exists, give-up on
already-won/tied/completed rounds, nearly-concurrent give-ups driven
through `asyncio.gather` over the route coroutines, give-up racing a
correct answer, status payload completeness per lifecycle state
(waiting/active/completed/abandoned), the three `round_winner` value
shapes (player id / `"tie"` / `null`), exact presence-boundary flips
under a steppable frozen clock, heartbeat bookkeeping, points/ELO
neutrality of give-ups, repeated give-ups, ranked-vs-friend parity,
both-players-stale resolution, outsider 403s, and polling while still
searching. With this file the full repository suite collects **900
tests** and runs as **863 passed, 37 xfailed**.

### New bug

34. **P1 — Concurrent give-ups on a cache-missed round lose one flag and
    wedge the round** (xfail
    `test_concurrent_give_ups_after_round_eviction_should_resolve_tie`).
    `give_up_round` takes no per-match lock and hydrates a cache-missed
    round via an awaited `rounds_collection.find_one`. Two concurrent
    give-ups (both players quitting the same stuck round — exactly the
    situation after an eviction/restart) each hydrate a **private copy**
    of the round doc; the second write-back to `in_memory_rounds`
    clobbers the first player's flag, so **both** callers get
    `{"status": "gave_up", "waiting_for_opponent": true}` and the round
    never resolves even though both players gave up. One of them must
    give up *again* to break the deadlock. Same missing-lock root cause
    as bug 2 (double-score race) and the same single-field-persistence
    family as bug 28. Pinned by
    `test_current_behavior_concurrent_hydrated_give_ups_lose_one_flag`
    (deterministic interleaving via a yielding `find_one`: player1's flag
    ends `False`, player2's `True`, `winner_id` stays `None`).

    *Fix:* the same as bug 2 — take `get_match_lock(match_id)` in
    `give_up_round` (and re-check the cache after the awaited hydrate),
    which also closes bug 28's erasure window.

### Independent rediscovery of bug 29

`test_status_should_report_round_winner_after_round_cache_eviction`
(strict xfail) and
`test_current_behavior_status_forgets_round_result_after_eviction`
pin the same root cause as bug 29 — `get_game_status` reads round fields
from `in_memory_rounds` only, with no `rounds_collection` fallback — this
time via the give-up/tie path: after a double give-up resolves the round
to `"tie"` and the round doc is evicted, the poll reports
`round_winner: null` and both gave-up flags `false` forever, while the
match-doc score survives, so the poller sees a board that contradicts
itself. Found independently while testing the `round_winner` value
shapes; kept as a second strict xfail so fixing bug 29 flips both.

### Passing findings worth knowing (current behavior, pinned by tests)

- **Give-up before any round is a clean 404** (`"No active round"`) on
  friend, ranked and even unjoined `waiting` matches — membership and
  heartbeat are processed first, so the failed call still marks the
  caller seen; no round state is created
  (`test_premature_give_up_leaves_no_round_state_behind`).
- **`already_ended` echoes any resolved round** — opponent's win, your
  own win, a `"tie"`, even the final round of a completed match (the
  champion can "give up" post-victory and just gets
  `{"status": "already_ended", "round_winner": <self>}` back). An
  `already_ended` give-up never touches the `*_gave_up` flags.
- **Concurrent give-ups on the shared in-memory round are safe**: with
  back-to-back execution one caller waits and the other resolves the
  tie; with a yielding DB write **both** callers receive the terminal
  `both_gave_up`/`"tie"` response (double resolution of the same value —
  harmless). The unsafe path is only the hydrated-copy race of bug 34.
- **Give-up racing a correct answer**: the answer wins the round and the
  giver still gets `gave_up`/waiting back although the round is already
  decided against them; the next status poll shows both the giver's flag
  and the answerer's `round_winner`
  (`test_give_up_racing_a_correct_answer_lets_the_answer_win`).
- **Status payload is shape-stable across all four lifecycle states** —
  the same 15 keys for waiting/active/completed/abandoned; `waiting`
  keeps the known `player2_id: "None"` quirk plus a "connected"
  never-seen ghost opponent; `abandoned` reports `round_start_time:
  null`; completed ranked matches report the positive `elo_change` while
  friend matches stay 0.
- **`round_winner` takes exactly three shapes** — a player id after a
  win, the literal string `"tie"` after a double give-up, `null` while
  undecided — and resets to `null` the moment the next round starts
  while the score persists.
- **Presence boundary is exact and per-player**: 12.000000s since the
  opponent's last heartbeat still reports `opponent_connected: true`,
  one microsecond more flips it to `false`, and one opponent poll flips
  it straight back. A player's own frantic polling refreshes only
  themselves (`test_own_polling_does_not_keep_opponent_connected`), and
  each status poll overwrites the caller's `player_last_seen` with the
  exact current timestamp — including on completed matches, where
  heartbeats are still recorded.
- **Give-ups are score- and ELO-neutral, always**: single give-ups,
  ties, and even four tie rounds in a row leave the match 0-0/active;
  a spy on `users_collection.update_one` confirms the give-up path never
  writes ELO or W/L in ranked matches. Combined with bug "no points for
  a stale-opponent tie" (presence suite) this means give-ups can stall
  but never finish a match.
- **Repeated give-up is idempotent** — the same `gave_up`/waiting body
  every time, the opponent's flag untouched; the opponent's single
  give-up (or the giver's retry once the opponent goes stale) still
  resolves the tie.
- **Ranked and friend give-up behavior is byte-identical** (single and
  tie responses compared across match types), and a connected human
  ranked opponent is never auto-mirrored by the bot branch (that
  requires the literal `"bot-opponent"` sentinel).
- **Both players stale, one gives up**: the caller's own staleness
  self-heals (the request marks them seen before the presence check), so
  only the opponent's staleness matters and the give-up auto-ties;
  the opponent's stale timestamp is left untouched.
- **Outsider hygiene**: 403 on status and give-up for active, completed
  and waiting matches, with no presence-map or round-doc pollution from
  the rejected calls. One latent quirk pinned as current behavior: the
  waiting-match membership check compares against `str(None) == "None"`,
  so an identity whose `_id` stringifies to `"None"` would be admitted
  to any waiting friend match
  (`test_current_behavior_identity_named_none_passes_waiting_membership`)
  — unreachable via HTTP today (guest ids always start with `"guest-"`,
  JWT ids are ObjectIds), so documented rather than xfailed.
- **While searching there is nothing to poll**: a queued player has no
  match id, guessing the next counter id 404s, `/api/game/active` says
  no match, and the searcher is a plain 403 outsider on other pairs'
  matches.

### How to run

```bash
# Give-up & status-polling suite only (56 tests: 54 pass, 2 xfail)
python3 -m pytest tests/test_match_giveup_status_edge_cases.py -q

# Full repository suite (900 tests: 863 pass, 37 xfail)
python3 -m pytest tests/ -q
```

Both xfails are `strict` with current-behavior sibling pins, matching the
campaign convention.

## Reconnect window & abandonment deep-dive (`/api/game/start`, `tests/test_match_reconnect_abandon_edge_cases.py`)

35 tests (34 pass, 1 strict `xfail`). A dedicated pass over the 5-second
reconnect window in `start_match` and every abandonment interaction it
has with people matches, going deeper than the boundary/lifecycle
coverage already in the ranked, presence, isolation, cancel and datetime
suites. Exact window ages are pinned to the microsecond with a frozen
clock (a `datetime` subclass monkeypatched over `main.datetime`, so the
naive `utcnow()` subtraction is deterministic). With this file the full
repository suite collects **935 tests** and runs as **897 passed, 38
xfailed**.

### New bug

35. **P1 — Abandonment is a memory-only mutation; Mongo keeps `active`
    and evicted abandoned matches resurrect**
    (xfail `test_abandonment_should_survive_memory_eviction`).
    The stale-match scan in `start_match` marks a >5s-old match abandoned
    by assigning `match["status"] = "abandoned"` on the in-memory doc —
    no `matches_collection.update_one` ever follows (verified with a spy:
    zero writes touch the abandoned match). The persisted doc stays
    `active` forever, so after a memory eviction / restart / worker
    switch, the hydrate paths (`status`, `question`, `answer`,
    `give-up`) reload the stale doc and the supposedly-dead match
    **resurrects as active for both players**, reappearing in
    `/api/game/active`. Combined with bug 8 (abandoned zombies remain
    playable) this makes abandonment cosmetic twice over: it neither
    stops play before an eviction nor survives one. Same
    never-persisted-mutation family as presence (`player_last_seen`)
    and the give-up flag findings (bugs 28/34). Pinned by
    `test_current_behavior_abandonment_never_written_to_db` (spy +
    fake DB doc still `active`) and
    `test_current_behavior_evicted_abandoned_match_resurrects_as_active`
    (status poll answers `active` post-eviction and `/api/game/active`
    advertises the match again).

    *Fix:* persist the transition where it happens — in the scan's
    abandon branch, `await matches_collection.update_one({"_id":
    match_id}, {"$set": {"status": "abandoned", "updated_at": ...}})`
    alongside the in-memory write (and consider batching if a caller can
    abandon several matches in one poll, see below).

### Window boundary (frozen clock, exact ages)

- The window is **strictly `match_age < 5`**: ages 0.000s and 4.999s
  reconnect; exactly 5.000s and 5.001s abandon the match and put the
  caller back in the queue. The boundary instant itself is already
  outside the window.
- A **future `created_at`** (clock skew) yields a negative age, which is
  `< 5` — the match is treated as brand new and reconnected, however far
  in the future the timestamp is.

### Multiple active matches: which one reconnects

- The scan iterates `in_memory_matches` in **insertion order** and
  returns on the first active `<5s` match involving the caller — the
  earliest-inserted match wins; later matches are never even looked at.
- One `/start` call can do **both jobs at once**: it abandons a stale
  first match as it walks past it, then reconnects into a still-recent
  second one.
- **Early-return quirk**: if the first match is recent and a LATER match
  is stale, the scan returns before reaching the stale one, which
  silently stays active — the user keeps two live matches.
- With **no recent match**, the scan walks the whole dict and abandons
  every stale active match the caller is part of in a single poll.

### continue_existing × friend/ranked matrix (8 combinations pinned)

- **Stale (>5s) matches**: `/start` always answers `"searching"`
  regardless of the flag; `continue_existing=True` never returns the old
  match (the quirk already documented in the ranked suite), it only
  decides whether the old match is abandoned (`False`) or silently left
  active (`True`). Identical for ranked and friend matches — the friend
  rows re-pin the `match_type`-blind scan (bug 7) from a new angle.
- **Recent (<5s) matches**: the reconnect branch fires **before**
  `continue_existing` is consulted, so both flag values reconnect —
  including the bug-7 hijack flavor where a ranked `/start` "reconnects"
  into a fresh friend match.

### Mid-round reconnects and terminal statuses

- **Reconnecting while the opponent is mid-round is lossless**: the
  round doc, `current_round_id` and the shared `round_start_time`
  countdown anchor all survive the `/start` poll, the opponent's
  in-flight answer still wins the round, and the reconnector can equally
  steal the open round with their own answer — reconnection neither
  resets nor forfeits the round.
- **Completed matches** are never reconnected (even <5s old) and never
  abandoned (the abandon branch only runs for `active`, so a >5s-old
  completed match keeps its terminal status); re-queueing pairs a fresh
  match with a fresh id and code.
- **Abandoned matches** are likewise skipped even inside the window; the
  search continues, and the same pair re-matches into a fresh match
  while the old one stays dead.

### Both players reconnecting

- Both players' `/start` polls (including interleaved A/B/A/B/A
  sequences, and both at the exact 4.999s boundary under the frozen
  clock) land in the **same** match — no duplicate matches, no
  re-queueing, queue empty throughout.

### ISO-string / missing created_at (beyond the existing datetime-suite pins)

- The **ISO-string `created_at` TypeError 500** (already pinned as the
  naive-created-at bug family in the datetime suite) has a precise blast
  radius: only the corrupted ACTIVE match's own two participants 500 —
  an outsider's matchmaking is untouched, and the same corrupted
  timestamp on a non-active (abandoned) match is harmless because the
  status check precedes the age math. `continue_existing=True` cannot
  dodge the 500 either: the subtraction happens before the flag is read.
- **Missing `created_at` is a permanent trap**: `match.get("created_at",
  datetime.utcnow())` re-defaults to the current time on EVERY scan, so
  the match is forever "0s old" — repeated `/start` polls (with
  `continue_existing=False`) always reconnect and the abandonment branch
  can never fire; the player cannot re-enter the queue until the match
  leaves `active` status (e.g. completion), at which point the scan
  skips it and searching resumes.

### How to run

```bash
# Reconnect/abandonment suite only (35 tests: 34 pass, 1 xfail)
python3 -m pytest tests/test_match_reconnect_abandon_edge_cases.py -q

# Full repository suite (935 tests: 897 pass, 38 xfail)
python3 -m pytest tests/ -q
```

The xfail is `strict` with two current-behavior sibling pins (DB-write
spy and post-eviction resurrection), matching the campaign convention.

## Match listing & history (`/matches/all`, `/match/{id}/details`, `tests/test_match_history_and_listing_edge_cases.py`)

33 tests (27 pass, 6 strict `xfail`). A dedicated pass over the read-side
"history" surface of people matches: the `/matches/all` listing, the
`/match/{id}/details` per-match history, and the corner of
`/api/leaderboard` that returns empty cleanly. Unlike the sibling suites,
`matches_collection` is backed by a small **Mongo-semantics emulator**
(real `sort`/`limit` on `find`, `$set`/`$push` and the positional
`rounds.$` operator on `update_one`, deepcopies at the driver boundary),
because both endpoints read **only from the DB** — they show exactly what
real Mongo would hold after the routes' `$push`/positional updates ran,
which is what surfaces the persistence bugs below. With this file the
full repository suite collects **968 tests** and runs as **924 passed,
44 xfailed**.

### New bug

36. **P1 — Post-tie round results and scores are never persisted; the
    listing/details history of any match containing a tie is frozen at
    the tie** (three xfails, all one root cause = bug 10's downstream
    blast radius, newly measured at the history surface):
    - `test_details_round_numbers_should_stay_strictly_increasing_after_tie`
      — after one tie the details rounds array reads `[1, 1]` (pinned by
      `test_current_behavior_tie_duplicates_round_number_in_rounds_array`).
    - `test_post_tie_round_results_should_be_persisted_to_history` — the
      round played after a tie is decided in memory, but the positional
      update filters on the round doc's count-based number (2), which no
      array element carries, so Mongo drops the winner, the winning
      answer **and the score `$set` bundled into the same update**
      (pinned by
      `test_current_behavior_post_tie_round_win_never_recorded_in_history`:
      details show the decided round as open, `player1_answer: null`,
      and the listing still says `0-0` while memory says `1-0`).
    - `test_completed_match_listing_score_should_match_final_result` —
      completion itself persists (plain `_id` filter), so after
      tie-then-3-wins the DB doc, the listing and details all report a
      **completed match with a winner and a `0-0` score** (pinned by
      `test_current_behavior_completed_match_after_tie_lists_stale_score`;
      `rounds_count` still counts 4 correctly because `$push` never
      misses).

    *Fix:* same one-liner as bug 10 — number the `$push`ed summary with
    the round doc's `round_count + 1`; every positional update then hits
    its element again and the score/winner/answer writes stop vanishing.

### Promotions / rediscoveries of known bugs (new xfails)

- **`/matches/all` requires no credentials and has no ownership filter**
  (`test_matches_all_should_require_credentials`, xfail
  `BUG(matches-all-open-history)`). Previously pinned as a passing quirk
  in the isolation suite (bug 21) — promoted to a strict xfail here per
  the security-pin convention, with fresh pins showing a headerless
  caller and any guest reading strangers' live scores.
- **The details history is a live answer oracle even for participants**
  (`test_details_should_not_reveal_unresolved_round_answer`, xfail
  `BUG(details-answer-oracle)`, same root cause as bug 1): mid-round the
  rounds array already carries `answer` and `derivative` of the open
  round, so either player can cheat from a second tab.
- **Abandonment never reaches the DB** (bug 35, rediscovered at the
  history surface): after an HTTP-triggered abandonment,
  `/api/game/status` says `abandoned` while `/matches/all` **and**
  `/match/{id}/details` (both DB-first) say `active`
  (`test_abandoned_match_should_be_listed_as_abandoned`, xfail, plus
  current-behavior pin). A directly-persisted `abandoned` doc IS listed
  verbatim, so the listing itself doesn't filter terminal states.

### Passing findings worth knowing (current behavior, pinned by tests)

- **Empty history is a clean `[]`** (authed and headerless), and so is
  an empty leaderboard. But the listing is **DB-only with no memory
  fallback** — with the DB down, a live playable match is in nobody's
  history (`test_matches_all_is_db_only_so_memory_matches_are_invisible`)
  — the exact inverse of details, which falls back to memory.
- **Details' memory fallback blanks round history**: round summaries are
  `$push`ed to Mongo only, so when the DB doc is gone the fallback
  serves the live score with `rounds: []` for a match that demonstrably
  played rounds
  (`test_current_behavior_details_memory_fallback_blanks_round_history`).
- **Limit/sort**: hard cap of 50, newest-first by `created_at`, the
  oldest entries silently fall off with no paging; datetime
  `created_at` is isoformat()ed while legacy string values pass through
  verbatim.
- **Sequential-play history is accurate (absent ties)**: 5 friend
  matches + 1 ranked + 1 live match list with correct per-match scores
  (`3-0`/`0-3`), statuses and `rounds_count`, newest first.
- **Completed details are complete**: ranked — `winner`, `score: "3-0"`,
  `elo_change == calculate_elo_change(1000, 1000) == 20`, per-round
  winners and answers; friend — winner set with `elo_change: 0`.
- **Friend vs ranked in listings**: `/matches/all` entries expose no
  `match_type` — friend, ranked and even bot matches are shape-identical
  (the only bot tell is the `"AI Opponent"` player2 label); details is
  the only place to distinguish (`friend`/`ranked`/`random`, and
  `"unknown"` for legacy docs missing the field). A waiting friend match
  is listed with a `"Player 2"` placeholder and details stringify the
  missing opponent as the known `id: "None"` quirk.

### How to run

```bash
# Listing & history suite only (33 tests: 27 pass, 6 xfail)
python3 -m pytest tests/test_match_history_and_listing_edge_cases.py -q

# Full repository suite (968 tests: 924 pass, 44 xfail)
python3 -m pytest tests/ -q
```

All six xfails are `strict` with current-behavior sibling pins, matching
the campaign convention.

## ObjectId ↔ guest-string id mixing (`tests/test_match_objectid_guest_mixing_edge_cases.py`)

35 tests (33 pass, 2 strict `xfail`). A dedicated pass over every seam
where a registered user's `ObjectId` `_id` meets a guest's plain string
id inside one people match: queue keys (`str(_id)` hex), `start_match`'s
`ObjectId(user_id) if ObjectId.is_valid(user_id) else user_id`
round-trip, the friend/challenge docs that store `current_user["_id"]`
raw, the `str()`-bridging gameplay ownership gates, the raw-comparing
challenge accept/cancel gates, and the ELO `$inc` targets on ranked
completion. Backed by a fake `users_collection` (resolves `_id` /
`email` / `username` queries, applies `$set`/`$inc` with **no upsert**,
and records every update filter so tests can assert *which id type* the
ELO writes carry) plus the flat-equality `matches_collection` fake for
the DB-only challenge routes. With this file the full repository suite
collects **1061 tests** and runs as **1005 passed, 56 xfailed**.

### New bug

37. **P3 — Challenge accept/cancel/pending have zero id-type bridging,
    so a challenge doc whose ids degraded to hex strings is permanently
    orphaned** (xfail `BUG(challenge-id-type-lockout)`,
    `test_hex_string_challenge_should_be_acceptable_by_its_objectid_invitee`,
    plus current-behavior pins). If `player2_id` is stored as the
    invitee's 24-hex *string* (JSON round-trip, backup restore, external
    writer) rather than the `ObjectId`, then for the genuine invitee's
    JWT: the pending list (`{"player2_id": ObjectId}` query) misses the
    doc entirely, accept raw-compares `ObjectId != "hex"` and returns
    403 "Not your challenge to accept" — yet the `str()`-bridging
    gameplay routes admit the very same credential to the very same
    match as player 2
    (`test_current_behavior_hex_string_challenge_locked_but_playable`).
    The mirror on the cancel side leaves the doc uncancellable by its
    own creator
    (`test_current_behavior_hex_string_creator_id_locks_out_cancel`).
    *Fix:* compare `str(match["player2_id"]) == str(current_user["_id"])`
    (and likewise for player1 in cancel), the same normalization every
    gameplay route already uses.

### Rediscoveries / deepenings of known bugs

- **Bug 5 deepened — the ObjectId pairing race also lies to both racers
  and mints a ghost match** (xfail `BUG(pairing-race)` deepened,
  `test_concurrent_objectid_joiners_should_not_both_report_matched`).
  The original xfail pins the double-pairing itself; the new one pins
  that BOTH concurrent joiners receive `status: "matched"` against the
  same single queued player. The current-behavior sibling
  (`test_current_behavior_double_pairing_leaves_a_ghost_match`) shows
  the fallout: the queued player sits in two active matches while
  `/api/game/active` only ever surfaces the first, so the second
  racer's match is an undiscoverable-but-playable ghost.
- **BUG(active-mislabels-humans-as-bot) rediscovered on ranked
  matches**: in a queue-paired human-vs-human match, the registered
  player's `/api/game/active` labels their guest opponent
  `"AI Opponent"` (raw-`_id` users lookup misses every guest), while
  the guest's own poll resolves the registered opponent's real username
  through the stored ObjectId
  (`test_active_match_resolves_objectid_opponent_but_mislabels_guest`).

### Current behavior worth knowing (asserted in passing tests)

- **The queue is all-strings; match docs are re-typed**: a registered
  user queues under their hex string, and pairing converts it *back*
  into a genuine `ObjectId` in the match doc, value-equal to the
  original. Guest ids stay strings; the two types sit side by side in
  one document (`player1_id: "guest-..."`, `player2_id: ObjectId`).
- **ELO snapshot asymmetry at pairing**: ObjectId opponents get a fresh
  live `users_collection` read (a rating change while queued is picked
  up), guests get the hard-coded 1000; the queue-time snapshot is
  ignored for both. The joiner is told `"Player"` when the opponent is
  a guest.
- **`cancelled_users` bridges types correctly**: cancel stores
  `str(_id)` and pairing checks `str(_id)`, so a registered user's
  cancel flag is honored across the type boundary.
- **Gameplay `str()` comparisons route everything correctly in mixed
  friend matches**: round wins credit the right side (winner stored as
  raw `ObjectId`, stringified to hex in every payload), give-up flags
  map the ObjectId caller to `player1_gave_up`, and a guest can win the
  match with `elo_change: 0` and zero `users_collection` writes.
- **Ranked ELO writes target the correct doc — when one exists**: the
  winner `$inc` filter carries a genuine `ObjectId` (never the hex
  string) and lands on the right user; the guest side's `$inc` targets
  the raw guest string, matches no doc, and (no upsert) silently
  evaporates — so a mixed ranked match applies rating changes to only
  one side, and a guest winner drains real ELO from an ObjectId loser
  while gaining nothing durable.
- **Challenge-by-username pins the invitee's ObjectId** next to a guest
  challenger's string; the real invitee accepts via raw
  `ObjectId == ObjectId`, the guest creator cancels via raw
  `str == str`, and the invitee cannot cancel (403).
- **A `"guest-<24hex>"` lookalike token stays a plain string
  everywhere**: `ObjectId.is_valid` rejects it (30 chars), so no
  coercion happens at pairing; the embedded hex being a strict suffix
  of the real user's id never cross-credits scores, never passes an
  ownership gate on the real user's matches, cannot accept their
  challenges, and is labeled `"Guest"` (not the registered username,
  not a bot) by the by-code endpoint.
- **Payload stringification is consistent across surfaces**: status,
  details and by-code all emit `str(ObjectId)` hex next to verbatim
  guest strings, resolve usernames only for the ObjectId side, and fall
  back to `"Player 1"`/`"Player 2"`/`"Guest"` for guests.

### How to run

```bash
# Id-mixing suite only (35 tests: 33 pass, 2 xfail)
python3 -m pytest tests/test_match_objectid_guest_mixing_edge_cases.py -q

# Full repository suite (1061 tests: 1005 pass, 56 xfail)
python3 -m pytest tests/ -q
```

Both xfails are `strict` with current-behavior sibling pins, matching
the campaign convention.

## Status gating matrix (`tests/test_match_status_gate_edge_cases.py`)

58 tests (48 pass, 10 strict `xfail`). A systematic pass over **which
match statuses allow which operations**: every status in {`waiting`,
`pending`, `active`, `completed`, `abandoned`} is crossed with every
mutating/reading match route, each cell asserted by a parametrized test.
Matches are seeded directly into both stores (in-memory + a FakeMatchesDB
stand-in, because the challenge endpoints are DB-only), so each cell
isolates the status check itself. With this file the full repository
suite collects **1026 tests** and runs as **972 passed, 54 xfailed**.

### The measured gate matrix (current behavior)

| route \ status | waiting | pending | active | completed | abandoned |
|---|---|---|---|---|---|
| `GET /api/game/question` | ALLOW ⚠ | ALLOW ⚠ | ALLOW | reject 400 | ALLOW ⚠ |
| `POST /api/game/answer` | ALLOW ⚠ | ALLOW ⚠ | ALLOW | reject 400 | ALLOW ⚠ |
| `POST /api/game/give-up` | ALLOW ⚠ | ALLOW ⚠ | ALLOW | ALLOW ⚠ | ALLOW ⚠ |
| `GET /api/game/status/{id}` | allow | allow | allow | allow | allow |
| `POST /api/game/friend/join` | ALLOW | reject 400 | reject 400 | reject 400 | reject 400 |
| `POST /api/challenges/accept` | reject 403 | ALLOW | reject 400 | reject 400 | reject 400 |
| `POST /api/challenges/cancel` | ALLOW | ALLOW | reject 400 | reject 400 | reject 400 |
| `POST /api/game/start` (reconnect) | searching | searching | reconnect | searching | searching |

⚠ = the status should be rejected but is not; each such cell carries a
strict xfail (`BUG(status-gate/question)`, `BUG(status-gate/answer)`,
`BUG(status-gate/give-up)`) alongside the passing current-behavior pin.

### Findings

- **The lifecycle routes gate correctly and strictly.** `friend/join`
  accepts only `waiting`, `challenges/accept` only `pending`,
  `challenges/cancel` only `waiting`/`pending`, and the `/start`
  reconnect scan only ever picks up `active` matches (everything else
  falls through to `"searching"` and leaves the seeded match untouched).
  One shape quirk: accepting a `waiting` match is rejected as **403**
  (the `player2_id is None` invitee check fires before the status
  check), not 400 like every other wrong-status accept.
- **The gameplay routes barely gate at all.** `question` and `answer`
  reject only `completed`; `waiting`, `pending` and `abandoned` matches
  serve rounds and score answers exactly like `active` ones. These are
  the matrix-level restatements of known bugs 8 (abandoned zombies), 9
  (pending challenges playable without accept) and 30 (creator
  solo-completes a `waiting` match, re-pinned here end-to-end 3-0
  against a `player2_id: None` opponent).
- **`give-up` has NO status check whatsoever** — the only route that
  does not even reject `completed`. A realistic completed match (final
  round has a winner) answers `200 {"status": "already_ended"}`, and a
  completed match with a still-open round processes a full give-up flow
  (`gave_up`, flag recorded, "waiting for opponent"), where
  question/answer on the same match return 400. Four xfail cells.
- **The status poll is open to every status by design** (read-only
  polling endpoint) — asserted allowed for all five statuses, no xfail.
- **Transitions flip the gates as expected**: `waiting → active →
  completed` (join + first-to-3: join/cancel/accept die at `active`,
  everything mutating dies at `completed` while polling still works),
  `pending → active` (accept is one-shot; cancel dies with it), and
  `active → abandoned` via the stale `/start` scan (no reconnect
  offered, join/cancel rejected, polling reports `abandoned` — but the
  flip is memory-only, re-pinning bug 35, and the zombie stays playable,
  bug 8).

### How to run

```bash
# Status-gate matrix suite only (58 tests: 48 pass, 10 xfail)
python3 -m pytest tests/test_match_status_gate_edge_cases.py -q

# Full repository suite (1026 tests: 972 pass, 54 xfail)
python3 -m pytest tests/ -q
```

All ten xfails are `strict` and sit next to passing parametrized pins of
the current behavior, matching the campaign convention.

## First-to-3 win completion deep-dive (`tests/test_match_win_completion_edge_cases.py`)

33 tests (32 pass, 1 strict `xfail`). A focused pass over the WIN
COMPLETION mechanics of people-vs-people matches: every reachable
completion scoreline, winner attribution, the one-shot nature of the
`completed` transition, post-completion lockout, ELO single-application,
the match-point concurrency race, and post-completion discoverability /
rematch hygiene. With this file the full repository suite collects
**1094 tests** and runs as **1037 passed, 57 xfailed**.

### What was covered

- **All completion scorelines, both match types**: 3-0, 3-1 and 3-2 are
  played out round-by-round for friend AND ranked matches (parametrized
  6 ways); the final answer flips `status` to `completed` and stamps
  `winner_id` and both scores consistently in the response, the
  in-memory doc, and the status poll.
- **Player2 wins are attributed correctly**: a 0-3 sweep by the joiner
  sets `winner_id` to player2 (never player1) in the answer response,
  the match doc, and both players' status views, for friend and ranked.
- **`completed` is written exactly once**: a `matches_collection` spy
  sees exactly one `$set {status: "completed"}` per match, carrying
  `winner_id`, `elo_change` and `updated_at` under the right `_id`
  filter (ranked persists the real ELO delta, friend persists 0); the
  rejected post-completion answers/questions and repeated status polls
  never re-issue it.
- **Post-completion lockout**: after the deciding answer, further
  `POST /api/game/answer` (correct OR wrong — the gate fires before
  grading) and `GET /api/game/question` return
  `400 "Match is already completed"` for BOTH players on both match
  types, and none of that traffic disturbs the stored result.
- **ELO exactly once on ranked, never on friend** (users_collection
  spy): completion issues exactly two `$inc` updates (winner
  `+elo/wins`, loser `-elo/losses`), zero before the deciding round,
  zero more on post-completion retries; friend matches complete with
  zero users_collection calls even across repeated polls.
- **Win via the last answer at 2-2**: alternating rounds to 2-2 then a
  deciding fifth round completes at 3-2 for either seat (ranked pays
  20, friend pays nothing).
- **elo_change magnitude mirrors the stored snapshots**:
  `calculate_elo_change` on the match's `player1_elo`/`player2_elo`
  snapshots — 20 for the even 1000v1000 guest case, 36 for an underdog
  win over a 1400 snapshot, 3 for the favorite — and the loser's `$inc`
  is the exact negation. Both players see the same winner-perspective
  magnitude via status; friend matches report 0 everywhere.
- **`/api/game/active` flips off**: both players see
  `has_active_match: true` (with the right match id) mid-match and a
  bare `{"has_active_match": false}` immediately after completion, for
  friend and ranked.
- **Rematches start clean**: after a completed ranked match, a fresh
  queue+join forms a NEW match id with 0-0 scores, `winner_id: None`,
  `elo_change: 0`, no reconnect into the finished match, and the old
  result stays frozen while the rematch scores independently; same for
  a fresh friend create/join cycle (opposite winner, both results
  stand, still zero users_collection writes).

### New bug found

- **Match-point DB-reload race double-completes the match and pays ELO
  twice** (strict xfail `BUG(match-point-db-race-double-elo)`,
  `test_race_at_match_point_via_db_reload_should_pay_elo_once`, with a
  passing current-behavior pin). This deepens known P0 bug 2 (the
  lock-free double-score race) at its most damaging moment: with both
  players at 2 points and the round doc re-read from the DB (memory
  miss + any latency), both submitters pass the `completed` and
  `winner_id` checks on their own copies, both score, and both execute
  the completion block. Result: an impossible 3-3 scoreline, FOUR
  users_collection `$inc` updates instead of two (winner +40 net and
  +2 wins, loser -40 net and +2 losses on 1000v1000 snapshots), the
  `completed` status written twice — and both completions credit
  player1, because `player1_score >= 3` is checked first, so player2's
  own match point evaporates even in the response handed to player2.
  *Fix:* take the per-match `asyncio.Lock` (already used by
  `get_question`) around the read-check-write span of `submit_answer`.

### Current behavior worth knowing (asserted in passing tests)

- **The same race is safe when the round doc is in memory**: there is
  no suspending await between the completed-status check and the
  round/score writes, so the first match-point submitter runs to
  completion and the second bounces off the completed gate with a 400
  — exactly one completion, exactly two `$inc` updates.
- **The post-completion 400 gate is the only thing standing between a
  completed match and re-scoring** — it fires before grading, so even
  malformed answers cost nothing after completion.

### How to run

```bash
# Win-completion suite only (33 tests: 32 pass, 1 xfail)
python3 -m pytest tests/test_match_win_completion_edge_cases.py -q

# Full repository suite (1094 tests: 1037 pass, 57 xfail)
python3 -m pytest tests/ -q
```

The xfail is `strict` with a passing current-behavior sibling pin,
matching the campaign convention.

## Mongo hydrate & fallback paths (`tests/test_match_mongo_hydrate_edge_cases.py`)

Findings from `tests/test_match_mongo_hydrate_edge_cases.py`
(26 tests: 24 pass, 2 xfail). conftest's default mocks answer `None` to
every `find_one`, so the rest of the campaign mostly exercises the pure
in-memory paths. This suite backs `matches_collection` and
`rounds_collection` with stateful fakes (`FakeMatchesDB` /
`FakeRoundsDB`, the challenge/history-suite pattern: deepcopies at the
driver boundary, flat-equality plus positional `rounds.round_number`
filters, `$set` incl. `rounds.$.` and `$push` updates, and a
`find_one_calls` counter) so the DB can actually *return* documents, and
walks every hydrate branch for people matches.

### Bugs (xfail, both strict re-pins of known bugs)

- **`status` hydrates the match but never the round** — re-pin of
  `BUG(status-no-round-hydration)` (bug 2 of the newly-found-bugs
  suite) from the DB-seeded side: with both docs in Mongo and neither
  in memory, the poll serves the match half fine (`player1_score`
  hydrated) but reports the resolved round's winner as `None`, and the
  `find_one_calls` counter proves the rounds collection receives
  **zero** reads (`test_status_should_hydrate_current_round_from_db`).
- **`/api/game/match/{code}` performs zero DB reads** — re-pin of
  `BUG(by-code-no-db-fallback)`: the by-code route 404s on a DB-only
  match and the counter shows it never even queried
  `matches_collection`
  (`test_by_code_should_hydrate_match_from_db`).

### The hydrate matrix (asserted in passing tests)

| Route | Match doc | Round doc | Cached back? | Writes back? |
|---|---|---|---|---|
| `question` | hydrates | never (re-creates instead, bug 11) | yes | round insert + `current_round_id`/`$push` on the match |
| `answer` | hydrates | hydrates | yes (both) | winner/answer to rounds, score + positional `rounds.$` to matches |
| `give-up` | hydrates | hydrates | yes (both) | give-up flags (and tie) to rounds |
| `status/{id}` | hydrates | **never** (xfail) | match only | presence only (memory) |
| `friend/join` | hydrates by code | n/a | **no** | join written to the DB doc only |
| `friend/status/{code}` | hydrates by code | n/a | **no** | nothing (read-only) |
| `match/{code}` | **never** (xfail) | n/a | no | nothing |
| `active` | **never** (memory scan only, zero reads) | n/a | no | nothing |

- A DB-only waiting friend match is fully joinable (lower-case code
  upper-cased, join written back to the DB doc), and the whole flow then
  works DB-first: `join` → `question` hydrates the now-active doc and
  serves the same round 1 to both players → `answer` scores. But `join`
  and `friend/status` are hydrate-for-the-request only: neither caches
  the doc into `in_memory_matches`, unlike question/answer/give-up/
  status. `/api/game/active` reports `has_active_match: false` for a
  DB-only active match (zero DB reads) until any hydrating route runs,
  after which it flips to `true`.
- Hydration happens **before** the status gate: probing a completed
  DB-only match with `question` 400s but still pulls the dead match
  into the memory cache.
- Give-up on a hydrated already-resolved round short-circuits to
  `already_ended` without mutating either store.

### Corrupted / oversized documents

- **Missing load-bearing fields 500 and poison the cache** (bug 28): a
  match doc missing `player2_id` or `player1_elo` (question), scores or
  `status` (status), or a round doc missing `answer` (answer) crashes
  the request with a `KeyError` 500 — *after* the doc was cached, so
  every subsequent request re-crashes off the cached copy without ever
  re-reading the (possibly repaired) DB.
- **Write ordering strands a half-created round** (bug 29): on a match
  doc missing `player1_score`, `question` 500s only after the round was
  stored in memory *and* inserted into `rounds_collection`; the match
  doc never learns about it (`rounds` array never pushed).
- `status` tolerates a *minimal* doc — identity, scores and `status`
  are the only hard requirements; `winner_id`/`elo_change`/
  `round_start_time`/`current_round_id` all default cleanly through
  `.get`.
- **Extra unexpected fields are harmless and preserved**: legacy blobs,
  unknown ids and nested junk ride along into the memory cache
  untouched and never leak into the response shape (status response
  keys stay exactly the documented 15).

### Memory-vs-DB precedence and write-backs

- **After one hydrate read, memory wins forever**: diverge the cached
  doc (score 2) from the DB doc (score 5) and the poll serves memory;
  the `find_one_calls` counter stays at 1 — the DB is never re-read.
- **Write-backs after a full hydrate land in both collections**: a
  correct answer on a match+round that lived only in Mongo writes the
  round winner and answer to the rounds doc *and* the score plus the
  positional `rounds.$` entry to the match doc (the seeded doc carries
  a real `rounds` array, so the positional update applies exactly as
  in Mongo). A wrong answer durably records the attempt while leaving
  the round open.
- **Membership is enforced before any write-back**: an outsider probing
  a DB-only match gets 403 on all four gameplay routes and neither
  collection sees a single mutation.

### How to run

```bash
# Hydrate/fallback suite only (26 tests: 24 pass, 2 xfail)
python3 -m pytest tests/test_match_mongo_hydrate_edge_cases.py -q

# Full repository suite (1120 tests: 1061 pass, 59 xfail)
python3 -m pytest tests/ -q
```

Both xfails are `strict` re-pins of already-catalogued bugs
(`status-no-round-hydration`, `by-code-no-db-fallback`), each with a
passing current-behavior sibling pin, matching the campaign convention.

## Miscellaneous people-match edges (`tests/test_match_misc_people_edge_cases.py`)

Findings from `tests/test_match_misc_people_edge_cases.py` (24 tests: 24
pass, 0 xfail). A sweep of the remaining niche edges no dedicated suite
owned: response link formats, the `MatchStart.continue_existing` default,
guest username storage, match-code alphabets, RNG determinism, the
lower-ELO difficulty rule, mid-game cache eviction, and cross-feature
non-interference (leaderboard, daily challenge, `/api/user/me`).

### Rediscovered known bug (current-behavior pin, no new xfail)

- **Aware `created_at` 500s `/api/game/start`** — bug 12's TypeError
  rediscovered from the error-handling side
  (`test_rediscovered_aware_created_at_500s_start_with_generic_detail`):
  an active match carrying a tz-aware `created_at` makes the reconnect
  window's naive `datetime.utcnow()` subtraction raise `TypeError`,
  which the global handler converts to the generic
  `"Something went wrong. Please try again."` 500. The pin adds the
  handler-side blast radius: **no internals leak** (`TypeError` /
  traceback text absent from the body), the failure is **persistent
  across retries**, and the poisoned match itself is left untouched
  (never abandoned, never matched). The should-reconnect xfail lives in
  the datetime suite; this suite adds only passing pins.

### Quirks pinned (passing tests)

- **Friend share links are hardcoded to localhost** — the create
  response's `link` is exactly
  `http://localhost:3000/play/friend/{match_code}` (identical for open
  invites and named challenges); no env-driven origin exists, so a
  production share link points at a dev frontend.
- **`continue_existing=True` never returns the old match** — it only
  *suppresses the abandonment*: the stale active match stays `active`
  in memory, but the caller is still queued and told `"searching"`.
  Omitted, explicit `false` and JSON `null` all behave identically
  (stale match → `abandoned`, caller queued); on the model, the default
  is `False` while an explicit `null` is preserved as `None` (falsy).
- **Named challenge to an unknown username degrades silently** — no
  user matches, so `player2_id` stays `None` and status is `"waiting"`
  (an open code invite, not `"pending"`), yet the unmatched username is
  still written onto the doc as `player2_username`.
- **Guest usernames in match docs**: `player1_username` falls back
  through `.get("username", name)` to the synthetic `"Guest xxxx"`
  display name (never a literal `None`); an open invite stores
  `player2_username: None` and `join` **never backfills it**. The
  status poll ignores both stored names anyway — guests aren't in
  `users_collection`, so it serves `"Player 1"` / `"Player 2"`.
- **Two code alphabets**: friend codes are exactly 6 chars of
  `A-Z0-9` (already uppercase as stored); ranked *and* bot-fallback
  codes are `secrets.token_urlsafe(8)` → 11 URL-safe base64 chars
  (`A-Za-z0-9_-`). Since the friend-join route upper-cases its input,
  a mixed-case ranked code could never be joined by code.
- **Bot identity is `random`-seeded, not `secrets`** — seeding the
  module-level RNG reproduces the exact same bot name *and* ELO offset
  across a full state wipe; the roster and the −150…−50 offset range
  hold as documented in the bot-fallback suite.
- **Difficulty always follows the lower ELO** — `generate_question`
  receives `min(player1_elo, player2_elo)` whichever slot holds the
  weaker player (2000/800 and 800/2000 both yield 800; 1500/1500 yields
  1500), asserted via a generator spy.
- **Mid-game eviction is survivable end-to-end** (FakeDB-backed): with
  the match doc deleted from memory mid-round, the next `question`
  hydrates the persisted doc — whose written-back `current_round_id`
  resolves to the still-cached round — and the opponent resumes the
  *same* round (id, expression, byte-identical `round_start_time`),
  then scores normally with write-backs landing in the DB doc. Evicted
  *after* a scored round, the status poll hydrates with the score
  intact and the next question rolls forward to round 2 (the persisted
  `rounds` array grows to 2) instead of replaying round 1.
- **Cross-feature non-interference**: `/api/leaderboard` returns 200
  (empty, guests never persist) before and after a completed ranked
  match; a full daily-challenge fetch+submit mid-match leaves
  `in_memory_matches`, `in_memory_rounds` and the queue deep-equal to
  their snapshots and the match fully playable; `/api/user/me` during
  an active match serves the stable guest identity (exactly
  `{id, email, name, username, elo}`, `username: None`, `elo: 1000`) —
  and still reports 1000 for both players *after* a ranked win pays out
  a positive `elo_change`, because guest identities are rebuilt from
  the token on every request.

### How to run

```bash
# Miscellaneous edges suite only (24 tests, all passing)
python3 -m pytest tests/test_match_misc_people_edge_cases.py -q

# Full repository suite (1144 tests: 1085 pass, 59 xfail)
python3 -m pytest tests/ -q
```

No new xfails: the one bug touched here (aware `created_at` → 500) was
already catalogued as bug 12 and is pinned from a new angle by passing
current-behavior tests.

## Audit pass 2 (`tests/test_match_audit_pass_2_edge_cases.py`)

A second full audit read of the match code in `main.py` (start_match,
submit_answer including the inline SymPy grading cascade, get_question,
give_up_round, presence, queue lifecycle), diffed line-by-line against the
existing suites and the numbered bug list above. Four genuinely new,
previously untested bugs were found; each was verified end-to-end through
the real endpoints before being pinned. 16 tests (12 pass, 4 strict
`xfail`). With this file the full repository suite collects **1160 tests**
and runs as **1097 passed, 63 xfailed**.

### New bugs (severity-ranked)

38. **P2 — `submit_answer` never enforces the 5-minute PvP round expiry**
    (strict xfail `BUG(answer-ignores-round-expiry)`,
    `test_answer_on_expired_round_should_not_score`, plus three passing
    pins). `get_question` voids any round older than 300s as a tie, and
    the bot branch of `submit_answer` forfeits at `time_limit` — but the
    people-vs-people answer path has **no expiry check at all**. A correct
    answer submitted hours late still wins the round
    (`test_current_behavior_hours_old_round_answer_still_wins`), and at
    match point it completes a ranked match and pays real ELO on a round
    the very next `get_question` would have voided
    (`test_current_behavior_expired_round_completes_ranked_match_and_pays_elo`).
    Which of "point" vs "void" a stale round becomes depends solely on
    which endpoint touches it first
    (`test_current_behavior_question_would_have_tied_the_same_round`).
    Exploit: sit on a question indefinitely (only poll `status`, never
    re-request `question`) and submit whenever the answer is worked out.
    *Fix:* run the same 300s `parse_round_start` check at the top of
    `submit_answer` and tie the round instead of grading.

39. **P2 — The numeric grading fallback accepts mathematically wrong
    answers within 1e-6** (strict xfail `BUG(numeric-fallback-epsilon)`,
    `test_answer_off_by_a_tiny_constant_should_be_rejected`, plus pins).
    When every symbolic check fails, `submit_answer` samples 5 points from
    `uniform(1, 10)` and flips `correct = True` if the values agree within
    an **absolute 1e-6**. `2*x + 1e-7`, `2*x + 0.0000001` and
    `2*x*(1+1e-9)` are all deterministically accepted for `2·x` — even
    though `simplify` and `.equals` both correctly refuted them moments
    earlier (the fallback overrides the symbolic verdict). The cliff is
    pinned from both sides: `2*x + 2e-6` is still rejected
    (`test_current_behavior_just_above_tolerance_is_still_rejected`).

40. **P3 — The numeric fallback samples only x ∈ (1, 10), so
    positive-axis lookalikes grade as correct** (strict xfail
    `BUG(numeric-fallback-positive-domain)`,
    `test_abs_answer_should_be_rejected_for_polynomial_derivative`, plus
    pins). Both spellings of 2|x| — `2*abs(x)` and `2*sqrt(x^2)` — are
    accepted for `2·x` although they are wrong for every x < 0, because
    every sample point the fallback can draw is positive
    (`test_current_behavior_positive_axis_lookalikes_are_accepted`). The
    contrast pin shows `-2*x` (wrong *on* the sampled interval) is caught
    by the same fallback, isolating the hole to the sign of the domain.
    *Fix for 39+40:* don't let the numeric fallback override a symbolic
    refutation; if kept, use a relative tolerance and sample both signs.

41. **P2 — The `/api/game/start` reconnect branch never dequeues the
    caller, deterministically pairing the next searcher into a ghost
    match** (strict xfail `BUG(reconnect-leaves-queue-entry)`,
    `test_reconnect_should_remove_the_callers_queue_entry`, plus two
    passing pins). Only the pairing branch pops `matchmaking_queue`; the
    <5s reconnect return does not. Realistic sequence: A queues for
    ranked ("searching"), a friend joins A's open friend match (now
    active and fresh), A's next ranked poll hits the reconnect branch and
    is handed the friend match (the bug-7 hijack window) — while A's
    queue entry stays live. The next ranked searcher C is then paired
    against A into a "ranked" match A never learns about: A's
    `/api/game/active` surfaces only the earlier friend match
    (insertion-order scan), so C waits against an absent opponent
    (`test_current_behavior_leftover_queue_entry_pairs_a_ghost_match`)
    and can solo-play the ghost to a completed 3-0
    (`test_current_behavior_ghost_match_is_playable_solo_by_the_newcomer`).
    Unlike the known hour-stale-queue-entry finding (stress suite), this
    needs no staleness at all — the entry is seconds old and the pairing
    is deterministic. *Fix:* `matchmaking_queue.pop(user_id, None)` in
    the reconnect return path (and arguably whenever the scan finds any
    active match for the caller).

### What was checked and found already covered

The pass also traced: `continue_existing` semantics (pinned, misc +
reconnect suites), unknown-username challenge degradation (pinned),
give-up vs answer races (pinned), hydrated give-up flag erasure (bug 28),
queue-ELO snapshot quirks (pinned in the ELO suite), reconnect opponent
labeling (pinned), `match_locks` leaks (cancel suite), bot `time_limit`
boundary semantics (bot suite), and the finite-difference grading cheat
(rejected by float cancellation — not a bug). Those produced no new
entries.

### How to run

```bash
# Audit pass 2 suite only (16 tests: 12 pass, 4 xfail)
python3 -m pytest tests/test_match_audit_pass_2_edge_cases.py -q

# Full repository suite (1160 tests: 1097 pass, 63 xfail)
python3 -m pytest tests/ -q
```

All four xfails are `strict` and sit next to passing current-behavior
pins, matching the campaign convention.

## Audit pass 3: deeper coverage of the grading/expiry bugs (38-41)

A third pass took the four bugs pinned in audit pass 2 and widened each
one along the axis pass 2 left open, rather than re-pinning the same
cases. The work lives in `tests/test_match_grading_expiry_deep_edge_cases.py`:
**41 tests (23 pass, 18 strict `xfail`)**. No genuinely new bug number was
needed — every finding deepens 38-41 — but one previously-unpinned
*consequence* of bug 41 is called out below (a real ELO transfer to an
absent player). With this file the full repository suite collects **1201
tests** and runs as **1120 passed, 81 xfailed**.

### What each area added (report numbers 38-41)

38. **PvP answer path ignores the 300s round expiry — broadened.** Pass 2
    pinned 301s and 7200s; pass 3 parametrizes the "still accepted"
    behavior across **301s / 600s / 3600s**
    (`test_pvp_answer_after_expiry_should_void` strict xfail +
    `test_current_behavior_pvp_answer_after_expiry_still_wins` pins) and
    brackets the boundary itself. `get_question` uses a strict `>300s`
    cutoff, so a round aged 299s is re-served live while a 301s round is
    voided as a tie — yet an equally-aged round is still fully scorable
    through `submit_answer`
    (`test_boundary_answer_and_question_should_agree_just_past_300s` strict
    xfail; `test_current_behavior_boundary_question_voids_but_answer_scores`
    pins both sides of the ~300s line).

38 × ELO. The most damaging interaction gets its own strict xfail:
    at match point a correct answer on an expired (3600s) deciding round
    completes the ranked match and pays a full ±20 ELO swing
    (`test_late_answer_at_match_point_should_not_pay_elo` xfail vs
    `test_current_behavior_late_answer_at_match_point_pays_elo` pin). A
    round the next `get_question` would have voided should not settle a
    ranked match.

39. **Numeric fallback's absolute 1e-6 cliff — swept.** Pass 2 pinned
    three near-misses; pass 3 sweeps the cliff from both sides with
    deterministic *constant* offsets: `2*x + {1e-7, 5e-7, 9e-7, 9.9e-7}`
    are all accepted (below tolerance) and `2*x + {1.1e-6, 2e-6, 1e-5,
    1e-3}` are all rejected (above it), bracketing 1e-6 tightly
    (`test_sub_tolerance_offsets_should_be_rejected` strict xfail +
    `test_current_behavior_sub_tolerance_offsets_win` /
    `test_current_behavior_above_tolerance_offsets_rejected` pins). A
    separate "symbolic reject then numeric accept" section covers
    *polynomial* near-misses whose error scales with x but stays under
    1e-6 across the whole (1, 10) window — `(2+1e-8)*x`, `2*x + 1e-8*x`,
    `2.0000001*x`, `(2+1e-9)*x` — all refuted by `simplify` yet accepted by
    the fallback (`test_polynomial_near_misses_should_be_rejected` strict
    xfail + `test_current_behavior_polynomial_near_misses_win` pin).

40. **Positive-axis lookalikes — beyond abs/sqrt.** Pass 2 pinned
    `2*abs(x)` and `2*sqrt(x^2)`; pass 3 broadens the family to `Abs(2*x)`,
    `sqrt(4*x^2)`, `x + Abs(x)` (which is 2x for x>0 and 0 for x<0), and
    `2*Max(x, -x)`. Every one agrees with 2·x on the sampled positive axis
    and disagrees for some x<0, so all grade correct
    (`test_positive_axis_lookalikes_should_be_rejected` strict xfail +
    `test_current_behavior_positive_axis_lookalikes_win` pins), while the
    contrast pin shows `-2*x` (wrong on the sampled interval) is still
    caught (`test_current_behavior_negative_only_disagreement_still_rejected`).

41. **Reconnect leaves the queue entry — full exploit to a real ELO
    transfer.** Pass 2 pinned the ghost pairing and that the newcomer can
    solo-play it. Pass 3 follows the exploit to its damaging end: the
    stranded newcomer C solo-plays the ranked ghost to 3-0, the match
    *completes and pays a real ±20 ELO swing*, charging the absent player A
    a ranked **loss on a match A was never even shown** by
    `/api/game/active`
    (`test_current_behavior_ghost_completion_pays_real_elo_to_absent_player`
    asserts the `{"elo": -20, "losses": 1}` write is keyed to A). A
    distinct strict xfail states the post-fix invariant — with the
    reconnect branch dequeuing the caller, the next ranked searcher should
    stay `"searching"` rather than be ghost-paired
    (`test_next_searcher_should_stay_searching_not_ghost_paired`). The ELO
    charge to an absent participant is the new, previously-unpinned
    consequence surfaced this pass; the *fix* is still the one-line
    `matchmaking_queue.pop(user_id, None)` in the reconnect return path.

### How to run

```bash
# Audit pass 3 suite only (41 tests: 23 pass, 18 xfail)
python3 -m pytest tests/test_match_grading_expiry_deep_edge_cases.py -q

# Full repository suite (1201 tests: 1120 pass, 81 xfail)
python3 -m pytest tests/ -q
```

All 18 xfails are `strict` and sit next to passing current-behavior pins,
matching the campaign convention.

## Deep lock/concurrency suite (`tests/test_match_lock_concurrency_deep_edge_cases.py`)

A dedicated deepening pass on concurrency around the ONE lock the match
code has (`match_locks` / `get_match_lock`, used only by `get_question`)
and the mutating endpoints that don't take it (`submit_answer`,
`give_up_round`, `join_friend_match`, the queue endpoints). **30 tests
(28 pass, 2 strict `xfail`)**, both xfails pinning one genuinely new bug
(numbered **42** below). With this file the full repository suite collects
**1231 tests** and runs as **1148 passed, 83 xfailed**.

Conventions as everywhere in the campaign: "simultaneous" requests are
route coroutines gathered on one event loop (single-uvicorn-worker
semantics); DB latency is a monkeypatched Motor method that awaits
`asyncio.sleep` before returning a deep-copied snapshot, which is exactly
the shape of a real Mongo round-trip.

### New bug

42. **P1 — Hydration write-back after a concurrent resolution reopens a
    decided round; points per round are unbounded** (two strict xfails,
    one root cause). Both `submit_answer` and `give_up_round` hydrate a
    cache-missed round with `in_memory_rounds[round_id] = round_doc`
    *before* looking at it. Any hydration that returns after a concurrent
    call resolved the round clobbers the recorded `winner_id` with its
    stale winnerless copy — and unlike the known double-score race (bug 2)
    or the give-up flag race (bug 34), the clobbering request doesn't have
    to score at all:
    - a **losing player's WRONG answer** racing the winner's correct one
      erases the winner and reopens the round; the winner then scores the
      SAME round again — two points (and counting) from one round
      (`test_late_wrong_answer_should_not_reopen_a_decided_round` xfail,
      `test_current_behavior_late_wrong_answer_reopens_the_round_for_a_second_win`
      pin);
    - a **give-up** racing a correct answer does the same: the answer's
      point sticks on the scoreboard but the round doc ends winnerless
      with only the quitter's flag, and the answerer re-wins it for a
      second point
      (`test_concurrent_give_up_should_not_erase_the_answers_round_winner`
      xfail,
      `test_current_behavior_give_up_write_back_reopens_the_scored_round`
      pin).
    *Fix:* the same one for bugs 2/34/42 — take `get_match_lock(match_id)`
    in `submit_answer` and `give_up_round`, and never overwrite a cached
    round that already has a `winner_id` with a hydrated copy that doesn't.

### Known bugs deepened (current-behavior pins, no new numbers)

- **Bug 2 (double-score race), three latency variants.** Symmetric
  hydration latency mid-match pays BOTH players for one round (1-1 from a
  single round, far from the match-point/ELO framing of the win suite).
  *Staggered* latency — the second read returns only after the first racer
  fully scored and returned — still double-pays, and the surviving round
  doc credits only the SLOW racer (the fast racer's acknowledged win
  exists nowhere but in the score). And the *same player's* duplicated
  request (double-click/retry) scores twice: 2-0 from one round.
- **Bug 34 (give-up lost update), staggered variant.** Player A's give-up
  fully completes and is acknowledged before B's hydration even returns —
  no interleaved flag writes at all — yet B's stale write-back still
  erases A's flag. The erasure window is the whole hydration latency, not
  a simultaneous write race. Recovery pin: the round stays stuck until the
  erased player gives up a second time.
- **Bug 4 (join race), overwrite variants.** A FOUR-way concurrent join
  race: all four get `200/active`, and the seat (id *and* the `player2_elo`
  snapshot) belongs to whichever coroutine wrote last. The first
  acknowledged joiner is then locked out with 403 "Not your match" while
  the unauthenticated by-code status keeps telling them the match is
  active — they have no way to learn they were kicked. The self-join guard
  is the one check that survives the race: a creator racing a real joiner
  is still rejected with 400.
- **Bug 6 (stale cancel flag), concurrent framings.** Cancel *winning* the
  race against a searcher's start re-queues cleanly (but still leaves the
  flag). Cancel *losing* it cannot unwind the pairing: the match stands,
  the flag lingers, and the canceller's next poll silently reconnects them
  into the match they tried to leave (the reconnect branch consumes no
  flags). A same-tick start+cancel from one user leaves a clean queue but
  plants the flag that eats the NEXT pairing, silently dequeuing the
  innocent opponent too. And when BOTH players cancel one tick after
  being paired, both stay stuck in the match with both flags primed.

### Lock behavior verified (passing tests)

- **The lock works where it exists.** A ten-caller `get_question`
  stampede whose round creation yields mid-write (insert + match-doc
  update both suspend inside the critical section) produces exactly one
  round, one shared `round_start_time`, one expression. Two players
  hitting the 300s expiry simultaneously tie round 1 exactly once and
  both land on the single round 2. A six-shot same-player retry burst
  with slow writes collapses to one round.
- **Same lock object, proven behaviorally.** `get_match_lock` returns the
  identical object across calls, rounds, give-up ties, status polls and
  completion; manually `acquire()`-ing the stored object stalls a live
  `get_question` task (nothing created) until `release()` — the endpoint
  really serializes on THAT object, not a per-call one.
- **`match_locks` only ever grows** (bug 22 family). Twelve questioned
  matches ⇒ twelve distinct locks; completing a match, abandoning another
  through the stale-match scan, even deleting the match doc from
  `in_memory_matches` (cache eviction) all leave every lock in place, and
  rehydration reuses the same object. Nothing ever evicts a lock; the
  dict is unbounded in production.
- **Status polls are read-mostly-safe but tear.** A 16-poll storm with
  yielding user lookups leaves match and round state byte-identical
  (only the heartbeat map changes); polls interleaved with a locked
  round-creation serve a coherent payload of the half-created round. But
  `get_game_status` reads round info BEFORE its awaited user lookups and
  scores AFTER them, so a poll racing a winning answer serves a **torn
  payload — the new score with the old `round_winner: null`**
  (`test_status_poll_racing_a_win_serves_a_torn_payload`); the next poll
  is consistent again. Benign but visible as a one-frame UI glitch.
- **The lockless answer path can interact with the locked question
  path.** Right after a round win, a player's next-question poll racing
  their own (retried) answer lets the answer **blind-snipe the freshly
  created round** — won before the client ever saw the question and
  before its synchronized `round_start_time` (≈3s in the future) ever
  arrived. With the match-doc write yielding while the lock is held, the
  answer lands mid-creation and the question response hands out an
  already-decided round. The opposite ordering is clean: the answer
  bounces off the previous round's winner gate (`already_won`) and the
  question rolls a fresh winnerless round 2.

### How to run

```bash
# Deep lock/concurrency suite only (30 tests: 28 pass, 2 xfail)
python3 -m pytest tests/test_match_lock_concurrency_deep_edge_cases.py -q

# Full repository suite (1231 tests: 1148 pass, 83 xfail)
python3 -m pytest tests/ -q
```

Both xfails are `strict` (bug 42) and sit next to passing
current-behavior pins, matching the campaign convention.
