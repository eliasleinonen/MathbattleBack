"""
Math-equivalence edge cases exercised through the *real* PvP answer path
(main.py: submit_answer ~1610), not the standalone check_math_equivalence
helper.  Every answer here is submitted to a live friend match via
/api/game/answer so the exact inline SymPy grading (preprocess + parse_expr
with implicit-multiplication + the cascade of simplify/expand/trigsimp/
logcombine/equals/root/numeric fallbacks) is what decides correctness.

Scope:
- Many equivalent answer forms that must be accepted (spaces, parentheses,
  fractions, implicit multiplication, unicode middle-dot, √, ln->log).
- Inequivalent / near-miss forms that must be rejected.
- Unicode operators: `·` is accepted; `×` and `∗` are NOT (an inconsistency
  with check_math_equivalence, pinned + xfailed as a real bug).
- Leading/trailing junk.
- Strings that parse as Python/SymPy but are the wrong math, plus
  code-injection-looking answers that must be graded wrong without executing.
- evaluate_at-style numeric questions (ask_for_derivative_only False), driven
  by monkeypatching generate_question, exercising the abs<0.1 tolerance branch.
- SymPy inputs that could crash grading: all are caught and graded wrong with
  200 (never 500); the two genuine failures — an unbounded integer power that
  hangs parse_expr, and a symbolic power tower that hangs the NUMERIC
  fallback when the random sample point is large — are each pinned with a
  subprocess watchdog and a strict xfail (the tower additionally has a
  deterministic passing test with the sample point pinned small).

See MATCH_EDGE_CASE_REPORT.md for the campaign summary.
"""

import multiprocessing as mp

import pytest
from fastapi.testclient import TestClient

import main


PLAYER_A = "guest-eq-aaa"
PLAYER_B = "guest-eq-bbb"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def derivative_question(monkeypatch):
    """Symbolic question whose stored answer is the server form '2·x'."""

    def _generate(_elo):
        return {
            "expression": "x^2",
            "derivative": "2·x",
            "evaluate_at": 0,
            "answer": "2·x",
            "difficulty": 1,
            "ask_for_derivative_only": True,
        }

    monkeypatch.setattr(main, "generate_question", _generate)
    return _generate


@pytest.fixture
def evaluate_at_question(monkeypatch):
    """Numeric question (ask_for_derivative_only False) with integer answer 6.

    Represents f'(x)=2x evaluated at x=3.  The stored answer is a number, so
    submit_answer takes its numeric branch (abs diff < 0.1 tolerance).
    """

    def _generate(_elo):
        return {
            "expression": "x^2",
            "derivative": "2·x",
            "evaluate_at": 3,
            "answer": 6,
            "difficulty": 1,
            "ask_for_derivative_only": False,
        }

    monkeypatch.setattr(main, "generate_question", _generate)
    return _generate


def _new_match(client, auth_headers, tag):
    """A fresh friend match with a served round; unique players per call."""
    p1, p2 = f"guest-eq-{tag}-a", f"guest-eq-{tag}-b"
    created = client.post("/api/game/friend/create", json={}, headers=auth_headers(p1))
    code = created.json()["match_code"]
    match_id = created.json()["match_id"]
    client.post(
        "/api/game/friend/join",
        json={"match_code": code},
        headers=auth_headers(p2),
    )
    served = client.get(
        "/api/game/question", params={"match_id": match_id}, headers=auth_headers(p1)
    )
    assert served.status_code == 200, served.text
    return match_id, p1


def _grade(client, auth_headers, answer, tag):
    """Submit `answer` to a fresh match; return the parsed answer response."""
    match_id, p1 = _new_match(client, auth_headers, tag)
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth_headers(p1),
    )
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# 1. equivalent forms that must be accepted
# ===========================================================================


@pytest.mark.parametrize(
    "form",
    [
        "2*x",
        "2x",  # implicit multiplication
        "2 * x",
        "  2x  ",  # surrounding whitespace
        "x*2",  # commuted
        "x 2",  # implicit mult with a space
        "x2",  # implicit mult, digit trailing
        "2·x",  # unicode middle dot (server's own form)
        "(2)(x)",  # adjacency multiplication
        "(x)(2)",
        "2(x)",
        "((((2x))))",  # deep nesting
        "+2x",  # leading unary plus
        "2x*1",
        "2x/1",
        "x*x/x*2",  # cancels to 2x
        "2*x + 0",
    ],
)
def test_algebraically_equal_forms_are_accepted(
    client, auth_headers, derivative_question, form
):
    body = _grade(client, auth_headers, form, "eq")
    assert body["correct"] is True, form
    assert body["player1_score"] == 1


@pytest.mark.parametrize(
    "fraction_form",
    ["4x/2", "6x/3", "x/(1/2)", "10*x/5", "(4/2)*x"],
)
def test_fraction_forms_are_accepted(
    client, auth_headers, derivative_question, fraction_form
):
    body = _grade(client, auth_headers, fraction_form, "frac")
    assert body["correct"] is True, fraction_form


@pytest.mark.parametrize(
    "generous_form",
    [
        "sqrt(4)*x",  # sqrt evaluates to 2
        "√4*x",  # unicode sqrt -> sqrt(4)
        "2.0x",  # float coefficient
        "2ex/e",  # e cancels (e is a plain symbol here, still cancels)
        "x+x",  # sum collapses
        "2x+0",
    ],
)
def test_grading_is_generous_on_equivalent_rewrites(
    client, auth_headers, derivative_question, generous_form
):
    body = _grade(client, auth_headers, generous_form, "gen")
    assert body["correct"] is True, generous_form


@pytest.mark.parametrize(
    "sympy_call_form",
    [
        "diff(x^2, x)",  # the derivative, expressed as a call
        "Derivative(x^2, x)",
        "diff(x**2)",
        "integrate(2, x)",  # antiderivative of the constant 2
        "exp(log(2x))",  # exp/log cancel
        "2*x*sin(x)**2 + 2*x*cos(x)**2",  # trig identity -> 2x
        "cancel((2x**2)/x)",
        "simplify(4*x/2)",
    ],
)
def test_sympy_function_calls_in_answers_are_evaluated_and_accepted(
    client, auth_headers, derivative_question, sympy_call_form
):
    # QUIRK: parse_expr evaluates arbitrary SymPy calculus/function calls, so
    # an answer like "diff(x^2, x)" is computed to 2*x and accepted. Generous,
    # but worth pinning: the grader does not restrict answers to plain algebra.
    body = _grade(client, auth_headers, sympy_call_form, "call")
    assert body["correct"] is True, sympy_call_form


def test_unicode_middle_dot_operator_is_accepted(
    client, auth_headers, derivative_question
):
    body = _grade(client, auth_headers, "2 · x", "dot")
    assert body["correct"] is True


# ===========================================================================
# 2. inequivalent / near-miss forms that must be rejected
# ===========================================================================


@pytest.mark.parametrize(
    "wrong",
    [
        "2",  # constant
        "x",  # missing coefficient
        "-2*x",  # sign flip
        "2*x + 1",  # off by a constant
        "x^2",  # the original expression, not its derivative
        "2*x^2",
        "2/x",  # reciprocal-ish
        "2*x + 0.5",
        "3*x",  # wrong coefficient
        "x/2",
    ],
)
def test_near_miss_forms_are_rejected(
    client, auth_headers, derivative_question, wrong
):
    body = _grade(client, auth_headers, wrong, "nm")
    assert body["correct"] is False, wrong
    assert body["player1_score"] == 0


@pytest.mark.parametrize(
    "python_but_wrong",
    [
        "2**x",  # power, not product
        "x**2",  # power
        "0x2",  # hex literal -> 2, wrong
        "2y",  # different symbol
        "2*X",  # capital X is a distinct symbol
        "idiff(x^2, x)",  # implicit-diff call, not 2*x
        "e**log(2x)",  # `e` is a plain symbol, not Euler's number -> not 2x
        "ln(e^(2x))",  # same: e symbol, ln->log, stays log(e**(2x))
    ],
)
def test_parses_as_python_but_wrong_math_is_rejected(
    client, auth_headers, derivative_question, python_but_wrong
):
    body = _grade(client, auth_headers, python_but_wrong, "pw")
    assert body["correct"] is False, python_but_wrong


# ===========================================================================
# 3. leading/trailing junk (parse errors -> graded wrong, no 500)
# ===========================================================================


@pytest.mark.parametrize(
    "junky",
    [
        "2x;",
        "answer is 2x",
        "2x!",
        "d/dx(x^2)",  # the notation itself, not a valid expression
        "2x @home",
        "= 2x",
        "2x)))))",  # unbalanced parens
        "the derivative is 2x obviously",
    ],
)
def test_junky_surrounding_text_is_graded_wrong_not_500(
    client, auth_headers, derivative_question, junky
):
    body = _grade(client, auth_headers, junky, "junk")
    assert body["correct"] is False, junky


def test_hash_comment_suffix_is_stripped_by_parser_quirk(
    client, auth_headers, derivative_question
):
    # QUIRK: SymPy's parser treats '#' as a line comment, so trailing text
    # after a '#' is discarded and "2x #comment" grades as the bare "2x".
    body = _grade(client, auth_headers, "2x #comment", "hash")
    assert body["correct"] is True


# ===========================================================================
# 4. unicode operator inconsistency (real bug: pinned + xfail)
# ===========================================================================


@pytest.mark.parametrize("op_form", ["2×x", "2∗x", "2✕x"])
def test_alternate_unicode_multiplication_is_rejected_current_behavior(
    client, auth_headers, derivative_question, op_form
):
    # submit_answer's inline preprocess maps only the middle dot "·" to "*".
    # The multiplication sign "×", the asterisk operator "∗", and heavy X "✕"
    # are NOT mapped, so SymPy can't parse them and the answer is graded wrong.
    body = _grade(client, auth_headers, op_form, "op")
    assert body["correct"] is False, op_form


def test_check_math_equivalence_accepts_times_sign_unlike_pvp():
    # The standalone helper (used by the daily-challenge path) maps BOTH "·"
    # and "×", so it accepts "2×x" — proving the PvP grader is the odd one out.
    assert main.check_math_equivalence("2·x", "2×x") is True


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG (grading inconsistency): submit_answer maps only '·' to '*', "
        "while check_math_equivalence maps '·' AND '×'. A player typing the "
        "multiplication sign '2×x' is marked wrong in PvP even though the "
        "identical answer passes the daily-challenge checker. The PvP grader "
        "should normalize '×' too."
    ),
)
def test_times_sign_should_be_accepted_in_pvp(
    client, auth_headers, derivative_question
):
    body = _grade(client, auth_headers, "2×x", "opx")
    assert body["correct"] is True


# ===========================================================================
# 5. security: code-injection-looking answers are graded wrong, not executed
# ===========================================================================


@pytest.mark.parametrize(
    "hostile",
    [
        "__import__('os').system('id')",
        "open('/etc/passwd').read()",
        "eval('2*x')",
        "exec('x=1')",
        "lambda: 2*x",
        "Symbol('x')*2",
        "[].__class__.__base__",
    ],
)
def test_code_injection_answers_are_graded_wrong_without_executing(
    client, auth_headers, derivative_question, hostile
):
    # No RCE, no 500 — parse_expr rejects/neutralizes these and grading falls
    # through to correct=False.
    body = _grade(client, auth_headers, hostile, "sec")
    assert body["correct"] is False, hostile


# ===========================================================================
# 6. evaluate_at numeric questions (ask_for_derivative_only False)
# ===========================================================================


def test_evaluate_at_question_payload_flags_numeric_mode(
    client, auth_headers, evaluate_at_question
):
    match_id, p1 = _new_match(client, auth_headers, "evalflag")
    payload = client.get(
        "/api/game/question", params={"match_id": match_id}, headers=auth_headers(p1)
    ).json()
    assert payload["ask_for_derivative_only"] is False
    assert payload["evaluate_at"] == 3


@pytest.mark.parametrize(
    "correct_value",
    [6, 6.0, "6", "6.0", " 6 ", "6\n", "0006", "6e0", 6.05, 5.95],
)
def test_numeric_answer_within_tolerance_is_accepted(
    client, auth_headers, evaluate_at_question, correct_value
):
    body = _grade(client, auth_headers, correct_value, "evok")
    assert body["correct"] is True, repr(correct_value)


@pytest.mark.parametrize(
    "wrong_value",
    [6.2, 6.5, 5.5, -6, 0, "six", "2*3", "inf", "nan", "", "  "],
)
def test_numeric_answer_outside_tolerance_or_unparseable_is_rejected(
    client, auth_headers, evaluate_at_question, wrong_value
):
    # The numeric branch is float(data.answer): non-numeric strings (including
    # "2*3", which it does NOT evaluate) and out-of-tolerance numbers are wrong.
    body = _grade(client, auth_headers, wrong_value, "evbad")
    assert body["correct"] is False, repr(wrong_value)


def test_boolean_answer_on_numeric_question_is_graded_wrong(
    client, auth_headers, evaluate_at_question
):
    # JSON true -> Union coerces to 1.0 -> abs(1.0-6) > 0.1 -> wrong (no crash).
    body = _grade(client, auth_headers, True, "evbool")
    assert body["correct"] is False


# ===========================================================================
# 7. SymPy inputs that could crash grading -> 200, never 500
# ===========================================================================


@pytest.mark.parametrize(
    "pathological",
    [
        "(",  # unbalanced
        "*",  # bare operator
        "**",
        "x..2",  # double dot
        "'2x'",  # quoted
        '"2x"',
        "1/0",  # zero division on evaluate
        "x/0",
        "factorial(50000)",  # huge but finite -> not equal, no hang
        "sqrt(-4)*x*I/1",  # complex
        "x**(10**6)",  # large symbolic exponent (finite work)
        "oo",  # infinity
        "zoo",  # complex infinity
        "nan",
    ],
)
def test_pathological_sympy_answers_return_200_graded_wrong(
    client, auth_headers, derivative_question, pathological
):
    body = _grade(client, auth_headers, pathological, "path")
    assert body["correct"] is False, pathological


def test_power_tower_answer_graded_wrong_when_sampled_at_small_point(
    client, auth_headers, derivative_question, monkeypatch
):
    # The symbolic cascade handles x**x**x**x**x fine (all steps return
    # False quickly); it is only the NUMERIC fallback that can blow up,
    # because it substitutes a random point from uniform(1, 10) and asks
    # mpmath to evaluate the tower there. At small sample points the value
    # is finite and grading completes; pin the sample point to make that
    # deterministic (see the xfail below for the hang at larger points).
    monkeypatch.setattr(main.random, "uniform", lambda a, b: 1.5)
    body = _grade(client, auth_headers, "x**x**x**x**x", "tower")
    assert body["correct"] is False


# --- the one genuine crash: an unbounded integer power that HANGS grading ---


def _pvp_grade_worker(answer, result_queue, pin_uniform=None):
    """Self-contained: stub Mongo, serve a round, submit `answer`, report status.

    Runs in a forked child so a hang can be killed without taking down the
    test process. Top-level (picklable) per the no-inline-import rule; the
    heavy imports it needs are already loaded in the parent and inherited.

    `pin_uniform` pins random.uniform (the numeric fallback's sample point)
    so tests that depend on WHERE the fallback evaluates are deterministic.
    """
    import os

    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017")

    class _Cursor:
        def sort(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        async def to_list(self, length=None):
            return []

    async def _find_one(*a, **k):
        return None

    async def _insert_one(*a, **k):
        return None

    async def _update_one(*a, **k):
        return type("R", (), {"modified_count": 1, "matched_count": 1, "upserted_id": None})()

    def _find(*a, **k):
        return _Cursor()

    for coll in (
        main.users_collection,
        main.matches_collection,
        main.rounds_collection,
        main.daily_challenges_collection,
        main.daily_completions_collection,
    ):
        coll.find_one = _find_one
        coll.insert_one = _insert_one
        coll.update_one = _update_one
        coll.find = _find

    main.generate_question = lambda _elo: {
        "expression": "x^2",
        "derivative": "2·x",
        "evaluate_at": 0,
        "answer": "2·x",
        "difficulty": 1,
        "ask_for_derivative_only": True,
    }
    if pin_uniform is not None:
        main.random.uniform = lambda a, b: pin_uniform

    client = TestClient(main.app)
    auth = lambda g: {"Authorization": f"Bearer {g}"}
    created = client.post("/api/game/friend/create", json={}, headers=auth("w1"))
    code = created.json()["match_code"]
    match_id = created.json()["match_id"]
    client.post("/api/game/friend/join", json={"match_code": code}, headers=auth("w2"))
    client.get("/api/game/question", params={"match_id": match_id}, headers=auth("w1"))
    response = client.post(
        "/api/game/answer",
        json={"match_id": match_id, "answer": answer},
        headers=auth("w1"),
    )
    result_queue.put(response.status_code)


def _grade_with_timeout(answer, timeout, pin_uniform=None):
    """Return the HTTP status, or None if grading did not finish in `timeout`s.

    Uses a `spawn` child (fresh interpreter) rather than `fork`: by the time
    this suite runs, earlier tests have started TestClient portal threads, and
    forking a multithreaded process can deadlock the child on inherited locks.
    Spawn starts clean, so terminating a hung child is reliable.
    """
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_pvp_grade_worker, args=(answer, queue, pin_uniform))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        return None
    return queue.get() if not queue.empty() else None


def test_grade_with_timeout_harness_reports_finish_for_normal_answer():
    # Sanity check on the watchdog: a normal answer finishes and returns 200.
    assert _grade_with_timeout("2*x", timeout=30) == 200


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG (DoS): grading has no timeout and calls parse_expr(evaluate=True). "
        "'9**9**9' forces Python to compute 9**387420489, an astronomically "
        "large integer, so submit_answer hangs indefinitely on a single "
        "request. Grading should bound work / reject unevaluated huge powers."
    ),
)
def test_unbounded_integer_power_answer_does_not_hang_grading():
    finished = _grade_with_timeout("9**9**9", timeout=12) is not None
    assert finished, "grading hung on '9**9**9' (no timeout guard)"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG (DoS, second entry point): the numeric fallback substitutes a "
        "random point from uniform(1, 10) into the user's expression and "
        "evaluates it with N(). For a symbolic power tower like "
        "'x**x**x**x**x' the tower is astronomically large at most of that "
        "range (only samples below ~2 stay finite), so with ~85% probability "
        "per attempt mpmath grinds forever and the request never returns - "
        "the same one-request wedge as '9**9**9', reached through subs/N "
        "instead of parse_expr. The fallback needs the same work bound / "
        "timeout as the parser."
    ),
)
def test_power_tower_should_not_hang_numeric_fallback_at_large_sample_points():
    # Pin the sample point to 9.0 (representative of most of uniform(1, 10))
    # so the hang is deterministic instead of an ~85% coin flip.
    finished = _grade_with_timeout("x**x**x**x**x", timeout=12, pin_uniform=9.0)
    assert finished is not None, (
        "grading hung evaluating x**x**x**x**x at x=9 (numeric fallback has "
        "no timeout guard)"
    )
