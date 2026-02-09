import sys
from sympy import sympify, simplify, Symbol, sqrt
from sympy.core.sympify import SympifyError

def check_equivalence(user_input, correct_answer):
    from sympy import Derivative, diff, Function

    def parse_derivative(expr):
        # Handle d/dx ...
        import re
        expr = expr.strip()
        # d/dx ...
        match = re.match(r'd/dx\s*(.*)', expr)
        if match:
            inner = match.group(1)
            x = Symbol('x')
            try:
                return diff(sympify(inner, locals={'sqrt': sqrt}), x)
            except Exception:
                return None
        # f'(x) or f''(x) etc. (only for f(x) = ... style, not general)
        match = re.match(r"([a-zA-Z]+)'\(([^)]*)\)", expr)
        if match:
            # Only handle f'(x) as diff(f(x), x)
            fname, var = match.groups()
            if fname == 'f' and var == 'x':
                # This is f'(x), so treat as diff(f(x), x)
                return Derivative(Function('f')(Symbol('x')), Symbol('x')).doit()
        return None

    import random
    from sympy import N
    import re
    def preprocess(expr):
        s = str(expr).strip().replace(" ", "").replace("·", "*")
        s = s.replace("^", "**")
        s = re.sub(r'√([a-zA-Z0-9_]+)', r'sqrt(\1)', s)
        s = s.replace('ln(', 'log(')
        # Only add * between letter/number and ( if not preceded by a function name
        # Avoid breaking sqrt(x) or log(x)
        s = re.sub(r'(?<![a-zA-Z_])([a-zA-Z])\(', r'\1*(', s)  # e.g. x( -> x*(
        s = re.sub(r'(\d)\(', r'\1*(', s)  # 2( -> 2*(
        s = re.sub(r'(\d)([a-zA-Z])', r'\1*\2', s)  # 2x -> 2*x
        s = re.sub(r'([a-zA-Z])(\d)', r'\1*\2', s)  # x2 -> x*2
        s = re.sub(r'\)([a-zA-Z0-9])', r')*\1', s)  # )x or )2 -> )*x or )*2
        return s

    user_expr = preprocess(user_input)
    correct_expr = preprocess(correct_answer)

    # If either input is a derivative, compute it
    user_deriv = parse_derivative(user_expr)
    correct_deriv = parse_derivative(correct_expr)
    if user_deriv is not None:
        user_expr = str(user_deriv)
    if correct_deriv is not None:
        correct_expr = str(correct_deriv)
    try:
        x = Symbol('x')
        # Pass sqrt as a known function to sympify
        user_sym = sympify(user_expr, locals={'sqrt': sqrt}, evaluate=True)
        correct_sym = sympify(correct_expr, locals={'sqrt': sqrt}, evaluate=True)

        from sympy import expand, expand_log, expand_trig, trigsimp, logcombine

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
        if simplify(logcombine(user_sym, force=True) - logcombine(correct_sym, force=True)) == 0:
            return True

        # Try expand_log and expand_trig
        user_expanded = expand_log(expand_trig(user_sym))
        correct_expanded = expand_log(expand_trig(correct_sym))
        if simplify(user_expanded - correct_expanded) == 0:
            return True

        # Try SymPy's equals method (symbolic)
        try:
            if user_sym.equals(correct_sym):
                return True
        except Exception:
            pass

        # Numeric fallback: test at random points (for expressions in x only)
        try:
            x = Symbol('x')
            for _ in range(5):
                val = random.uniform(1, 10)
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

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python check_math_equiv.py '<user_input>' '<correct_answer>'")
        sys.exit(1)
    user_input = sys.argv[1]
    correct_answer = sys.argv[2]
    result = check_equivalence(user_input, correct_answer)
    print("Equivalent" if result else "Not equivalent")
