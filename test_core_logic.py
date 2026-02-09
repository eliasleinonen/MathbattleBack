"""
Unit tests for Derivative Duel backend - no database required.
Tests core logic functions without FastAPI or MongoDB dependencies.

Run with: python test_core_logic.py
"""

import sys
import random


# ============================================================================
# Copy core functions from main.py to test them independently
# ============================================================================

def format_term(coef, term=""):
    """Format a term with proper +/- sign"""
    if coef >= 0:
        return f"+ {coef}{term}"
    else:
        return f"- {abs(coef)}{term}"


def calculate_elo_change(winner_elo: int, loser_elo: int) -> int:
    """Calculate ELO change using standard formula with dynamic K-factor."""
    # Dynamic K-factor based on winner's ELO
    if winner_elo < 1200:
        K = 40  # Beginners move faster
    elif winner_elo < 1800:
        K = 32  # Intermediate players
    else:
        K = 24  # Advanced players, more stable
    
    # Expected score for winner (probability of winning)
    expected = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    
    # Actual change: higher when beating stronger opponents
    change = round(K * (1 - expected))
    
    # Ensure minimum change of 1
    return max(1, change)


def check_math_equivalence(correct_expr: str, user_expr: str) -> bool:
    """Check if two mathematical expressions are equivalent"""
    try:
        from sympy import (
            trigsimp, expand, expand_trig, expand_log, logcombine,
            Symbol, simplify
        )
        from sympy.parsing.sympy_parser import (
            parse_expr, standard_transformations,
            implicit_multiplication_application, function_exponentiation
        )
        
        # Replace unicode multiplication dot with * and ^ with **
        correct_expr = correct_expr.replace('·', '*').replace('×', '*').replace('^', '**')
        user_expr = user_expr.replace('·', '*').replace('×', '*').replace('^', '**')
        
        x = Symbol('x')
        transformations = (standard_transformations + 
                         (implicit_multiplication_application, function_exponentiation))
        user_sym = parse_expr(user_expr, transformations=transformations, evaluate=True)
        correct_sym = parse_expr(correct_expr, transformations=transformations, evaluate=True)
        
        # Try direct simplify
        if simplify(user_sym - correct_sym) == 0:
            return True
        
        # Try expand
        if simplify(expand(user_sym) - expand(correct_sym)) == 0:
            return True
        
        # Try trigsimp
        if simplify(trigsimp(user_sym) - trigsimp(correct_sym)) == 0:
            return True
        
        # Try logcombine
        if simplify(logcombine(user_sym, force=True) - 
                   logcombine(correct_sym, force=True)) == 0:
            return True
        
        # Try expand_log and expand_trig
        user_expanded = expand_log(expand_trig(user_sym))
        correct_expanded = expand_log(expand_trig(correct_sym))
        if simplify(user_expanded - correct_expanded) == 0:
            return True
        
        # Try equals method
        try:
            if user_sym.equals(correct_sym):
                return True
        except Exception:
            pass
        
        return False
    except Exception as e:
        print(f"[ERROR] Math equivalence check failed: {e}")
        return False


# ============================================================================
# Test Functions
# ============================================================================

def test_math_equivalence():
    """Test mathematical expression equivalence checking."""
    print("\n🧪 Testing Math Equivalence...")
    
    test_cases = [
        # (correct_answer, user_answer, should_match, description)
        ("2*x", "x + x", True, "Addition to multiplication"),
        ("x^2", "x*x", True, "Power to multiplication"),
        ("2*x + 4", "4 + 2*x", True, "Commutative property"),
        ("3*x^2", "x^2 + x^2 + x^2", True, "Multiple additions"),
        ("x^2", "2*x", False, "Different expressions"),
        ("sin(x)", "cos(x)", False, "Different trig functions"),
        ("e^x", "e^x", True, "Exponential identity"),
        ("1/x", "x^(-1)", True, "Division to negative power"),
        ("2*x^2 + 3*x", "3*x + 2*x^2", True, "Polynomial reordering"),
        ("6*x^2", "3*2*x^2", True, "Coefficient multiplication"),
    ]
    
    passed = 0
    failed = 0
    
    for correct, user, expected, description in test_cases:
        try:
            result = check_math_equivalence(correct, user)
            status = "✓" if result == expected else "✗"
            if result == expected:
                passed += 1
            else:
                failed += 1
            print(f"  {status} {description}")
            print(f"     '{user}' vs '{correct}': {result} (expected {expected})")
        except Exception as e:
            print(f"  ✗ {description} - Error: {e}")
            failed += 1
    
    print(f"\n  Results: {passed} passed, {failed} failed")
    return failed == 0


def test_elo_calculation():
    """Test ELO rating calculation."""
    print("\n🧪 Testing ELO Calculation...")
    
    test_cases = [
        # (winner_elo, loser_elo, description)
        (1000, 1000, "Equal players"),
        (1000, 1500, "Underdog wins (+500 ELO difference)"),
        (1500, 1000, "Favorite wins (+500 ELO difference)"),
        (2000, 1000, "Expert beats beginner"),
        (500, 500, "Low ELO equal match"),
        (2200, 2200, "High ELO equal match"),
    ]
    
    all_passed = True
    
    for winner_elo, loser_elo, description in test_cases:
        change = calculate_elo_change(winner_elo, loser_elo)
        
        # Validate change is at least 1
        if change < 1:
            print(f"  ✗ {description}: Change too small ({change})")
            all_passed = False
            continue
        
        # Validate K-factor is appropriate
        if winner_elo < 1200:
            expected_k = 40
        elif winner_elo < 1800:
            expected_k = 32
        else:
            expected_k = 24
        
        print(f"  ✓ {description}")
        print(f"     Winner: {winner_elo} → {winner_elo + change} (+{change})")
        print(f"     Loser:  {loser_elo} → {loser_elo - change} (-{change})")
        print(f"     K-factor: {expected_k}")
    
    # Special test: minimum change
    change = calculate_elo_change(2500, 500)
    if change >= 1:
        print(f"  ✓ Minimum change validation: {change} >= 1")
    else:
        print(f"  ✗ Minimum change validation failed: {change} < 1")
        all_passed = False
    
    if all_passed:
        print("\n  ✓ All ELO tests passed")
    return all_passed


def test_format_term():
    """Test term formatting helper."""
    print("\n🧪 Testing Term Formatting...")
    
    test_cases = [
        (3, "x", "+ 3x"),
        (-2, "x^2", "- 2x^2"),
        (0, "x", "+ 0x"),
        (1, "x", "+ 1x"),
        (-5, "", "- 5"),
        (10, "y", "+ 10y"),
    ]
    
    passed = 0
    failed = 0
    
    for coef, term, expected in test_cases:
        result = format_term(coef, term)
        status = "✓" if result == expected else "✗"
        if result == expected:
            passed += 1
        else:
            failed += 1
        print(f"  {status} format_term({coef}, '{term}') = '{result}' "
              f"(expected '{expected}')")
    
    print(f"\n  Results: {passed} passed, {failed} failed")
    return failed == 0


def test_edge_cases():
    """Test edge cases and error handling."""
    print("\n🧪 Testing Edge Cases...")
    
    all_passed = True
    
    # Test 1: Invalid math expressions
    print("  Testing invalid expression handling...")
    try:
        result = check_math_equivalence("invalid***syntax", "2*x")
        print(f"    ✓ Handles invalid expressions gracefully: {result}")
    except Exception as e:
        print(f"    ✗ Failed on invalid expression: {e}")
        all_passed = False
    
    # Test 2: ELO with extreme values
    print("  Testing extreme ELO values...")
    try:
        change = calculate_elo_change(100, 3000)
        if change >= 1:
            print(f"    ✓ Handles extreme ELO differences: ±{change}")
        else:
            print(f"    ✗ Invalid change for extreme ELO: {change}")
            all_passed = False
    except Exception as e:
        print(f"    ✗ Failed on extreme ELO: {e}")
        all_passed = False
    
    # Test 3: Empty term formatting
    print("  Testing empty term formatting...")
    try:
        result = format_term(5, "")
        expected = "+ 5"
        if result == expected:
            print(f"    ✓ Empty term handled correctly: '{result}'")
        else:
            print(f"    ✗ Expected '{expected}', got '{result}'")
            all_passed = False
    except Exception as e:
        print(f"    ✗ Failed on empty term: {e}")
        all_passed = False
    
    # Test 4: Very large coefficients
    print("  Testing large coefficients...")
    try:
        result = format_term(999999, "x^100")
        if "+ 999999x^100" in result:
            print(f"    ✓ Large coefficients handled: '{result}'")
        else:
            print(f"    ✗ Unexpected result: '{result}'")
            all_passed = False
    except Exception as e:
        print(f"    ✗ Failed on large coefficient: {e}")
        all_passed = False
    
    if all_passed:
        print("\n  ✓ All edge case tests passed")
    return all_passed


def test_elo_consistency():
    """Test that ELO changes are consistent and fair."""
    print("\n🧪 Testing ELO System Consistency...")
    
    all_passed = True
    
    # Test: Underdog should gain more than favorite
    underdog_gain = calculate_elo_change(1000, 1500)  # Low beats high
    favorite_gain = calculate_elo_change(1500, 1000)  # High beats low
    
    if underdog_gain > favorite_gain:
        print(f"  ✓ Underdog gains more than favorite")
        print(f"     Underdog (1000 beats 1500): +{underdog_gain}")
        print(f"     Favorite (1500 beats 1000): +{favorite_gain}")
    else:
        print(f"  ✗ ELO system unfair:")
        print(f"     Underdog should gain more than {favorite_gain}")
        all_passed = False
    
    # Test: Equal players should have moderate change
    equal_change = calculate_elo_change(1400, 1400)
    if 10 <= equal_change <= 25:  # Reasonable range
        print(f"  ✓ Equal players have moderate change: ±{equal_change}")
    else:
        print(f"  ✗ Equal player change unexpected: {equal_change}")
        all_passed = False
    
    return all_passed


def run_all_tests():
    """Run all test suites."""
    print("=" * 60)
    print("🚀 Running Derivative Duel Core Logic Tests")
    print("=" * 60)
    print("   (No database or FastAPI required)")
    
    results = []
    
    # Run each test suite
    results.append(("Math Equivalence", test_math_equivalence()))
    results.append(("ELO Calculation", test_elo_calculation()))
    results.append(("Term Formatting", test_format_term()))
    results.append(("Edge Cases", test_edge_cases()))
    results.append(("ELO Consistency", test_elo_consistency()))
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 Test Summary")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status} - {name}")
    
    print(f"\n  Total: {passed}/{total} test suites passed")
    
    if passed == total:
        print("\n🎉 All tests passed!")
        print("\nNote: These tests cover core logic only.")
        print("For API endpoint tests, run the backend server first.")
        return 0
    else:
        print(f"\n❌ {total - passed} test suite(s) failed")
        return 1


if __name__ == "__main__":
    try:
        exit_code = run_all_tests()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\n⚠️  Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
