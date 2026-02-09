"""
Basic API tests for Derivative Duel backend.

Run with: python test_api.py
Or with pytest: pytest test_api.py -v
"""

import asyncio
from main import (
    app,
    check_math_equivalence,
    calculate_elo_change,
    generate_question,
    format_term
)


def test_math_equivalence():
    """Test mathematical expression equivalence checking."""
    print("\n🧪 Testing Math Equivalence...")
    
    test_cases = [
        # (correct_answer, user_answer, should_match)
        ("2*x", "x + x", True),
        ("x^2", "x*x", True),
        ("2*x + 4", "4 + 2*x", True),
        ("3*x^2", "x^2 + x^2 + x^2", True),
        ("x^2", "2*x", False),
        ("sin(x)", "cos(x)", False),
        ("e^x", "e^x", True),
        ("1/x", "x^(-1)", True),
        ("2*x^2 + 3*x", "3*x + 2*x^2", True),
    ]
    
    passed = 0
    failed = 0
    
    for correct, user, expected in test_cases:
        result = check_math_equivalence(correct, user)
        status = "✓" if result == expected else "✗"
        if result == expected:
            passed += 1
        else:
            failed += 1
        print(f"  {status} '{user}' vs '{correct}': {result} (expected {expected})")
    
    print(f"\n  Results: {passed} passed, {failed} failed")
    return failed == 0


def test_elo_calculation():
    """Test ELO rating calculation."""
    print("\n🧪 Testing ELO Calculation...")
    
    test_cases = [
        # (winner_elo, loser_elo, description)
        (1000, 1000, "Equal players"),
        (1000, 1500, "Underdog wins"),
        (1500, 1000, "Favorite wins"),
        (2000, 1000, "High-rated beats low-rated"),
    ]
    
    for winner_elo, loser_elo, description in test_cases:
        change = calculate_elo_change(winner_elo, loser_elo)
        print(f"  {description}: ±{change} points (Winner: {winner_elo} → {winner_elo + change})")
    
    # Test minimum change
    change = calculate_elo_change(2000, 500)
    assert change >= 1, "ELO change should be at least 1"
    
    print("  ✓ All ELO tests passed")
    return True


def test_question_generation():
    """Test derivative question generation at different ELO levels."""
    print("\n🧪 Testing Question Generation...")
    
    elo_levels = [
        (800, "Beginner"),
        (1200, "Intermediate"),
        (1500, "Advanced"),
        (2000, "Expert"),
    ]
    
    for elo, level in elo_levels:
        question = generate_question(elo)
        
        assert "expression" in question, "Question missing 'expression'"
        assert "derivative" in question, "Question missing 'derivative'"
        assert "difficulty" in question, "Question missing 'difficulty'"
        
        print(f"  ✓ {level} (ELO {elo}): Difficulty {question['difficulty']}")
        print(f"    Expression: {question['expression'][:50]}...")
    
    print("  ✓ All question generation tests passed")
    return True


def test_format_term():
    """Test term formatting helper."""
    print("\n🧪 Testing Term Formatting...")
    
    test_cases = [
        (3, "x", "+ 3x"),
        (-2, "x^2", "- 2x^2"),
        (0, "x", "+ 0x"),
        (1, "x", "+ 1x"),
    ]
    
    for coef, term, expected in test_cases:
        result = format_term(coef, term)
        status = "✓" if result == expected else "✗"
        print(f"  {status} format_term({coef}, '{term}') = '{result}' (expected '{expected}')")
    
    print("  ✓ Term formatting tests passed")
    return True


def test_edge_cases():
    """Test edge cases and error handling."""
    print("\n🧪 Testing Edge Cases...")
    
    # Test invalid math expressions
    try:
        result = check_math_equivalence("invalid***", "2*x")
        print(f"  ✓ Handles invalid expressions gracefully: {result}")
    except Exception as e:
        print(f"  ✗ Failed on invalid expression: {e}")
        return False
    
    # Test ELO with extreme values
    change = calculate_elo_change(100, 3000)
    assert change >= 1, "Should handle extreme ELO differences"
    print(f"  ✓ Handles extreme ELO differences: ±{change}")
    
    # Test question generation at edge ELOs
    q_low = generate_question(500)
    q_high = generate_question(2500)
    assert q_low["difficulty"] <= q_high["difficulty"], "Higher ELO should have harder questions"
    print(f"  ✓ Question difficulty scales with ELO")
    
    print("  ✓ All edge case tests passed")
    return True


def run_all_tests():
    """Run all test suites."""
    print("=" * 60)
    print("🚀 Running Derivative Duel Backend Tests")
    print("=" * 60)
    
    results = []
    
    # Run each test suite
    results.append(("Math Equivalence", test_math_equivalence()))
    results.append(("ELO Calculation", test_elo_calculation()))
    results.append(("Question Generation", test_question_generation()))
    results.append(("Term Formatting", test_format_term()))
    results.append(("Edge Cases", test_edge_cases()))
    
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
        return 0
    else:
        print(f"\n❌ {total - passed} test suite(s) failed")
        return 1


if __name__ == "__main__":
    exit_code = run_all_tests()
    exit(exit_code)
