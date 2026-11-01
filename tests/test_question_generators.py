"""Tests for question generation, difficulty tiers, and answer normalization."""

import re
import pytest
from sympy import Symbol, E, diff, simplify, trigsimp, N
from sympy.parsing.sympy_parser import (
    parse_expr,
    standard_transformations,
    implicit_multiplication_application,
    function_exponentiation,
)
import main

transformations = (standard_transformations + (implicit_multiplication_application, function_exponentiation))
x = Symbol('x')
sym_e = Symbol('e')


def assert_calculus_pair_valid(expr_str: str, deriv_str: str):
    """Verify that deriv_str is the mathematically exact derivative of expr_str."""
    assert '<' not in deriv_str, f"Derivative contains HTML tags: {deriv_str}"
    assert 'fraction' not in deriv_str, f"Derivative contains fraction class: {deriv_str}"

    norm_expr = main.normalize_answer_text(expr_str)
    norm_deriv = main.normalize_answer_text(deriv_str)

    sym_expr = parse_expr(norm_expr, transformations=transformations, evaluate=True)
    sym_deriv = parse_expr(norm_deriv, transformations=transformations, evaluate=True)

    actual_deriv = diff(sym_expr.subs(sym_e, E), x)
    expected_deriv = sym_deriv.subs(sym_e, E)

    # Direct algebraic equality
    if simplify(actual_deriv - expected_deriv) == 0:
        return True
    # Trig simplification equality
    if simplify(trigsimp(actual_deriv) - trigsimp(expected_deriv)) == 0:
        return True
    # Numerical evaluation test at multiple points
    for val in [1.3, 2.1, 3.2]:
        val_diff = abs(complex(N(actual_deriv.subs(x, val) - expected_deriv.subs(x, val))))
        assert val_diff < 1e-4, f"Mismatch on {expr_str} -> {deriv_str} (at x={val}: diff={val_diff})"
    return True


def test_normalize_answer_text_html_fractions():
    html_sample = "<div class='fraction'><span class='numerator'>-1</span><span class='denominator'>x^2</span></div>"
    normalized = main.normalize_answer_text(html_sample)
    assert normalized == "((-1)/(x**2))"


def test_normalize_answer_text_nested_sup():
    sample = "e<sup>x<sup>2</sup></sup>"
    normalized = main.normalize_answer_text(sample)
    assert normalized == "e**(x**(2))"


def test_normalize_answer_text_unicode_and_sqrt():
    assert main.normalize_answer_text("3·x × 2∗y") == "3*x * 2*y"
    assert main.normalize_answer_text("1/(2·√x)") == "1/(2*sqrt(x))"
    assert main.normalize_answer_text("√(x + 1)") == "sqrt(x + 1)"


def test_random_poly_easy_mathematical_accuracy():
    lengths = []
    for _ in range(100):
        expr, deriv = main.random_poly_easy()
        assert isinstance(expr, str) and expr
        assert isinstance(deriv, str) and deriv
        assert_calculus_pair_valid(expr, deriv)
        lengths.append(len(deriv))
    # Easy questions should be quick to type (average <= 10 characters)
    avg_len = sum(lengths) / len(lengths)
    assert avg_len <= 10, f"Easy questions too long to type: avg={avg_len}"


def test_random_poly_medium_mathematical_accuracy():
    lengths = []
    for _ in range(100):
        expr, deriv = main.random_poly_medium()
        assert isinstance(expr, str) and expr
        assert isinstance(deriv, str) and deriv
        assert_calculus_pair_valid(expr, deriv)
        lengths.append(len(deriv))
    avg_len = sum(lengths) / len(lengths)
    assert avg_len <= 15, f"Medium questions too long to type: avg={avg_len}"


def test_random_poly_hard_mathematical_accuracy():
    for _ in range(100):
        res = main.random_poly_hard()
        expr, deriv = res[0], res[1]
        assert isinstance(expr, str) and expr
        assert isinstance(deriv, str) and deriv
        assert_calculus_pair_valid(expr, deriv)


def test_previously_broken_questions_accept_user_inputs():
    """Verify that formerly impossible questions are now graded 100% correctly."""
    test_cases = [
        # ln(x) -> 1/x
        ("1/x", "1/x"),
        ("1/x", "x^(-1)"),
        # 1/x -> -1/x^2
        ("-1/x^2", "-1/x^2"),
        ("-1/x^2", "-x^(-2)"),
        # 1/x^2 -> -2/x^3
        ("-2/x^3", "-2/x^3"),
        ("-2/x^3", "-2*x^(-3)"),
        # √x -> 1/(2·√x)
        ("1/(2·√x)", "1/(2*sqrt(x))"),
        ("1/(2·√x)", "1/(2*x^(1/2))"),
        # x/2 -> 1/2
        ("1/2", "1/2"),
        # 1/cos(x) -> sin(x)/cos(x)^2
        ("sin(x)/cos(x)^2", "sin(x)/cos(x)^2"),
        ("sin(x)/cos(x)^2", "tan(x)/cos(x)"),
        # Product rule x*e^x
        ("e^x + x·e^x", "e^x + x*e^x"),
        ("e^x + x·e^x", "(x + 1)*e^x"),
        ("e^x + x·e^x", "e^x*(1 + x)"),
    ]
    for expected, user_input in test_cases:
        assert main.check_math_equivalence(expected, user_input) is True, (
            f"Failed on expected='{expected}' vs user='{user_input}'"
        )


def test_generate_question_difficulty_scaling():
    # ELO < 1150: Difficulty 1
    q_easy = main.generate_question(900)
    assert q_easy["difficulty"] == 1
    assert q_easy["ask_for_derivative_only"] is True
    assert '<' not in q_easy["answer"]

    # ELO 1200-1499: Difficulty 2
    q_med = main.generate_question(1300)
    assert q_med["difficulty"] == 2
    assert q_med["ask_for_derivative_only"] is True
    assert '<' not in q_med["answer"]

    # ELO >= 1500: Difficulty 3
    q_hard = main.generate_question(1650)
    assert q_hard["difficulty"] == 3
    assert q_hard["ask_for_derivative_only"] is True
    assert '<' not in q_hard["answer"]
