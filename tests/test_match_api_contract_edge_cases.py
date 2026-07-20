"""
API-contract edge cases for the people-match routes in main.py.

Scope (all against human-vs-human / friend matches unless noted):
- Response-key contracts for every people-match endpoint: friend
  create/join/status, challenges pending/accept/cancel, game
  start/cancel/active/status, question, answer, give-up and match-by-code.
- The HTTP status-code matrix for common misuse (403/404/400/422).  This app
  has NO 401 path: get_current_user falls back to a guest identity for
  missing/garbage credentials, so unauthenticated calls succeed with 200.
  That is pinned here (and cross-referenced as a real auth bug via a strict
  xfail) rather than assumed.
- Content-type handling and missing JSON bodies.
- Extra unknown fields (ignored by pydantic) vs type-coercion rejections.
- match_id / match_code path injection, weird characters and null bytes.
- Very long match_code / match_id values.
- Wrong HTTP method on match routes -> 405.

These assert the *shape* of the contract; grading/scoring semantics live in
test_match_answer_and_scoring_edge_cases.py and the math-equivalence suite.
See MATCH_EDGE_CASE_REPORT.md for the campaign summary.
"""

import copy

import pytest

import main


PLAYER_A = "guest-contract-aaa"
PLAYER_B = "guest-contract-bbb"
OUTSIDER = "guest-contract-outsider"

CORRECT = "2*x"  # matches fixed_question's stored answer "2·x"


# ---------------------------------------------------------------------------
# fake Mongo collections (challenge accept/cancel/pending need a findable doc)
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self._docs)
        return list(self._docs)[:length]


class _FakeMatchesDB:
    """Enough of the Mongo matches collection for the challenge/friend flows."""

    def __init__(self):
        self.docs = {}

    @staticmethod
    def _matches(doc, query):
        return all(doc.get(key) == value for key, value in (query or {}).items())

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = copy.deepcopy(doc)

    async def find_one(self, query, *args, **kwargs):
        for doc in self.docs.values():
            if self._matches(doc, query):
                return copy.deepcopy(doc)
        return None

    def find(self, query=None, *args, **kwargs):
        hits = [copy.deepcopy(d) for d in self.docs.values() if self._matches(d, query)]
        return _FakeCursor(hits)

    async def update_one(self, query, update, *args, **kwargs):
        for doc in self.docs.values():
            if self._matches(doc, query):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                break
        return type("R", (), {"modified_count": 1, "matched_count": 1, "upserted_id": None})()

    async def delete_one(self, query, *args, **kwargs):
        for match_id, doc in list(self.docs.items()):
            if self._matches(doc, query):
                del self.docs[match_id]
                break
        return None


@pytest.fixture
def fake_matches_db(mock_mongo, monkeypatch):
    db = _FakeMatchesDB()
    for method in ("insert_one", "find_one", "find", "update_one", "delete_one"):
        monkeypatch.setattr(main.matches_collection, method, getattr(db, method))
    return db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _create(client, auth_headers, player=PLAYER_A, body=None):
    return client.post(
        "/api/game/friend/create", json=body or {}, headers=auth_headers(player)
    )


def _friend_match(client, auth_headers, p1=PLAYER_A, p2=PLAYER_B):
    """Create + join a friend match; return (match_id, match_code)."""
    created = _create(client, auth_headers, p1)
    assert created.status_code == 200, created.text
    code = created.json()["match_code"]
    joined = client.post(
        "/api/game/friend/join",
        json={"match_code": code},
        headers=auth_headers(p2),
    )
    assert joined.status_code == 200, joined.text
    return created.json()["match_id"], code


def _question(client, auth_headers, match_id, player):
    return client.get(
        "/api/game/question",
        params={"match_id": match_id},
        headers=auth_headers(player),
    )


def _answer(client, auth_headers, match_id, player, answer):
    return client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(player),
    )


def _complete_match(client, auth_headers, match_id, winner=PLAYER_A):
    """Drive a friend match to completion (winner scores 3 rounds)."""
    for _ in range(3):
        _question(client, auth_headers, match_id, winner)
        body = _answer(client, auth_headers, match_id, winner, CORRECT).json()
    return body


# ===========================================================================
# 1. response-key contracts
# ===========================================================================


def test_friend_create_response_keys(client, auth_headers):
    body = _create(client, auth_headers).json()
    assert set(body) == {"match_id", "match_code", "link", "status"}
    assert body["status"] == "waiting"
    assert body["match_id"].startswith("match-")
    assert len(body["match_code"]) == 6
    assert body["match_code"] in body["link"]


def test_friend_join_response_keys(client, auth_headers):
    code = _create(client, auth_headers).json()["match_code"]
    body = client.post(
        "/api/game/friend/join",
        json={"match_code": code},
        headers=auth_headers(PLAYER_B),
    ).json()
    assert set(body) == {"match_id", "status"}
    assert body["status"] == "active"


def test_friend_status_response_keys(client, auth_headers):
    code = _create(client, auth_headers).json()["match_code"]
    body = client.get(f"/api/game/friend/status/{code}").json()
    assert set(body) == {"match_id", "status", "player1_ready", "player2_ready"}
    assert body["player1_ready"] is True
    assert body["player2_ready"] is False


def test_game_active_response_keys_false_and_true(client, auth_headers):
    idle = client.get("/api/game/active", headers=auth_headers(OUTSIDER)).json()
    assert set(idle) == {"has_active_match"}
    assert idle["has_active_match"] is False

    _friend_match(client, auth_headers)
    active = client.get("/api/game/active", headers=auth_headers(PLAYER_A)).json()
    assert set(active) == {"has_active_match", "match_id", "match_type", "opponent"}
    assert active["has_active_match"] is True
    assert active["match_type"] == "friend"


def test_game_cancel_response_keys(client, auth_headers):
    body = client.post("/api/game/cancel", headers=auth_headers(PLAYER_A)).json()
    assert set(body) == {"status"}
    assert body["status"] == "cancelled"


def test_question_response_keys(client, auth_headers, fixed_question):
    match_id, _ = _friend_match(client, auth_headers)
    body = _question(client, auth_headers, match_id, PLAYER_A).json()
    # No time_limit for human matches (bot matches add it).
    assert set(body) == {
        "round_id",
        "expression",
        "evaluate_at",
        "ask_for_derivative_only",
        "round_start_time",
    }
    assert body["ask_for_derivative_only"] is True


def test_answer_in_progress_response_keys(client, auth_headers, fixed_question):
    match_id, _ = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    body = _answer(client, auth_headers, match_id, PLAYER_A, "totally-wrong").json()
    assert set(body) == {
        "correct",
        "round_winner",
        "player1_score",
        "player2_score",
        "match_winner",
        "elo_change",
    }
    assert body["correct"] is False


def test_answer_already_won_response_keys(client, auth_headers, fixed_question):
    match_id, _ = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    _answer(client, auth_headers, match_id, PLAYER_A, CORRECT)
    late = _answer(client, auth_headers, match_id, PLAYER_B, CORRECT).json()
    assert set(late) == {
        "correct",
        "already_won",
        "round_winner",
        "player1_score",
        "player2_score",
        "match_winner",
        "elo_change",
    }
    assert late["already_won"] is True
    assert late["correct"] is False


def test_give_up_response_keys(client, auth_headers, fixed_question):
    match_id, _ = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    alone = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    ).json()
    assert set(alone) == {"status", "waiting_for_opponent"}
    assert alone == {"status": "gave_up", "waiting_for_opponent": True}

    both = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_B),
    ).json()
    assert set(both) == {"status", "round_winner", "player1_score", "player2_score"}
    assert both["status"] == "both_gave_up"
    assert both["round_winner"] == "tie"

    again = client.post(
        "/api/game/give-up",
        params={"match_id": match_id},
        headers=auth_headers(PLAYER_A),
    ).json()
    assert set(again) == {"status", "round_winner"}
    assert again["status"] == "already_ended"


def test_game_status_response_keys(client, auth_headers, fixed_question):
    match_id, _ = _friend_match(client, auth_headers)
    body = client.get(
        f"/api/game/status/{match_id}", headers=auth_headers(PLAYER_A)
    ).json()
    assert set(body) == {
        "match_id",
        "player1_id",
        "player2_id",
        "player1_name",
        "player2_name",
        "player1_score",
        "player2_score",
        "status",
        "winner_id",
        "elo_change",
        "round_winner",
        "round_start_time",
        "player1_gave_up",
        "player2_gave_up",
        "opponent_connected",
    }


def test_match_by_code_response_keys(client, auth_headers):
    _, code = _friend_match(client, auth_headers)
    body = client.get(
        f"/api/game/match/{code}", headers=auth_headers(PLAYER_A)
    ).json()
    assert set(body) == {
        "match_id",
        "status",
        "player1_id",
        "player2_id",
        "player1_score",
        "player2_score",
        "current_round",
        "is_player1",
        "opponent_name",
        "is_opponent_bot",
    }
    assert body["is_player1"] is True
    assert body["is_opponent_bot"] is False


def test_challenge_lifecycle_response_keys(client, auth_headers, fake_matches_db):
    # Seed a pending challenge directly so the invitee sees/accepts it.
    match_id = "match-challenge-contract"
    fake_matches_db.docs[match_id] = {
        "_id": match_id,
        "match_code": "CHALLC",
        "match_type": "friend",
        "player1_id": PLAYER_A,
        "player1_username": "Alpha",
        "player2_id": PLAYER_B,
        "status": "pending",
        "created_at": main.datetime.utcnow(),
    }

    pending = client.get(
        "/api/challenges/pending", headers=auth_headers(PLAYER_B)
    ).json()
    assert len(pending) == 1
    assert set(pending[0]) == {"match_id", "match_code", "challenger", "created_at"}
    assert pending[0]["challenger"] == "Alpha"

    accepted = client.post(
        f"/api/challenges/accept/{match_id}", headers=auth_headers(PLAYER_B)
    ).json()
    assert set(accepted) == {"match_id", "match_code", "status"}
    assert accepted["status"] == "active"


def test_challenge_cancel_response_keys(client, auth_headers, fake_matches_db):
    match_id = "match-challenge-cancel"
    fake_matches_db.docs[match_id] = {
        "_id": match_id,
        "match_code": "CANCLC",
        "player1_id": PLAYER_A,
        "player2_id": PLAYER_B,
        "status": "pending",
        "created_at": main.datetime.utcnow(),
    }
    body = client.post(
        f"/api/challenges/cancel/{match_id}", headers=auth_headers(PLAYER_A)
    ).json()
    assert set(body) == {"status"}
    assert body["status"] == "cancelled"


# ===========================================================================
# 2. status-code matrix for common misuse
# ===========================================================================


def test_unknown_match_id_is_404_across_routes(client, auth_headers):
    ghost = "match-does-not-exist"
    assert _question(client, auth_headers, ghost, PLAYER_A).status_code == 404
    assert _answer(client, auth_headers, ghost, PLAYER_A, CORRECT).status_code == 404
    assert (
        client.post(
            "/api/game/give-up",
            params={"match_id": ghost},
            headers=auth_headers(PLAYER_A),
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/game/status/{ghost}", headers=auth_headers(PLAYER_A)).status_code
        == 404
    )


def test_unknown_code_is_404_on_join_and_status_and_match(client, auth_headers):
    assert (
        client.post(
            "/api/game/friend/join",
            json={"match_code": "NOPE99"},
            headers=auth_headers(PLAYER_A),
        ).status_code
        == 404
    )
    assert client.get("/api/game/friend/status/NOPE99").status_code == 404
    assert (
        client.get("/api/game/match/NOPE99", headers=auth_headers(PLAYER_A)).status_code
        == 404
    )


def test_unknown_challenge_id_is_404(client, auth_headers, fake_matches_db):
    assert (
        client.post(
            "/api/challenges/accept/nope", headers=auth_headers(PLAYER_A)
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/challenges/cancel/nope", headers=auth_headers(PLAYER_A)
        ).status_code
        == 404
    )


def test_outsider_gets_403_on_member_only_routes(client, auth_headers, fixed_question):
    match_id, code = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)

    assert _question(client, auth_headers, match_id, OUTSIDER).status_code == 403
    assert _answer(client, auth_headers, match_id, OUTSIDER, CORRECT).status_code == 403
    assert (
        client.post(
            "/api/game/give-up",
            params={"match_id": match_id},
            headers=auth_headers(OUTSIDER),
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/game/status/{match_id}", headers=auth_headers(OUTSIDER)
        ).status_code
        == 403
    )
    assert (
        client.get(f"/api/game/match/{code}", headers=auth_headers(OUTSIDER)).status_code
        == 403
    )


def test_wrong_actor_gets_403_on_challenge_accept_cancel(
    client, auth_headers, fake_matches_db
):
    match_id = "match-challenge-403"
    fake_matches_db.docs[match_id] = {
        "_id": match_id,
        "match_code": "C403CC",
        "player1_id": PLAYER_A,
        "player2_id": PLAYER_B,
        "status": "pending",
        "created_at": main.datetime.utcnow(),
    }
    # Only player2 can accept; only player1 can cancel.
    assert (
        client.post(
            f"/api/challenges/accept/{match_id}", headers=auth_headers(OUTSIDER)
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/challenges/cancel/{match_id}", headers=auth_headers(OUTSIDER)
        ).status_code
        == 403
    )


def test_completed_match_is_400_on_question_and_answer(
    client, auth_headers, fixed_question
):
    match_id, _ = _friend_match(client, auth_headers)
    _complete_match(client, auth_headers, match_id)
    assert _question(client, auth_headers, match_id, PLAYER_A).status_code == 400
    assert _answer(client, auth_headers, match_id, PLAYER_A, CORRECT).status_code == 400


def test_join_started_match_is_400(client, auth_headers):
    _, code = _friend_match(client, auth_headers)
    # match is now active; a third player joining the same code -> 400
    third = client.post(
        "/api/game/friend/join",
        json={"match_code": code},
        headers=auth_headers(OUTSIDER),
    )
    assert third.status_code == 400
    assert third.json()["detail"] == "Match already started"


def test_join_own_match_is_400(client, auth_headers):
    code = _create(client, auth_headers).json()["match_code"]
    same = client.post(
        "/api/game/friend/join",
        json={"match_code": code},
        headers=auth_headers(PLAYER_A),
    )
    assert same.status_code == 400
    assert same.json()["detail"] == "Cannot join your own match"


def test_active_round_before_question_is_404_no_active_round(client, auth_headers):
    # Joined but nobody fetched a question yet -> answer/give-up 404.
    match_id, _ = _friend_match(client, auth_headers)
    assert _answer(client, auth_headers, match_id, PLAYER_A, CORRECT).status_code == 404
    assert (
        client.post(
            "/api/game/give-up",
            params={"match_id": match_id},
            headers=auth_headers(PLAYER_A),
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"answer": "2x"},  # missing match_id
        {"match_id": "m"},  # missing answer
        {"match_id": None, "answer": "2x"},  # null match_id
        {"match_id": 42, "answer": "2x"},  # int match_id
        {"match_id": "m", "answer": None},  # null answer
        {"match_id": "m", "answer": ["2x"]},  # list answer
        {"match_id": "m", "answer": {"v": 1}},  # dict answer
    ],
)
def test_answer_validation_errors_are_422(client, auth_headers, payload):
    response = client.post(
        "/api/game/answer", json=payload, headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 422


def test_missing_query_match_id_is_422(client, auth_headers):
    # question (GET) and give-up (POST) take match_id as a *query* param.
    assert (
        client.get("/api/game/question", headers=auth_headers(PLAYER_A)).status_code
        == 422
    )
    assert (
        client.post("/api/game/give-up", headers=auth_headers(PLAYER_A)).status_code
        == 422
    )


def test_start_validation_errors_are_422(client, auth_headers):
    assert (
        client.post("/api/game/start", json={}, headers=auth_headers(PLAYER_A)).status_code
        == 422
    )
    assert (
        client.post(
            "/api/game/start", json={"mode": 123}, headers=auth_headers(PLAYER_A)
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/game/start", json={"mode": None}, headers=auth_headers(PLAYER_A)
        ).status_code
        == 422
    )


# ===========================================================================
# 3. content-type / missing JSON body
# ===========================================================================


def test_missing_json_body_is_422(client, auth_headers):
    assert (
        client.post("/api/game/friend/join", headers=auth_headers(PLAYER_A)).status_code
        == 422
    )
    assert (
        client.post("/api/game/start", headers=auth_headers(PLAYER_A)).status_code == 422
    )
    assert (
        client.post("/api/game/answer", headers=auth_headers(PLAYER_A)).status_code == 422
    )


def test_form_encoded_body_is_rejected_422(client, auth_headers):
    response = client.post(
        "/api/game/friend/join",
        content=b"match_code=ABCDEF",
        headers={
            **auth_headers(PLAYER_A),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status_code == 422


def test_json_body_with_text_plain_content_type_is_422(client, auth_headers):
    response = client.post(
        "/api/game/friend/join",
        content=b'{"match_code": "ABCDEF"}',
        headers={**auth_headers(PLAYER_A), "Content-Type": "text/plain"},
    )
    assert response.status_code == 422


def test_malformed_json_body_is_422(client, auth_headers):
    response = client.post(
        "/api/game/friend/join",
        content=b"{not valid json",
        headers={**auth_headers(PLAYER_A), "Content-Type": "application/json"},
    )
    assert response.status_code == 422


# ===========================================================================
# 4. extra unknown fields ignored vs coercion rejected (pydantic)
# ===========================================================================


def test_extra_unknown_fields_are_ignored(client, auth_headers):
    # pydantic models here don't forbid extras -> unknown keys are dropped.
    response = client.post(
        "/api/game/start",
        json={"mode": "random", "bogus": "ignored", "nested": {"a": 1}},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 200
    assert response.json()["status"] in {"searching", "matched"}


def test_extra_fields_on_answer_are_ignored(client, auth_headers, fixed_question):
    match_id, _ = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": CORRECT, "cheat": True, "score": 999},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 200
    assert response.json()["correct"] is True


def test_string_boolean_coercion_is_accepted_for_continue_existing(
    client, auth_headers
):
    # continue_existing is Optional[bool]; pydantic coerces the JSON string
    # "yes"/"true" but rejects a non-boolean-ish string.
    ok = client.post(
        "/api/game/start",
        json={"mode": "random", "continue_existing": "true"},
        headers=auth_headers(PLAYER_A),
    )
    assert ok.status_code == 200
    bad = client.post(
        "/api/game/start",
        json={"mode": "random", "continue_existing": "maybe"},
        headers=auth_headers(PLAYER_B),
    )
    assert bad.status_code == 422


def test_numeric_answer_is_accepted_by_union_type(client, auth_headers, fixed_question):
    # AnswerSubmit.answer is Union[str, float]; a JSON number is valid input
    # (graded via the numeric branch, wrong here against a symbolic answer).
    match_id, _ = _friend_match(client, auth_headers)
    _question(client, auth_headers, match_id, PLAYER_A)
    response = _answer(client, auth_headers, match_id, PLAYER_A, 7)
    assert response.status_code == 200
    assert response.json()["correct"] is False


# ===========================================================================
# 5. path injection / weird characters / null bytes
# ===========================================================================


@pytest.mark.parametrize(
    "weird_id",
    [
        "a b",  # space
        "a%00b",  # url-encoded null byte
        "match/../secrets",  # traversal-ish (encoded slash handled below)
        "match;drop",
        "match|pipe",
        "m<script>",
        "m'or'1'='1",
        "..",
    ],
)
def test_weird_match_id_in_status_path_is_404_not_500(
    client, auth_headers, weird_id
):
    response = client.get(
        f"/api/game/status/{weird_id}", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code in {404, 422}
    assert response.status_code != 500


@pytest.mark.parametrize(
    "weird_id",
    ["match-∆-∞", "mátch-ñ", "match-\u200b-zwsp", "match-\U0001f600"],
)
def test_unicode_match_id_query_is_404_not_500(client, auth_headers, weird_id):
    response = client.get(
        "/api/game/question",
        params={"match_id": weird_id},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "weird_code",
    ["'; DROP TABLE matches;--", "../../etc/passwd", "<xml>", "co de", "🎲🎲🎲"],
)
def test_injection_match_code_on_join_is_404_not_500(
    client, auth_headers, weird_code
):
    response = client.post(
        "/api/game/friend/join",
        json={"match_code": weird_code},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404


def test_encoded_slash_in_status_path_is_404(client, auth_headers):
    response = client.get(
        "/api/game/status/a%2Fb", headers=auth_headers(PLAYER_A)
    )
    assert response.status_code == 404


# ===========================================================================
# 6. very long match_code / match_id
# ===========================================================================


@pytest.mark.parametrize("length", [1000, 50000])
def test_very_long_match_id_query_is_404_not_500(client, auth_headers, length):
    response = client.get(
        "/api/game/question",
        params={"match_id": "m" * length},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404


@pytest.mark.parametrize("length", [1000, 50000])
def test_very_long_match_code_on_join_is_404_not_500(
    client, auth_headers, length
):
    response = client.post(
        "/api/game/friend/join",
        json={"match_code": "Z" * length},
        headers=auth_headers(PLAYER_A),
    )
    assert response.status_code == 404


def test_very_long_match_code_on_status_path_is_404(client, auth_headers):
    response = client.get("/api/game/friend/status/" + "A" * 5000)
    assert response.status_code == 404


# ===========================================================================
# 7. wrong HTTP method -> 405
# ===========================================================================


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/game/answer"),
        ("get", "/api/game/start"),
        ("get", "/api/game/cancel"),
        ("get", "/api/game/give-up"),
        ("put", "/api/game/start"),
        ("delete", "/api/game/friend/create"),
        ("post", "/api/game/active"),
        ("post", "/api/game/question"),
        ("get", "/api/challenges/accept/some-id"),
        ("get", "/api/challenges/cancel/some-id"),
        ("post", "/api/challenges/pending"),
        ("delete", "/api/game/status/some-id"),
    ],
)
def test_wrong_http_method_is_405(client, auth_headers, method, path):
    response = getattr(client, method)(path, headers=auth_headers(PLAYER_A))
    assert response.status_code == 405


def test_405_advertises_allowed_method(client, auth_headers):
    # FastAPI/Starlette sets the Allow header on a 405.
    response = client.get("/api/game/answer", headers=auth_headers(PLAYER_A))
    assert response.status_code == 405
    assert "POST" in response.headers.get("allow", "")


# ===========================================================================
# 8. the "no 401" contract (real auth bug, pinned + xfail)
# ===========================================================================


@pytest.mark.parametrize(
    "headers",
    [
        {},  # no Authorization header at all
        {"Authorization": "Bearer not.a.real.jwt"},  # garbage bearer
        {"Authorization": "Bearer "},  # empty bearer
        {"Authorization": "Basic Zm9vOmJhcg=="},  # wrong scheme
    ],
)
def test_missing_or_bad_credentials_never_401_current_behavior(client, headers):
    # get_current_user falls back to a guest identity, so every one of these
    # succeeds with 200 instead of challenging with 401. Pinned as-is.
    response = client.get("/api/game/active", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"has_active_match": False}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG (auth, cross-ref isolation-suite bug #1): state-changing match "
        "routes accept requests with NO credentials by falling back to a "
        "shared guest identity. friend/create should require authentication "
        "and reject an anonymous caller with 401, but returns 200."
    ),
)
def test_anonymous_state_change_should_be_401(client):
    response = client.post("/api/game/friend/create", json={})
    assert response.status_code == 401
