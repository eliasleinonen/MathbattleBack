# Automated test for answer checking logic in main.py
from sympy import Symbol
import sys

# Import the answer checking logic from main.py
import importlib.util
import os

spec = importlib.util.spec_from_file_location("main", os.path.join(os.path.dirname(__file__), "main.py"))
main = importlib.util.module_from_spec(spec)
sys.modules["main"] = main
spec.loader.exec_module(main)

# Patch: extract the answer checking logic as a function
from sympy import SympifyError

def check_equiv(user_answer, correct_answer):
    try:
        # Use the same preprocess and logic as in main.py
        def preprocess(expr):
            import re
            s = str(expr).strip().replace(" ", "")
            s = s.replace("·", "*")
            s = s.replace("^", "**")
            s = re.sub(r'√([a-zA-Z0-9_]+)', r'sqrt(\1)', s)
            s = s.replace('ln(', 'log(')
            s = re.sub(r'(?<![\w)])-(sin|cos|tan|log|sqrt|exp)\(', r'-1*\1(', s)
            s = re.sub(r'(\d)([a-zA-Z])', r'\1*\2', s)
            s = re.sub(r'([a-zA-Z])(\d)', r'\1*\2', s)
            s = re.sub(r'([a-zA-Z0-9_])\(', r'\1*(', s)
            s = re.sub(r'\)([a-zA-Z0-9_])', r')*\1', s)
            s = re.sub(r'\*{2,}', '*', s)
            s = re.sub(r'(sin|cos|tan|log|sqrt|exp)(x)(?![a-zA-Z0-9_])', r'\1(x)', s)
            return s
        user_expr = preprocess(user_answer)
        correct_expr = preprocess(correct_answer)
        from sympy import trigsimp, expand, expand_trig, expand_log, logcombine, Pow, Function
        x = Symbol('x')
        user_sym = main.sympify(user_expr, evaluate=True)
        correct_sym = main.sympify(correct_expr, evaluate=True)
        # Try direct simplify
        if main.simplify(user_sym - correct_sym) == 0:
            return True
        # Try expand
        if main.simplify(main.expand(user_sym) - main.expand(correct_sym)) == 0:
            return True
        # Try trigsimp
        if main.simplify(main.trigsimp(user_sym) - main.trigsimp(correct_sym)) == 0:
            return True
        # Try logcombine
        if main.simplify(main.logcombine(user_sym, force=True) - main.logcombine(correct_sym, force=True)) == 0:
            return True
        # Try expand_log and expand_trig
        user_expanded = main.expand_log(main.expand_trig(user_sym))
        correct_expanded = main.expand_log(main.expand_trig(correct_sym))
        if main.simplify(user_expanded - correct_expanded) == 0:
            return True
        # Try SymPy's equals method
        try:
            if user_sym.equals(correct_sym):
                return True
        except Exception:
            pass
        # Try reciprocal root equivalence
        try:
            def try_root_equiv(expr1, expr2):
                from sympy import sqrt
                expr1_alt = expr1.replace(lambda e: isinstance(e, Pow) and e.exp == -1/2, lambda e: 1/sqrt(e.base))
                expr2_alt = expr2.replace(lambda e: isinstance(e, Pow) and e.exp == -1/2, lambda e: 1/sqrt(e.base))
                return main.simplify(expr1 - expr2_alt) == 0 or main.simplify(expr1_alt - expr2) == 0 or main.simplify(expr1_alt - expr2_alt) == 0
            if try_root_equiv(user_sym, correct_sym):
                return True
        except Exception:
            pass
        # Fallback: if both are the same function of x (e.g., cos(x)), accept
        try:
            if isinstance(user_sym, Function) and isinstance(correct_sym, Function):
                if user_sym.func == correct_sym.func and user_sym.args == correct_sym.args:
                    return True
        except Exception:
            pass
        # Numeric fallback
        try:
            from sympy import N
            for _ in range(5):
                val = main.random.uniform(1, 10)
                uval = N(user_sym.subs(x, val))
                cval = N(correct_sym.subs(x, val))
                if abs(uval - cval) > 1e-6:
                    break
            else:
                return True
        except Exception:
            pass
        return False
    except (SympifyError, Exception) as e:
        print(f"SymPy error: {e}")
        return False

tests = [
    # Trig
    ("cos(x)", "cos(x)", True),
    ("cos x", "cos(x)", True),
    ("cos(x)", "cos x", True),
    ("-sin(x)", "-sin(x)", True),
    ("-sin x", "-sin(x)", True),
    ("3*cos(3*x)", "3*cos(3*x)", True),
    ("3cos(3x)", "3*cos(3*x)", True),
    ("3·cos(3x)", "3*cos(3*x)", True),
    # Roots
    ("(1/2)*x^(-1/2)", "1/(2*sqrt(x))", True),
    ("1/(2*sqrt(x))", "(1/2)*x^(-1/2)", True),
    ("1/(2*sqrt(x))", "1/(2*sqrt(x))", True),
    # Poly
    ("2*x", "2x", True),
    ("2x", "2*x", True),
    ("2*x^2", "2x^2", True),
    ("2x^2", "2*x^2", True),
    # Wrong
    ("sin(x)", "cos(x)", False),
    ("-cos(x)", "cos(x)", False),
]

print("Testing answer checking logic:\n")
for user, correct, should_pass in tests:
    result = check_equiv(user, correct)
    status = "PASS" if result == should_pass else "FAIL" 
    print(f"User: {user:20} | Correct: {correct:20} | Expected: {should_pass} | Got: {result} | {status}")
