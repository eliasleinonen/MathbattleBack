# Match Edge Case Campaign Report

## Executive summary

### What was tested

Every people-vs-people match flow in the backend: ranked matchmaking
(queue, cancel, reconnect, bot fallback), friend matches (create/join by
code, challenges to a named user), the challenge accept/cancel endpoints,
presence tracking and give-up resolution, question serving and round
progression, answer grading and first-to-3 win conditions, ELO calculation
and payout, datetime/timezone handling, in-memory-vs-Mongo state
divergence, and cross-match isolation / access control. Concurrency races,
DB-unavailable fallbacks, cache eviction/restart scenarios, and malformed
input were exercised throughout.

### Test inventory

Nine dedicated edge-case suites, **455 tests collected** (verified with
`pytest --collect-only -q`), currently running as **439 passed,
16 xfailed** — every xfail is strict and pins a real bug documented below.

| File | Tests | xfail |
|---|---|---|
| `tests/test_ranked_matchmaking_edge_cases.py` | 44 | 2 |
| `tests/test_friend_match_edge_cases.py` | 44 | 2 |
| `tests/test_challenge_match_edge_cases.py` | 31 | 1 |
| `tests/test_match_presence_and_lifecycle_edge_cases.py` | 68 | 1 |
| `tests/test_elo_and_match_completion_edge_cases.py` | 49 | 2 |
| `tests/test_match_answer_and_scoring_edge_cases.py` | 67 | 1 |
| `tests/test_match_question_and_round_edge_cases.py` | 68 | 2 |
| `tests/test_match_datetime_and_memory_edge_cases.py` | 46 | 3 |
| `tests/test_match_isolation_and_access_edge_cases.py` | 38 | 2 |
| **Total** | **455** | **16** |

The full repository suite (including pre-existing tests) collects
491 tests.

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
   cache eviction hits this path.
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
   `"cancelled"` and the user is silently dropped from the queue.
7. **Queueing for ranked abandons or hijacks an active friend match**
   (xfail, isolation suite). The stale-match scan ignores `match_type`,
   so "play ranked" reconnects into (or abandons) a live friend match.
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

**P2 — robustness, consistency and policy gaps**

14. **User ELO can go negative** (xfail, ELO suite) — the loser `$inc`
    has no floor.
15. **`calculate_elo_change` crashes (`OverflowError`) on extreme rating
    gaps** (xfail, ELO suite) — no input guard; corrupted ratings turn
    answer submission into a 500.
16. **Match-code case-sensitivity is inconsistent** (xfail, friend
    suite). `/api/game/match/{code}` compares case-sensitively while the
    friend endpoints upper-case; the two code namespaces disagree.
17. **Self-challenge is allowed** (xfail, challenge suite) — a user can
    create and accept a challenge against themselves.
18. **Challenge endpoints have no in-memory fallback** (challenge suite).
    With the DB down, a memory-held pending challenge is invisible to
    `pending`/`accept`/`cancel` yet still playable (see bug 9).
19. **Broad unauthenticated exposure**: `/matches/all` returns the last
    50 matches of all players to any caller, and
    `/api/game/friend/status/{code}` polls any match anonymously.

### Recommended fix order

1. **Lock down `/match/{id}/details`** (bug 1): require auth + participant
   check, and strip unresolved-round answers from the payload. Smallest
   change, biggest exploit closed.
2. **Serialize writes with the existing per-match lock** (bugs 2, 4, 5):
   `submit_answer` and `join_friend_match` should take `get_match_lock`
   like `get_question` already does; move the ranked opponent pop before
   the `find_one` await (or re-check after it).
3. **Fix id generation** (bugs 3, 11): replace `match_counter` with a
   collision-free id (UUID/ObjectId) and derive round numbers from the
   match doc or Mongo, not from `in_memory_rounds`.
4. **Fix lifecycle/status gating** (bugs 6, 7, 8, 9): clear
   `cancelled_users` on re-queue, filter the stale-match scan by
   `match_type`, and have gameplay routes reject `abandoned` and
   `pending` matches, not just `completed`.
5. **Fix round-array numbering** (bug 10): number the `$push`ed summary
   with the same `round_count + 1` used for the round doc.
6. **Harden datetime handling** (bug 12): run `created_at` through
   `ensure_utc`/`parse_round_start` in the reconnect window.
7. **Sweep the P2s** (bugs 13–19): in-memory code-collision check, ELO
   floor and overflow guard, code-case normalization, self-challenge
   rejection, challenge memory fallback, and an access review of
   `/matches/all` and the anonymous status poller.

### How to run the tests

```bash
# Full suite (491 tests)
python3 -m pytest tests/ -q

# Edge-case campaign only (455 tests: 439 pass, 16 xfail)
python3 -m pytest tests/test_ranked_matchmaking_edge_cases.py \
  tests/test_friend_match_edge_cases.py \
  tests/test_challenge_match_edge_cases.py \
  tests/test_match_presence_and_lifecycle_edge_cases.py \
  tests/test_elo_and_match_completion_edge_cases.py \
  tests/test_match_answer_and_scoring_edge_cases.py \
  tests/test_match_question_and_round_edge_cases.py \
  tests/test_match_datetime_and_memory_edge_cases.py \
  tests/test_match_isolation_and_access_edge_cases.py -q

# Verify the inventory
python3 -m pytest --collect-only -q tests/
```

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
