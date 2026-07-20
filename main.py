
from sympy import sympify, simplify, Symbol, sqrt, Pow, count_ops
from sympy.core.sympify import SympifyError
from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List, Union
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
import asyncio
import logging
import os
import re
import secrets
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
import random
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google.auth.exceptions import GoogleAuthError
from dotenv import load_dotenv

load_dotenv()

# Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days
MONGODB_URL = os.getenv("DATABASE_URL", "mongodb://localhost:27017")
DATABASE_NAME = "derivative_duel"
# Google OAuth: sign-in and daily challenges require this to be set in the environment.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")

if not GOOGLE_CLIENT_ID:
    print("[WARNING] GOOGLE_CLIENT_ID is not set - Google login and daily challenges will be unavailable")

# Initialize FastAPI
app = FastAPI(title="Derivative Duel API")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mathbattle.xyz",
        "https://www.mathbattle.xyz",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:3001"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("derivative_duel")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Catch-all for uncaught errors so clients never see stack traces or internal
    messages. The full error is logged server-side; the client gets a generic text.
    Explicit HTTPExceptions are handled by FastAPI and keep their own detail.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong. Please try again."},
    )

# Security
security = HTTPBearer(auto_error=False)

# In-memory storage (will be replaced with MongoDB)
in_memory_users = {}
in_memory_matches = {}
in_memory_rounds = {}
matchmaking_queue = {}  # {user_id: {"elo": int, "joined_at": datetime}}
cancelled_users = set()  # Track users who cancelled matchmaking
time_trials = {}  # {trial_id: {"user_id": str, "questions": list, "start_time": datetime}}
daily_challenges_storage = {}  # {date: {"expression": str, "derivative": str, "answer": str}}
daily_completions_storage = {}  # {(user_id, date): {"time": float, "correct": bool, "rank": int}}
user_counter = 0
match_counter = 0
round_counter = 0
match_locks = {}  # {match_id: asyncio.Lock} - serializes round creation per match

# How long (seconds) a player can go without polling before we treat them as
# having left the match. The frontend polls status every 0.5s, so 12s covers
# slow networks and short refreshes without false positives.
PRESENCE_TIMEOUT_SECONDS = 12


def utc_now() -> datetime:
    """Timezone-aware current UTC time. Use for all round timing math."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Treat naive datetimes (Mongo/legacy docs) as UTC so aware/naive math never mixes."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_round_start(raw) -> Optional[datetime]:
    """Parse a round start timestamp that may be an ISO string or datetime."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return ensure_utc(datetime.fromisoformat(raw))
        except ValueError:
            return None
    return ensure_utc(raw)


def get_match_lock(match_id: str) -> asyncio.Lock:
    lock = match_locks.get(match_id)
    if lock is None:
        lock = asyncio.Lock()
        match_locks[match_id] = lock
    return lock


def mark_player_seen(match: dict, user_id) -> None:
    match.setdefault("player_last_seen", {})[str(user_id)] = utc_now()


def is_player_connected(match: dict, player_id) -> bool:
    """A player counts as connected if they polled recently (or never had to yet)."""
    if str(player_id) == "bot-opponent":
        return True
    last_seen = match.get("player_last_seen", {}).get(str(player_id))
    if last_seen is None:
        return True
    return (utc_now() - ensure_utc(last_seen)).total_seconds() <= PRESENCE_TIMEOUT_SECONDS

# Pre-generate 100 daily challenges
def initialize_daily_challenges():
    """Pre-generate daily challenges for the next 100 days"""
    today = datetime.now(timezone.utc).date()
    
    for day_offset in range(100):
        challenge_date = today + timedelta(days=day_offset)
        date_str = challenge_date.isoformat()
        
        if date_str not in daily_challenges_storage:
            # Use date hash for deterministic ELO
            date_hash = sum(ord(c) for c in date_str)
            elo_options = [1300, 1400, 1500, 1600]
            elo = elo_options[date_hash % len(elo_options)]
            
            # Seed random with date for consistency
            old_state = random.getstate()
            random.seed(date_str)
            question = generate_question(elo)
            random.setstate(old_state)
            
            daily_challenges_storage[date_str] = {
                "date": date_str,
                "expression": question["expression"],
                "derivative": question["derivative"],
                "answer": question["answer"],
                "difficulty": question.get("difficulty", 2)
            }
    
    print(f"[INFO] Pre-generated {len(daily_challenges_storage)} daily challenges")

# Database
client = AsyncIOMotorClient(MONGODB_URL)
db = client[DATABASE_NAME]
users_collection = db.users
matches_collection = db.matches
rounds_collection = db.rounds
daily_challenges_collection = db.DailyChallenge
daily_completions_collection = db.DailyChallengeCompletion


# Models
class Token(BaseModel):
    access_token: str
    token_type: str


class User(BaseModel):
    id: str
    email: str
    name: str
    username: Optional[str] = None
    elo: int
    wins: int
    losses: int


class MatchStart(BaseModel):
    mode: str  # "random" or "friend"
    continue_existing: Optional[bool] = False  # Whether to continue an existing match


class FriendMatchCreate(BaseModel):
    opponent_username: Optional[str] = None  # If searching by username


class FriendMatchJoin(BaseModel):
    match_code: str


class AnswerSubmit(BaseModel):
    match_id: str
    answer: Union[str, float]  # Can be either string (derivative) or number (evaluated)


class GoogleAuthRequest(BaseModel):
    token: str  # Google ID token


class SetUsernameRequest(BaseModel):
    username: str


# Helper functions
def verify_google_token(token: str) -> dict:
    """
    Verify a Google ID token and return its claims.

    Isolated in its own function so tests can stub it without reaching Google.
    Raises ValueError (bad signature/audience/expiry) or GoogleAuthError
    (wrong issuer) if the token is invalid, per the google-auth contract.
    """
    return id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """
    DEMO MODE: Always returns guest user for easy demo access.
    Supports unique guest IDs via "Bearer guest-UUID" tokens.
    """
    # 1. Check for explicit guest token "Bearer guest-UUID"
    if credentials:
        token = credentials.credentials
        if token.startswith("guest-"):
            # Use the provided guest ID
            guest_id = token
            return {
                "_id": guest_id,
                "email": f"{guest_id}@derivative-duel.com",
                "name": f"Guest {guest_id[-4:]}",
                "elo": 1000,
                "wins": 0,
                "losses": 0,
                "created_at": datetime.utcnow()
            }
    
    # 2. Default fallback (if no token or standard JWT processing fails)
    # We'll use a random ID here too if no token, 
    # BUT this is risky as subsequent requests (get_question) need the SAME ID.
    # For now, if no token, we return the generic one (browser should send guest token)
    default_guest = {
        "_id": "guest-user-id",
        "email": "guest@derivative-duel.com",
        "name": "Guest Player",
        "elo": 1000,
        "wins": 0,
        "losses": 0,
        "created_at": datetime.utcnow()
    }
    
    if not credentials:
        return default_guest
    
    try:
        # Try to decode JWT token
        token = credentials.credentials
        
        # Double check it's not a guest token we missed
        if token.startswith("guest-"):
            return {
                "_id": token,
                "email": f"{token}@derivative-duel.com",
                "name": f"Guest {token[-4:]}",
                "elo": 1000,
                "wins": 0,
                "losses": 0,
                "created_at": datetime.utcnow()
            }
            
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        
        if email is None:
            return default_guest
        
        # Get user from database
        user = await users_collection.find_one({"email": email})
        if user is None:
            return default_guest
        
        return user
        
    except JWTError:
        return default_guest


# Game Logic - Helper functions for derivative generation
def format_term(coef, term=""):
    """Format a term with proper +/- sign"""
    if coef >= 0:
        return f"+ {coef}{term}"
    else:
        return f"- {abs(coef)}{term}"

def random_poly_easy():
    """Generate easy polynomials (degree 2-3, 3-4 terms)"""
    expressions = []
    
    # x^3 with 3 terms
    a = random.randint(1, 3)
    b = random.randint(-4, 4)
    c = random.randint(-5, 5)
    if b != 0 and c != 0:
        expr = f"{a}·x^3 {format_term(b, '·x^2')} {format_term(c, '·x')}"
        deriv = f"{3*a}·x^2 {format_term(2*b, '·x')} {format_term(c)}"
        expressions.append((expr, deriv))
    
    # x^3 with 4 terms
    a = random.randint(1, 2)
    b = random.randint(-3, 3)
    c = random.randint(-4, 4)
    d = random.randint(-5, 5)
    if b != 0 and c != 0 and d != 0:
        expr = f"{a}·x^3 {format_term(b, '·x^2')} {format_term(c, '·x')} {format_term(d)}"
        deriv = f"{3*a}·x^2 {format_term(2*b, '·x')} {format_term(c)}"
        expressions.append((expr, deriv))
    
    # x^2 with 3 terms
    a = random.randint(1, 4)
    b = random.randint(-5, 5)
    c = random.randint(-6, 6)
    if b != 0 and c != 0:
        expr = f"{a}·x^2 {format_term(b, '·x')} {format_term(c)}"
        deriv = f"{2*a}·x {format_term(b)}"
        expressions.append((expr, deriv))

    if not expressions:
        return ("2·x^2 + 4·x + 6", "4·x + 4")
    return random.choice(expressions)

def random_poly_medium():
    """Generate medium polynomials (degree 4-5, 3-4 terms)"""
    expressions = []
    
    # x^4 with 4 terms
    a = random.randint(1, 2)
    b = random.randint(-3, 3)
    c = random.randint(-3, 3)
    d = random.randint(-4, 4)
    if b != 0 and c != 0 and d != 0:
        expr = f"{a}·x^4 {format_term(b, '·x^3')} {format_term(c, '·x^2')} {format_term(d, '·x')}"
        deriv = f"{4*a}·x^3 {format_term(3*b, '·x^2')} {format_term(2*c, '·x')} {format_term(d)}"
        expressions.append((expr, deriv))
    
    # x^5 with 4 terms
    a = random.randint(1, 2)
    b = random.randint(-2, 2)
    c = random.randint(-3, 3)
    d = random.randint(-3, 3)
    if b != 0 and c != 0 and d != 0:
        expr = f"{a}·x^5 {format_term(b, '·x^4')} {format_term(c, '·x^2')} {format_term(d, '·x')}"
        deriv = f"{5*a}·x^4 {format_term(4*b, '·x^3')} {format_term(2*c, '·x')} {format_term(d)}"
        expressions.append((expr, deriv))
    
    # x^4 with 3 terms
    a = random.randint(1, 3)
    b = random.randint(-3, 3)
    c = random.randint(-4, 4)
    if b != 0 and c != 0:
        expr = f"{a}·x^4 {format_term(b, '·x^2')} {format_term(c, '·x')}"
        deriv = f"{4*a}·x^3 {format_term(2*b, '·x')} {format_term(c)}"
        expressions.append((expr, deriv))
    
    # x^5 with 3 terms
    a = random.randint(1, 2)
    b = random.randint(-3, 3)
    c = random.randint(-3, 3)
    if b != 0 and c != 0:
        expr = f"{a}·x^5 {format_term(b, '·x^3')} {format_term(c, '·x')}"
        deriv = f"{5*a}·x^4 {format_term(3*b, '·x^2')} {format_term(c)}"
        expressions.append((expr, deriv))
    
    # Easy exponentials with superscript (expression has HTML, derivative uses ^)
    expressions.append(("e<sup>x</sup>", "e^x"))
    a = random.randint(2, 3)
    expressions.append((f"e<sup>{a}x</sup>", f"{a}·e^({a}·x)"))
    expressions.append((f"{a}·e<sup>x</sup>", f"{a}·e^x"))
    
    # Easy trig
    expressions.append(("sin(x)", "cos(x)"))
    expressions.append(("cos(x)", "-sin(x)"))
    a = random.randint(2, 3)
    expressions.append((f"sin({a}·x)", f"{a}·cos({a}·x)"))
    
    # Easy logarithms
    expressions.append(("ln(x)", "1/x"))
    a = random.randint(2, 4)
    expressions.append((f"{a}·ln(x)", f"{a}/x"))
    
    # Simple square root: √x = x^(1/2), derivative = (1/2)·x^(-1/2) = 1/(2√x)
    expressions.append(("√x", "1/(2·√x)"))
    a = random.randint(2, 4)
    expressions.append((f"{a}·√x", f"{a}/(2·√x)"))
    
    return random.choice(expressions)

def random_poly_hard():
    """Generate varied difficulty derivative questions"""
    expressions = []
    
    # Basic exponential and trig (keep original simple ones)
    expressions.append(("e<sup>x</sup>", "e^x", "is_exp"))
    a = random.randint(2, 5)
    expressions.append((f"e<sup>{a}x</sup>", f"{a}·e^({a}·x)", "is_exp"))
    
    expressions.append(("sin(x)", "cos(x)", "is_trig"))
    expressions.append(("cos(x)", "-sin(x)", "is_trig"))
    a = random.randint(2, 4)
    expressions.append((f"sin({a}·x)", f"{a}·cos({a}·x)", "is_trig"))
    expressions.append((f"cos({a}·x)", f"-{a}·sin({a}·x)", "is_trig"))
    
    expressions.append(("ln(x)", "<div class='fraction'><span class='numerator'>1</span><span class='denominator'>x</span></div>", "is_log"))
    
    # 1. 1/x → -1/x^2
    expressions.append((
        "<div class='fraction'><span class='numerator'>1</span><span class='denominator'>x</span></div>",
        "<div class='fraction'><span class='numerator'>-1</span><span class='denominator'>x^2</span></div>",
        None
    ))
    
    # 2. 1/x^2 → -2/x^3
    expressions.append((
        "<div class='fraction'><span class='numerator'>1</span><span class='denominator'>x<sup>2</sup></span></div>",
        "<div class='fraction'><span class='numerator'>-2</span><span class='denominator'>x^3</span></div>",
        None
    ))
    
    # 3. √x → 1/(2√x)
    expressions.append((
        "√x",
        "<div class='fraction'><span class='numerator'>1</span><span class='denominator'>2·√x</span></div>",
        None
    ))
    
    # 7. x/2 → 1/2
    expressions.append((
        "<div class='fraction'><span class='numerator'>x</span><span class='denominator'>2</span></div>",
        "<div class='fraction'><span class='numerator'>1</span><span class='denominator'>2</span></div>",
        None
    ))
    
    # 9. e^x + 2x → e^x + 2
    expressions.append((
        "e<sup>x</sup> + 2·x",
        "e^x + 2",
        "is_exp"
    ))
    
    # 10. e^x - x^2 → e^x - 2x
    expressions.append((
        "e<sup>x</sup> - x<sup>2</sup>",
        "e^x - 2·x",
        "is_exp"
    ))
    
    # 11. 3e^x → 3e^x
    a = random.randint(2, 5)
    expressions.append((
        f"{a}·e<sup>x</sup>",
        f"{a}·e^x",
        "is_exp"
    ))
    
    # 12. 2sin(x) → 2cos(x)
    a = random.randint(2, 4)
    expressions.append((
        f"{a}·sin(x)",
        f"{a}·cos(x)",
        "is_trig"
    ))
    
    # 13. x·e^x → e^x + x·e^x
    expressions.append((
        "x·e<sup>x</sup>",
        "e^x + x·e^x",
        "is_exp"
    ))
    
    # 14. (2x + 2)^3 → 6(2x + 2)^2
    expressions.append((
        "(2·x + 2)<sup>3</sup>",
        "6·(2·x + 2)^2",
        None
    ))
    
    # 16. e^(2x) → 2e^(2x)
    a = random.randint(2, 4)
    expressions.append((
        f"e<sup>{a}·x</sup>",
        f"{a}·e^({a}·x)",
        "is_exp"
    ))
    
    # 17. e^(3x) → 3e^(3x) (already covered by #16 with random)
    
    # 18. sin(2x) → 2cos(2x) (already covered in original)
    
    # 19. (x + 1)^2 → 2(x + 1)
    a = random.randint(1, 3)
    expressions.append((
        f"(x + {a})<sup>2</sup>",
        f"2·(x + {a})",
        None
    ))
    
    # 20. sin(e^x) → cos(e^x)·e^x
    expressions.append((
        "sin(e<sup>x</sup>)",
        "cos(e^x)·e^x",
        "is_trig"
    ))
    
    # 21. sin(e^(3x)) → 3e^(3x)·cos(e^(3x))
    a = random.randint(2, 4)
    expressions.append((
        f"sin(e<sup>{a}·x</sup>)",
        f"{a}·e^({a}·x)·cos(e^({a}·x))",
        "is_trig"
    ))
    
    # 22. e^(x^2) → 2x·e^(x^2)
    n = random.randint(2, 3)
    expressions.append((
        f"e<sup>x<sup>{n}</sup></sup>",
        f"{n}·x^{n-1}·e^(x^{n})",
        "is_exp"
    ))
    
    # 23. ln(2x) → 1/x
    a = random.randint(2, 5)
    expressions.append((
        f"ln({a}·x)",
        "<div class='fraction'><span class='numerator'>1</span><span class='denominator'>x</span></div>",
        "is_log"
    ))
    
    # 24. cos(x^2) → -2x·sin(x^2)
    n = random.randint(2, 3)
    expressions.append((
        f"cos(x<sup>{n}</sup>)",
        f"-{n}·x^{n-1}·sin(x^{n})",
        "is_trig"
    ))
    
    # Keep some harder quotient rule ones
    a = random.randint(2, 5)
    expressions.append((
        f"<div class='fraction'><span class='numerator'>1</span><span class='denominator'>{a}·e<sup>x</sup></span></div>",
        f"<div class='fraction'><span class='numerator'>-1</span><span class='denominator'>{a}·e^x</span></div>",
        "is_exp"
    ))
    
    expressions.append((
        "<div class='fraction'><span class='numerator'>1</span><span class='denominator'>cos(x)</span></div>",
        "<div class='fraction'><span class='numerator'>sin(x)</span><span class='denominator'>cos^2(x)</span></div>",
        "is_trig"
    ))
    
    # Reduce polynomial complexity - simpler ones
    a = random.randint(1, 3)
    b = random.randint(1, 4)
    expr = f"{a}·x<sup>3</sup> + {b}·x"
    deriv = f"{3*a}·x^2 + {b}"
    expressions.append((expr, deriv, None))
    
    choice = random.choice(expressions)
    if len(choice) == 3:
        return (choice[0], choice[1], choice[2])
    return (choice[0], choice[1], None)


def generate_question(elo: int) -> dict:
    """Generate a derivative question based on ELO"""
    
    # Select difficulty based on ELO - always ask for derivative only
    # 800-1200: Mix of easy and medium questions
    if elo < 1200:
        # 50/50 chance of easy or medium
        if random.choice([True, False]):
            expr, deriv = random_poly_easy()
            difficulty = 1
        else:
            expr, deriv = random_poly_medium()
            difficulty = 2
        special_type = None
    elif elo < 1500:
        expr, deriv = random_poly_medium()
        difficulty = 2
        special_type = None
    else:
        result = random_poly_hard()
        expr, deriv = result[0], result[1]
        special_type = result[2] if len(result) == 3 else None
        difficulty = 3
    
    # Always set evaluate_at for storage consistency
    evaluate_at = random.randint(1, 5)
    
    return {
        "expression": expr,
        "derivative": deriv,
        "evaluate_at": evaluate_at,
        "answer": deriv,  # The answer IS always the derivative
        "difficulty": difficulty,
        "ask_for_derivative_only": True
    }


def calculate_elo_change(winner_elo: int, loser_elo: int) -> int:
    """
    Calculate ELO change using standard formula with dynamic K-factor.
    - Higher K for lower ELO players (faster movement)
    - Lower K for higher ELO players (more stable)
    - Bigger changes when beating higher-rated opponents
    """
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


# --- Answer grading safety guards -------------------------------------------
# User answers are parsed with sympy's evaluate=True, so hostile inputs like
# power towers (9**9**9) can hang the worker. These guards reject such inputs
# cheaply, before any sympy parsing/evaluation happens.

MAX_ANSWER_LENGTH = 200
_MAX_POW_OPS = 8       # legitimate derivative answers use only a handful of powers
_MAX_OPERATORS = 40
_MAX_NUMERIC_EXPONENT = 100
# Chained exponentiation with a simple operand in between, e.g. 9**9**9.
_POW_TOWER_RE = re.compile(r"\*\*\s*[+-]?[\w.]+\s*\*\*")
_HUGE_INT_RE = re.compile(r"\d{7,}")
_LARGE_EXPONENT_RE = re.compile(r"\*\*\s*\(?\s*-?\s*\d{4,}")


def normalize_answer_text(expr: str) -> str:
    """Shared preprocessing: normalize unicode operators and ^ to Python syntax."""
    s = str(expr).strip()
    s = s.replace('·', '*').replace('×', '*').replace('∗', '*')
    s = s.replace('^', '**')
    return s


def _answer_looks_unsafe(expr: str) -> bool:
    """Cheap string-level check for inputs that could hang sympy evaluation."""
    s = normalize_answer_text(expr)
    if len(s) > MAX_ANSWER_LENGTH:
        return True
    if s.count('**') > _MAX_POW_OPS:
        return True
    if _POW_TOWER_RE.search(s):
        return True
    if _HUGE_INT_RE.search(s):
        return True
    if _LARGE_EXPONENT_RE.search(s):
        return True
    if sum(s.count(op) for op in '+-*/') > _MAX_OPERATORS:
        return True
    return False


def _parsed_answer_unsafe(expr_sym) -> bool:
    """Structural check on an unevaluated parse tree (parse_expr(..., evaluate=False)).

    Catches power towers and huge exponents that the string heuristics can miss
    (e.g. parenthesized towers like 9**((9**9))), before any evaluation happens.
    """
    try:
        for p in expr_sym.atoms(Pow):
            exp = p.exp
            for inner in exp.atoms(Pow):
                # Pow(Integer, -1) is just an unevaluated rational like 1/2.
                if inner.exp == -1 and getattr(inner.base, "is_Integer", False):
                    continue
                return True
            if exp.is_number:
                try:
                    if abs(float(exp)) > _MAX_NUMERIC_EXPONENT:
                        return True
                except (TypeError, ValueError, OverflowError):
                    return True
    except Exception:
        return True
    return False


def _expression_too_complex(expr_sym) -> bool:
    """Guard for the numeric fallback: skip substitution on power-heavy trees."""
    try:
        if len(expr_sym.atoms(Pow)) > _MAX_POW_OPS:
            return True
        if count_ops(expr_sym) > _MAX_OPERATORS:
            return True
    except Exception:
        return True
    return False


def check_math_equivalence(correct_expr: str, user_expr: str) -> bool:
    """Check if two mathematical expressions are equivalent"""
    try:
        from sympy import trigsimp, expand, expand_trig, expand_log, logcombine, Pow, Function, sqrt
        from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application, function_exponentiation
        
        # Replace unicode multiplication dot/cross with * and ^ with **
        correct_expr = normalize_answer_text(correct_expr)
        user_expr = normalize_answer_text(user_expr)
        
        # Reject inputs that could hang sympy before any parsing happens.
        if _answer_looks_unsafe(user_expr):
            print("[WARNING] Rejecting unsafe user expression in check_math_equivalence")
            return False
        
        x = Symbol('x')
        transformations = (standard_transformations + (implicit_multiplication_application, function_exponentiation))
        # Parse unevaluated first so power towers can be rejected before evaluation.
        if _parsed_answer_unsafe(parse_expr(user_expr, transformations=transformations, evaluate=False)):
            print("[WARNING] Rejecting unsafe user expression tree in check_math_equivalence")
            return False
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
        if simplify(logcombine(user_sym, force=True) - logcombine(correct_sym, force=True)) == 0:
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


# Initialize daily challenges on startup
initialize_daily_challenges()

# Load daily completions from MongoDB on startup
async def load_daily_completions():
    """Load all completions from MongoDB into memory"""
    try:
        # Load all completions (or at least the past year)
        year_ago = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
        completions = await daily_completions_collection.find({"date": {"$gte": year_ago}}).to_list(None)
        for comp in completions:
            key = (comp["user_id"], comp["date"])
            daily_completions_storage[key] = {
                "time": comp["time"],
                "correct": comp.get("correct", False),
                "rank": comp.get("rank")
            }
        print(f"[INFO] Loaded {len(completions)} completions from MongoDB")
    except Exception as e:
        print(f"[WARNING] Failed to load completions from MongoDB: {e}")

@app.on_event("startup")
async def startup_event():
    """Called when the app starts"""
    await load_daily_completions()

# Routes
@app.get("/")
async def root():
    return {"message": "Derivative Duel API"}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    """Liveness check for uptime monitors (GET and HEAD)."""
    return {"status": "ok"}


@app.get("/api/server-time")
async def get_server_time():
    """Return the server's current date/time in UTC"""
    now = datetime.now(timezone.utc)
    return {
        "iso": now.isoformat(),
        "date": now.date().isoformat(),
        "timestamp": now.timestamp()
    }


@app.post("/api/auth/google", response_model=Token)
async def google_auth(auth_request: GoogleAuthRequest):
    """
    Log in or register with a Google ID token.

    This is the only account-based sign-in path; there is no email/password auth.
    On success we upsert the user in MongoDB and issue our own JWT keyed by email,
    which get_current_user then resolves back to the stored user.
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google login is not configured")

    # google-auth raises ValueError for bad signature/audience/expiry and
    # GoogleAuthError for a wrong issuer; both mean the token is untrustworthy.
    try:
        idinfo = verify_google_token(auth_request.token)
    except (ValueError, GoogleAuthError):
        raise HTTPException(status_code=401, detail="Invalid Google token")

    email = idinfo.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Google token is missing an email")

    # Only trust an email Google has actually verified, since accounts are keyed by it.
    if not idinfo.get("email_verified"):
        raise HTTPException(status_code=401, detail="Google email is not verified")

    name = idinfo.get("name") or email.split("@")[0]

    # Upsert: create the user on first sign-in, otherwise keep their existing record.
    user = await users_collection.find_one({"email": email})
    if user is None:
        await users_collection.insert_one({
            "email": email,
            "name": name,
            "elo": 1000,
            "wins": 0,
            "losses": 0,
            "created_at": datetime.now(timezone.utc),
        })

    access_token = create_access_token({"sub": email})
    return {"access_token": access_token, "token_type": "bearer"}



@app.get("/api/user/profile", response_model=User)
async def get_profile(current_user = Depends(get_current_user)):
    return {
        "id": str(current_user["_id"]),
        "email": current_user["email"],
        "name": current_user["name"],
        "username": current_user.get("username"),
        "elo": current_user["elo"],
        "wins": current_user["wins"],
        "losses": current_user["losses"]
    }


@app.post("/api/user/set-username")
async def set_username(request: SetUsernameRequest, current_user = Depends(get_current_user)):
    username = request.username.strip()
    
    # Validate username
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be at least 3 characters")
    
    if len(username) > 20:
        raise HTTPException(status_code=400, detail="Username must be less than 20 characters")
    
    # Check if username already exists
    existing = await users_collection.find_one({"username": username})
    if existing and str(existing["_id"]) != str(current_user["_id"]):
        raise HTTPException(status_code=400, detail="Username already taken")
    
    # Update username
    await users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$set": {"username": username}}
    )
    
    return {"success": True, "username": username}


@app.get("/api/users/search")
async def search_users(username: str, current_user = Depends(get_current_user)):
    """Search for users by username"""
    if len(username) < 2:
        return []
    
    # Search for users with usernames matching the query
    users_cursor = users_collection.find({
        "username": {"$regex": f"^{username}", "$options": "i"},
        "_id": {"$ne": current_user.get("_id")}  # Exclude current user
    }).limit(10)
    
    users = await users_cursor.to_list(length=10)
    
    return [
        {
            "username": user.get("username"),
            "elo": user.get("elo", 1000)
        }
        for user in users if user.get("username")
    ]


@app.post("/api/game/friend/create")
async def create_friend_match(data: FriendMatchCreate, current_user = Depends(get_current_user)):
    """Create a friend match - either with a specific user or generate a shareable code"""
    global match_counter
    
    # Generate a unique 6-character match code
    match_code = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=6))
    
    # Ensure match code is unique
    while await matches_collection.find_one({"match_code": match_code}):
        match_code = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=6))
    
    # If opponent username is provided, look them up
    opponent_id = None
    opponent_elo = 1000
    if data.opponent_username:
        opponent = await users_collection.find_one({"username": data.opponent_username})
        if opponent:
            opponent_id = opponent["_id"]
            opponent_elo = opponent.get("elo", 1000)
    
    # Create match with timestamp-based unique ID
    match_id = f"match-{int(datetime.utcnow().timestamp() * 1000)}-{random.randint(1000, 9999)}"
    
    match_doc = {
        "_id": match_id,
        "match_code": match_code,
        "match_type": "friend",  # Mark as friend match
        "player1_id": current_user["_id"],
        "player1_username": current_user.get("username", current_user.get("name")),
        "player2_id": opponent_id,  # None if waiting for someone to join
        "player2_username": data.opponent_username if data.opponent_username else None,
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": current_user["elo"],
        "player2_elo": opponent_elo,
        "status": "pending" if opponent_id else "waiting",  # pending = waiting for opponent to accept
        "winner_id": None,
        "elo_change": 0,
        "created_at": datetime.utcnow()
    }
    
    # Store in both memory and database
    in_memory_matches[match_id] = match_doc
    await matches_collection.insert_one(match_doc.copy())
    
    # Generate shareable link
    link = f"http://localhost:3000/play/friend/{match_code}"
    
    return {
        "match_id": match_id,
        "match_code": match_code,
        "link": link,
        "status": match_doc["status"]
    }


@app.post("/api/game/friend/join")
async def join_friend_match(data: FriendMatchJoin, current_user = Depends(get_current_user)):
    """Join a friend match using a match code"""
    # Find match by code in database first
    match = await matches_collection.find_one({"match_code": data.match_code.upper()})
    
    if not match:
        # Try in-memory matches as fallback
        for mid, m in in_memory_matches.items():
            if m.get("match_code") == data.match_code.upper():
                match = m
                break
    
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    
    match_id = match["_id"]
    
    if match["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Match already started")
    
    if match["player1_id"] == current_user["_id"]:
        raise HTTPException(status_code=400, detail="Cannot join your own match")
    
    # Join as player 2
    match["player2_id"] = current_user["_id"]
    match["player2_elo"] = current_user["elo"]
    match["status"] = "active"
    
    # Update in memory
    if match_id in in_memory_matches:
        in_memory_matches[match_id] = match
    
    # Update in database
    await matches_collection.update_one(
        {"_id": match_id},
        {"$set": {
            "player2_id": current_user["_id"],
            "player2_elo": current_user["elo"],
            "status": "active"
        }}
    )
    
    return {
        "match_id": match_id,
        "status": "active"
    }


@app.get("/api/game/friend/status/{match_code}")
async def get_match_status(match_code: str):
    """Check if a friend match is ready (doesn't require auth)"""
    # Find match by code in database
    match = await matches_collection.find_one({"match_code": match_code.upper()})
    
    if not match:
        # Try in-memory matches as fallback
        for mid, m in in_memory_matches.items():
            if m.get("match_code") == match_code.upper():
                match = m
                break
    
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    
    return {
        "match_id": match["_id"],
        "status": match["status"],
        "player1_ready": match["player1_id"] is not None,
        "player2_ready": match["player2_id"] is not None
    }


@app.get("/api/user/me")
async def get_current_user_info(current_user = Depends(get_current_user)):
    """Get current user information"""
    return {
        "id": str(current_user["_id"]),
        "email": current_user.get("email"),
        "name": current_user.get("name"),
        "username": current_user.get("username"),
        "elo": current_user.get("elo", 1000)
    }


@app.get("/api/challenges/pending")
async def get_pending_challenges(current_user = Depends(get_current_user)):
    """Get challenges waiting for this user to accept"""
    # Find matches where this user is player2 and status is pending
    challenges = await matches_collection.find({
        "player2_id": current_user["_id"],
        "status": "pending"
    }).to_list(length=10)
    
    return [
        {
            "match_id": c["_id"],
            "match_code": c["match_code"],
            "challenger": c.get("player1_username", "Unknown"),
            "created_at": c["created_at"]
        }
        for c in challenges
    ]


@app.post("/api/challenges/accept/{match_id}")
async def accept_challenge(match_id: str, current_user = Depends(get_current_user)):
    """Accept a pending challenge"""
    # Find the match
    match = await matches_collection.find_one({"_id": match_id})
    
    if not match:
        raise HTTPException(status_code=404, detail="Challenge not found")
    
    if match["player2_id"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your challenge to accept")
    
    if match["status"] != "pending":
        raise HTTPException(status_code=400, detail="Challenge already accepted or expired")
    
    # Update match status to active
    await matches_collection.update_one(
        {"_id": match_id},
        {"$set": {"status": "active"}}
    )
    
    # Update in memory if exists
    if match_id in in_memory_matches:
        in_memory_matches[match_id]["status"] = "active"
    
    return {
        "match_id": match_id,
        "match_code": match["match_code"],
        "status": "active"
    }


@app.post("/api/challenges/cancel/{match_id}")
async def cancel_challenge(match_id: str, current_user = Depends(get_current_user)):
    """Cancel a pending challenge that you created"""
    # Find the match
    match = await matches_collection.find_one({"_id": match_id})
    
    if not match:
        raise HTTPException(status_code=404, detail="Challenge not found")
    
    # Only the creator (player1) can cancel
    if match["player1_id"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your challenge to cancel")
    
    if match["status"] != "pending" and match["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Challenge already active or completed")
    
    # Delete the match from database
    await matches_collection.delete_one({"_id": match_id})
    
    # Remove from memory if exists
    if match_id in in_memory_matches:
        del in_memory_matches[match_id]
    
    return {"status": "cancelled"}


@app.post("/api/game/start")
async def start_match(match_data: MatchStart, current_user = Depends(get_current_user)):
    global match_counter
    
    user_id = str(current_user["_id"])  # Convert to string for consistent comparison
    user_elo = current_user["elo"]
    
    # Check if user has a recently created active match (created in last 5 seconds by another player joining)
    for match_id, match in in_memory_matches.items():
        if match.get("status") == "active" and (str(match["player1_id"]) == user_id or str(match["player2_id"]) == user_id):
            # If match is very recent (less than 5 seconds old), it's a new match from matchmaking
            match_age = (datetime.utcnow() - match.get("created_at", datetime.utcnow())).total_seconds()
            if match_age < 5:
                opponent_id = match["player1_id"] if str(match["player2_id"]) == user_id else match["player2_id"]
                opponent = await users_collection.find_one({"_id": opponent_id})
                return {
                    "status": "matched",
                    "match_id": match_id,
                    "match_code": match.get("match_code"),
                    "opponent": opponent.get("username", "Player") if opponent else "Player"
                }
            else:
                # Old match from previous session, mark as abandoned
                if not match_data.continue_existing:
                    match["status"] = "abandoned"
    
    # Try to find any opponent from the queue (no ELO restriction)
    opponent_id = None
    for queued_id, queued_data in list(matchmaking_queue.items()):
        if queued_id != user_id:
            opponent_id = queued_id
            break
    
    # If found opponent, create match
    if opponent_id:
        result = (await users_collection.find_one({"_id": ObjectId(opponent_id)})) if ObjectId.is_valid(opponent_id) else {"_id": opponent_id, "elo": 1000}
        opponent = result or {"_id": opponent_id, "elo": 1000}
        # Remove both from queue
        matchmaking_queue.pop(user_id, None)
        matchmaking_queue.pop(opponent_id, None)
        
        # Check if either user cancelled during matchmaking
        if user_id in cancelled_users or opponent_id in cancelled_users:
            cancelled_users.discard(user_id)
            cancelled_users.discard(opponent_id)
            return {"status": "cancelled"}
        
        # Create match
        match_counter += 1
        match_id = f"match-{match_counter}"
        match_code = secrets.token_urlsafe(8)  # Generate unique code
        
        match_doc = {
            "_id": match_id,
            "match_code": match_code,
            "match_type": "ranked",  # Human vs human
            "player1_id": ObjectId(user_id) if ObjectId.is_valid(user_id) else user_id,
            "player2_id": ObjectId(opponent_id) if ObjectId.is_valid(opponent_id) else opponent_id,
            "player1_score": 0,
            "player2_score": 0,
            "player1_elo": user_elo,
            "player2_elo": opponent["elo"],
            "status": "active",
            "winner_id": None,
            "elo_change": 0,
            "rounds": [],  # Store all rounds data
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        
        in_memory_matches[match_id] = match_doc
        # Save to database (check if exists first to avoid duplicate key error)
        existing_match = await matches_collection.find_one({"_id": match_id})
        if not existing_match:
            await matches_collection.insert_one(match_doc.copy())
        else:
            # Update existing match
            await matches_collection.update_one(
                {"_id": match_id},
                {"$set": match_doc}
            )
        
        return {
            "status": "matched",
            "match_id": match_id,
            "match_code": match_code,
            "opponent": opponent.get("username", "Player")
        }
    
    # Check if user is already in queue
    if user_id in matchmaking_queue:
        # Check if 10 seconds have passed
        time_in_queue = (datetime.utcnow() - matchmaking_queue[user_id]["joined_at"]).total_seconds()
        
        # Still searching
        if time_in_queue < 10:
            return {
                "status": "searching",
                "time_remaining": int(10 - time_in_queue)
            }
        # Time expired, create bot match
    else:
        # Add user to matchmaking queue
        matchmaking_queue[user_id] = {
            "elo": user_elo,
            "joined_at": datetime.utcnow()
        }
        return {
            "status": "searching",
            "time_remaining": 10
        }
    
    # No opponent found within ELO range, create bot match after 10 seconds
    matchmaking_queue.pop(user_id, None)
    
    # Check if user cancelled during matchmaking
    if user_id in cancelled_users:
        cancelled_users.discard(user_id)
        return {"status": "cancelled"}
    
    # Create bot opponent with calibrated difficulty and random name
    bot_names = ["James (bot)", "Alex (bot)", "Sam (bot)", "Taylor (bot)", "Jordan (bot)", "Casey (bot)", "Morgan (bot)"]
    bot_name = random.choice(bot_names)
    bot_elo_offset = random.randint(-150, -50)  # Bot is 50-150 ELO lower
    bot = {
        "_id": "bot-opponent",
        "email": "bot@derivative-duel.com",
        "name": bot_name,
        "elo": user_elo + bot_elo_offset,
        "wins": 0,
        "losses": 0
    }
    
    # Create match
    match_counter += 1
    match_id = f"match-{match_counter}"
    match_code = secrets.token_urlsafe(8)  # Generate unique code
    
    match_doc = {
        "_id": match_id,
        "match_code": match_code,
        "match_type": "random",  # Bot match
        "player1_id": ObjectId(user_id) if ObjectId.is_valid(user_id) else user_id,
        "player2_id": bot["_id"],
        "player1_score": 0,
        "player2_score": 0,
        "player1_elo": user_elo,
        "player2_elo": bot["elo"],
        "status": "active",
        "winner_id": None,
        "elo_change": 0,
        "rounds": [],  # Store all rounds data
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }
    
    in_memory_matches[match_id] = match_doc
    # Save to database (check if exists first to avoid duplicate key error)
    existing_match = await matches_collection.find_one({"_id": match_id})
    if not existing_match:
        await matches_collection.insert_one(match_doc.copy())
    else:
        # Update existing match
        await matches_collection.update_one(
            {"_id": match_id},
            {"$set": match_doc}
        )
    
    return {
        "status": "matched",
        "match_id": match_id,
        "match_code": match_code,
        "opponent": bot_name
    }


@app.post("/api/game/cancel")
async def cancel_matchmaking(current_user = Depends(get_current_user)):
    """Remove user from matchmaking queue and mark as cancelled"""
    user_id = str(current_user["_id"])
    matchmaking_queue.pop(user_id, None)
    cancelled_users.add(user_id)  # Mark as cancelled to prevent match creation
    return {"status": "cancelled"}


@app.get("/api/game/active")
async def get_active_match(current_user = Depends(get_current_user)):
    """Check if user has an active match"""
    user_id = str(current_user["_id"])
    
    for match_id, match in in_memory_matches.items():
        if match.get("status") == "active" and (str(match["player1_id"]) == user_id or str(match["player2_id"]) == user_id):
            # Get opponent info
            opponent_id = match["player1_id"] if str(match["player2_id"]) == user_id else match["player2_id"]
            opponent = await users_collection.find_one({"_id": opponent_id})
            
            return {
                "has_active_match": True,
                "match_id": match_id,
                "match_type": match.get("match_type", "random"),
                "opponent": opponent.get("username", "Player") if opponent else "AI Opponent"
            }
    
    return {"has_active_match": False}


@app.get("/api/game/match/{match_code}")
async def get_match_by_code(match_code: str, current_user = Depends(get_current_user)):
    """Get match details by match code"""
    user_id = str(current_user["_id"])
    
    # Find match by code
    from bson import ObjectId, errors as bson_errors
    def is_valid_objectid(oid):
        try:
            ObjectId(oid)
            return True
        except bson_errors.InvalidId:
            return False

    for match_id, match in in_memory_matches.items():
        if match.get("match_code") == match_code:
            # Verify user is part of this match
            if str(match["player1_id"]) == user_id or str(match["player2_id"]) == user_id:
                # Determine if current user is player1 or player2
                is_player1 = str(match["player1_id"]) == user_id
                opponent_id = match["player2_id"] if is_player1 else match["player1_id"]

                # Get opponent info only if valid ObjectId
                if is_valid_objectid(opponent_id):
                    opponent_user = await users_collection.find_one({"_id": ObjectId(opponent_id)})
                    opponent_name = opponent_user.get("username", opponent_user.get("name", "Opponent")) if opponent_user else "Opponent"
                else:
                    opponent_user = None
                    if "guest" in str(opponent_id):
                        opponent_name = "Guest"
                    elif "bot" in str(opponent_id):
                        opponent_name = "Bot"
                    else:
                        opponent_name = "Player"

                # Check if opponent is bot (if they're a string ID like 'bot-opponent')
                is_opponent_bot = isinstance(opponent_id, str) and ("bot" in str(opponent_id) or opponent_name.endswith('(bot)'))

                return {
                    "match_id": match_id,
                    "status": match.get("status"),
                    "player1_id": str(match["player1_id"]),
                    "player2_id": str(match["player2_id"]),
                    "player1_score": match.get("player1_score", 0),
                    "player2_score": match.get("player2_score", 0),
                    "current_round": match.get("current_round", 0),
                    "is_player1": is_player1,
                    "opponent_name": opponent_name,
                    "is_opponent_bot": is_opponent_bot
                }
            else:
                raise HTTPException(status_code=403, detail="Not authorized to access this match")
    
    raise HTTPException(status_code=404, detail="Match not found")


def _question_response(round_id: str, round_doc: dict, round_start_time) -> dict:
    """Client-facing question payload; shared by resume and creation paths."""
    response = {
        "round_id": round_id,
        "expression": round_doc["question"],
        "evaluate_at": round_doc["evaluate_at"],
        "ask_for_derivative_only": round_doc.get("ask_for_derivative_only", True),
        "round_start_time": round_start_time,
    }
    if "time_limit" in round_doc:
        response["time_limit"] = round_doc["time_limit"]
    return response


@app.get("/api/game/question")
async def get_question(match_id: str, current_user = Depends(get_current_user)):
    match = in_memory_matches.get(match_id)

    if not match:
        # Try to load from database
        match = await matches_collection.find_one({"_id": match_id})
        if match:
            in_memory_matches[match_id] = match
        else:
            raise HTTPException(status_code=404, detail="Match not found")
    user_id = str(current_user["_id"])
    if user_id not in (str(match["player1_id"]), str(match["player2_id"])):
        raise HTTPException(status_code=403, detail="Not your match")
    mark_player_seen(match, user_id)

    # Check if match is already completed
    if match.get("status") == "completed":
        raise HTTPException(status_code=400, detail="Match is already completed")

    # Serialize round lookup/creation per match: after a round ends, both
    # clients request the next question at nearly the same time. Without the
    # lock each request created its own round and the players ended up on
    # different questions.
    async with get_match_lock(match_id):
        match = in_memory_matches.get(match_id, match)

        # Check if there's already a current round
        current_round_id = match.get("current_round_id")
        if current_round_id and current_round_id in in_memory_rounds:
            round_doc = in_memory_rounds[current_round_id]
            if not round_doc.get("winner_id"):
                round_start = parse_round_start(round_doc.get("created_at"))
                timed_out = (
                    round_start is not None
                    and (utc_now() - round_start).total_seconds() > 300  # 5 minutes
                )
                if not timed_out:
                    # Round still in progress - both players get the same question
                    return _question_response(
                        current_round_id, round_doc, match.get("round_start_time")
                    )

                # Timed out - mark round as tie, then fall through to create a new one
                round_doc["winner_id"] = "tie"
                in_memory_rounds[current_round_id] = round_doc
                await rounds_collection.update_one(
                    {"_id": current_round_id},
                    {"$set": {"winner_id": "tie"}}
                )
                await matches_collection.update_one(
                    {"_id": match_id, "rounds.round_number": round_doc["round_number"]},
                    {"$set": {"rounds.$.winner": "tie"}}
                )

        return await _create_next_round(match_id, match)


async def _create_next_round(match_id: str, match: dict) -> dict:
    """Create the next round for a match. Caller must hold the match lock."""
    # Use the LOWER ELO to ensure both players see the same difficulty question
    lower_elo = min(match["player1_elo"], match["player2_elo"])
    question = generate_question(lower_elo)

    # Count rounds for this match
    round_count = len([r for r in in_memory_rounds.values() if r["match_id"] == match_id])

    # Set round start time for synchronization (3 seconds from now).
    # Timezone-aware so the ISO string carries an offset and browsers in any
    # timezone parse it as the correct instant.
    round_start_time = utc_now() + timedelta(seconds=3)
    in_memory_matches[match_id]["round_start_time"] = round_start_time.isoformat()

    # Deterministic per-match round id so a retried/duplicated creation cannot
    # fork the match into two different "current" rounds.
    round_id = f"round-{match_id}-{round_count + 1}"
    round_doc = {
        "_id": round_id,
        "match_id": match_id,
        "round_number": round_count + 1,
        "question": question["expression"],
        "answer": question["answer"],
        "evaluate_at": question["evaluate_at"],
        "ask_for_derivative_only": question.get("ask_for_derivative_only", True),
        "difficulty": question["difficulty"],
        "player1_answer": None,
        "player2_answer": None,
        "winner_id": None,
        "created_at": utc_now()
    }
    # If this is a bot match, calculate time_limit based on user ELO
    is_bot_match = match.get("match_type") == "random" and match["player2_id"] == "bot-opponent"
    time_limit = None
    if is_bot_match:
        user_elo = match["player1_elo"]
        # Time limit based on user ELO and question difficulty
        # Lower ELO = more time, higher difficulty = more time
        if user_elo <= 1000:
            base_time = 15
        elif user_elo <= 1400:
            base_time = 12
        elif user_elo <= 1800:
            base_time = 10
        else:
            base_time = 8
        
        # Add time based on difficulty (1-5)
        difficulty_bonus = question["difficulty"] * 1
        time_limit = base_time + difficulty_bonus
        round_doc["time_limit"] = time_limit
    
    # Store in both memory and database
    in_memory_rounds[round_id] = round_doc
    
    # Check if round already exists in database before inserting
    existing_round = await rounds_collection.find_one({"_id": round_id})
    if not existing_round:
        await rounds_collection.insert_one(round_doc.copy())
    
    # Store current round ID in match and add round to rounds array
    in_memory_matches[match_id]["current_round_id"] = round_id
    round_summary = {
        "round_number": (match["player1_score"] + match["player2_score"] + 1),
        "question": question["expression"],
        "derivative": question["derivative"],
        "evaluate_at": question["evaluate_at"],
        "ask_for_derivative_only": question.get("ask_for_derivative_only", True),
        "answer": question["answer"],
        "difficulty": question["difficulty"],
        "started_at": round_start_time.isoformat(),
        "winner": None,
        "player1_answer": None,
        "player2_answer": None
    }
    
    await matches_collection.update_one(
        {"_id": match_id},
        {
            "$set": {"current_round_id": round_id, "round_start_time": round_start_time.isoformat(), "updated_at": datetime.utcnow()},
            "$push": {"rounds": round_summary}
        }
    )
    
    return _question_response(round_id, round_doc, round_start_time.isoformat())


@app.post("/api/game/give-up")
async def give_up_round(match_id: str, current_user = Depends(get_current_user)):
    """Mark that player wants to give up current round"""
    match = in_memory_matches.get(match_id)

    
    if not match:
        match = await matches_collection.find_one({"_id": match_id})
        if match:
            in_memory_matches[match_id] = match
        else:
            raise HTTPException(status_code=404, detail="Match not found")
    
    user_id = str(current_user["_id"])
    if user_id not in (str(match["player1_id"]), str(match["player2_id"])):
        raise HTTPException(status_code=403, detail="Not your match")
    mark_player_seen(match, user_id)

    round_id = match.get("current_round_id")
    if not round_id:
        raise HTTPException(status_code=404, detail="No active round")
    
    round_doc = in_memory_rounds.get(round_id)
    if not round_doc:
        round_doc = await rounds_collection.find_one({"_id": round_id})
        if round_doc:
            in_memory_rounds[round_id] = round_doc
        else:
            raise HTTPException(status_code=404, detail="Round not found")
    
    # Check if round already has a winner
    if round_doc.get("winner_id"):
        return {"status": "already_ended", "round_winner": str(round_doc["winner_id"])}
    
    # Determine if this is player1 or player2
    is_player1 = str(current_user["_id"]) == str(match["player1_id"])
    give_up_field = "player1_gave_up" if is_player1 else "player2_gave_up"
    
    # Mark player as gave up
    if give_up_field not in round_doc:
        round_doc["player1_gave_up"] = False
        round_doc["player2_gave_up"] = False
    
    round_doc[give_up_field] = True
    in_memory_rounds[round_id] = round_doc
    
    # Update in database
    await rounds_collection.update_one(
        {"_id": round_id},
        {"$set": {give_up_field: True}}
    )
    
    # Check if this is a bot match - bot automatically gives up too
    is_bot_match = match.get("match_type") == "random" and match["player2_id"] == "bot-opponent"

    # If the opponent stopped polling (closed the tab, lost connection), treat
    # their give-up as automatic so the remaining player can advance instead of
    # waiting forever for someone who left.
    opponent_id = match["player2_id"] if is_player1 else match["player1_id"]
    opponent_left = not is_player_connected(match, opponent_id)

    if is_bot_match or opponent_left:
        round_doc["player1_gave_up"] = True
        round_doc["player2_gave_up"] = True
        in_memory_rounds[round_id] = round_doc
        
        await rounds_collection.update_one(
            {"_id": round_id},
            {"$set": {"player1_gave_up": True, "player2_gave_up": True}}
        )
    
    # Check if both players gave up
    if round_doc.get("player1_gave_up") and round_doc.get("player2_gave_up"):
        # Both gave up - mark as tie and move to next round
        round_doc["winner_id"] = "tie"
        in_memory_rounds[round_id] = round_doc
        
        await rounds_collection.update_one(
            {"_id": round_id},
            {"$set": {"winner_id": "tie"}}
        )
        
        # Update match rounds array
        await matches_collection.update_one(
            {"_id": match_id, "rounds.round_number": round_doc["round_number"]},
            {"$set": {"rounds.$.winner": "tie", "updated_at": datetime.utcnow()}}
        )
        
        return {
            "status": "both_gave_up",
            "round_winner": "tie",
            "player1_score": match["player1_score"],
            "player2_score": match["player2_score"]
        }
    
    return {
        "status": "gave_up",
        "waiting_for_opponent": True
    }


@app.post("/api/game/answer")
async def submit_answer(data: AnswerSubmit, current_user = Depends(get_current_user)):
    match = in_memory_matches.get(data.match_id)

    

    if not match:
        # Try to find in database
        match = await matches_collection.find_one({"_id": data.match_id})
        if match:
            # Load match back into memory
            in_memory_matches[data.match_id] = match
        else:
            raise HTTPException(status_code=404, detail="Match not found")
    
    user_id = str(current_user["_id"])
    if user_id not in (str(match["player1_id"]), str(match["player2_id"])):
        raise HTTPException(status_code=403, detail="Not your match")
    mark_player_seen(match, user_id)

    # Check if match is already completed - prevent processing answers after match ends
    if match.get("status") == "completed":
        raise HTTPException(status_code=400, detail="Match is already completed")
    
    # Get current round from match
    round_id = match.get("current_round_id")
    if not round_id:
        raise HTTPException(status_code=404, detail="No active round")
    
    # Try to get round from memory, otherwise load from database
    round_doc = in_memory_rounds.get(round_id)
    if not round_doc:
        round_doc = await rounds_collection.find_one({"_id": round_id})
        if round_doc:
            in_memory_rounds[round_id] = round_doc
        else:
            raise HTTPException(status_code=404, detail="Round not found")
    
    # Check if round already has a winner (someone already answered correctly)
    if round_doc.get("winner_id"):
        # Round already won, return current state
        return {
            "correct": False,
            "already_won": True,
            "round_winner": str(round_doc["winner_id"]) if round_doc.get("winner_id") else None,
            "player1_score": match["player1_score"],
            "player2_score": match["player2_score"],
            "match_winner": str(match["winner_id"]) if match.get("winner_id") else None,
            "elo_change": 0
        }

    # For bot matches, check if time limit exceeded
    is_bot_match = match.get("match_type") == "random" and match["player2_id"] == "bot-opponent"
    if is_bot_match and "time_limit" in round_doc:
        # Use the synced round start time if available; fall back to created_at
        round_start_raw = match.get("round_start_time") or round_doc.get("started_at") or round_doc.get("created_at")
        round_start = parse_round_start(round_start_raw) or parse_round_start(round_doc.get("created_at")) or utc_now()
        now = utc_now()
        elapsed_time = (now - round_start).total_seconds()
        
        # If time limit exceeded, user loses the round
        if elapsed_time > round_doc["time_limit"]:
            # Bot wins
            in_memory_rounds[round_id]["winner_id"] = match["player2_id"]
            await rounds_collection.update_one(
                {"_id": round_id},
                {"$set": {"winner_id": match["player2_id"]}}
            )
            # Update match scores
            player2_score = match["player2_score"] + 1
            in_memory_matches[data.match_id]["player2_score"] = player2_score
            await matches_collection.update_one(
                {"_id": data.match_id, "rounds.round_number": round_doc["round_number"]},
                {"$set": {
                    "rounds.$.winner": "player2",
                    "player2_score": player2_score,
                    "updated_at": datetime.utcnow()
                }}
            )
            # If match is over, update match status and ELO
            match_winner = None
            elo_change = 0
            if player2_score >= 3:
                match_winner = match["player2_id"]
                # Bot won the match - calculate ELO change for player1 (who lost)
                elo_change = calculate_elo_change(match["player2_elo"], match["player1_elo"])
                
                # Update player1 ELO (loser)
                await users_collection.update_one(
                    {"_id": match["player1_id"]},
                    {"$inc": {"elo": -elo_change, "losses": 1}}
                )
                
                in_memory_matches[data.match_id]["status"] = "completed"
                in_memory_matches[data.match_id]["winner_id"] = match_winner
                in_memory_matches[data.match_id]["elo_change"] = elo_change
                await matches_collection.update_one(
                    {"_id": data.match_id},
                    {"$set": {"status": "completed", "winner_id": match_winner, "elo_change": elo_change, "updated_at": datetime.utcnow()}}
                )
            response_data = {
                "correct": False,
                "already_won": True,
                "round_winner": str(match["player2_id"]),
                "player1_score": match["player1_score"],
                "player2_score": player2_score,
                "match_winner": str(match_winner) if match_winner else None,
                "elo_change": elo_change,
                "message": "Time limit exceeded"
            }
            print(f"[DEBUG TIMEOUT] Bot wins round. Scores: {match['player1_score']}-{player2_score}, Match winner: {match_winner}, ELO change: {elo_change}")
            return response_data
    
    # Check answer - handle both derivative strings and numeric answers
    expected_answer = round_doc["answer"]
    
    if isinstance(expected_answer, str):
        # Use SymPy to check for mathematical equivalence
        def preprocess(expr):
            s = normalize_answer_text(expr)
            s = re.sub(r'√([a-zA-Z0-9_]+)', r'sqrt(\1)', s)
            s = s.replace('ln(', 'log(')
            return s
        user_expr = preprocess(data.answer)
        correct_expr = preprocess(expected_answer)
        try:
            from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application, function_exponentiation
            from sympy import trigsimp, expand, expand_trig, expand_log, logcombine, Pow, Function
            x = Symbol('x')
            # Reject inputs that could hang sympy evaluation (e.g. 9**9**9 power
            # towers) before any parsing; the except below grades them incorrect.
            if _answer_looks_unsafe(user_expr):
                raise ValueError("answer rejected by grading safety guard")
            transformations = (standard_transformations + (implicit_multiplication_application, function_exponentiation))
            # Parse unevaluated first so power towers are rejected before evaluation.
            if _parsed_answer_unsafe(parse_expr(user_expr, transformations=transformations, evaluate=False)):
                raise ValueError("answer rejected by grading safety guard")
            user_sym = parse_expr(user_expr, transformations=transformations, evaluate=True)
            correct_sym = parse_expr(correct_expr, transformations=transformations, evaluate=True)
            print(f"[DEBUG] user_expr: {user_expr}")
            print(f"[DEBUG] correct_expr: {correct_expr}")
            print(f"[DEBUG] user_sym: {user_sym}")
            print(f"[DEBUG] correct_sym: {correct_sym}")
            # Try direct simplify
            correct = simplify(user_sym - correct_sym) == 0
            print(f"[DEBUG] simplify: {correct}")
            if not correct:
                # Try expand
                correct = simplify(expand(user_sym) - expand(correct_sym)) == 0
                print(f"[DEBUG] expand: {correct}")
            if not correct:
                # Try trigsimp
                correct = simplify(trigsimp(user_sym) - trigsimp(correct_sym)) == 0
                print(f"[DEBUG] trigsimp: {correct}")
            if not correct:
                # Try logcombine
                correct = simplify(logcombine(user_sym, force=True) - logcombine(correct_sym, force=True)) == 0
                print(f"[DEBUG] logcombine: {correct}")
            if not correct:
                # Try expand_log and expand_trig
                user_expanded = expand_log(expand_trig(user_sym))
                correct_expanded = expand_log(expand_trig(correct_sym))
                correct = simplify(user_expanded - correct_expanded) == 0
                print(f"[DEBUG] expand_log+expand_trig: {correct}")
            if not correct:
                # Try SymPy's equals method
                try:
                    correct = user_sym.equals(correct_sym)
                    print(f"[DEBUG] equals: {correct}")
                except Exception:
                    pass
            if not correct:
                # Try reciprocal root equivalence: (1/2)*x**(-1/2) == 1/(2*sqrt(x))
                try:
                    # Only attempt if user_sym or correct_sym contains Pow with negative exponent
                    def try_root_equiv(expr1, expr2):
                        # (1/2)*x**(-1/2) <-> 1/(2*sqrt(x))
                        from sympy import sqrt
                        expr1_alt = expr1.replace(lambda e: isinstance(e, Pow) and e.exp == -1/2, lambda e: 1/sqrt(e.base))
                        expr2_alt = expr2.replace(lambda e: isinstance(e, Pow) and e.exp == -1/2, lambda e: 1/sqrt(e.base))
                        return simplify(expr1 - expr2_alt) == 0 or simplify(expr1_alt - expr2) == 0 or simplify(expr1_alt - expr2_alt) == 0
                    correct = try_root_equiv(user_sym, correct_sym)
                    print(f"[DEBUG] root equivalence: {correct}")
                except Exception:
                    pass
            if not correct:
                # Fallback: if both are the same function of x (e.g., cos(x)), accept
                try:
                    if isinstance(user_sym, Function) and isinstance(correct_sym, Function):
                        correct = user_sym.func == correct_sym.func and user_sym.args == correct_sym.args
                        print(f"[DEBUG] function fallback: {correct}")
                except Exception:
                    pass
            if not correct:
                # Numeric fallback: test at random points (for expressions in x only).
                # Skipped for power-heavy trees whose substitution could hang.
                try:
                    from sympy import N
                    if not (_expression_too_complex(user_sym) or _expression_too_complex(correct_sym)):
                        for _ in range(5):
                            val = random.uniform(1, 10)
                            uval = N(user_sym.subs(x, val))
                            cval = N(correct_sym.subs(x, val))
                            if abs(uval - cval) > 1e-6:
                                break
                        else:
                            correct = True
                    print(f"[DEBUG] numeric fallback: {correct}")
                except Exception:
                    pass
        except (SympifyError, Exception) as e:
            print(f"SymPy error: {e}")
            correct = False
    else:
        # This is a numeric question - compare with tolerance
        try:
            correct = abs(float(data.answer) - float(expected_answer)) < 0.1
        except (ValueError, TypeError):
            correct = False
    
    # Determine if this is against bot
    is_player1 = str(current_user["_id"]) == str(match["player1_id"])
    is_bot_match = match.get("match_type") == "random" and match.get("player2_id") == "bot-opponent"
    
    # If incorrect, allow retry - don't end the round
    if not correct:
        # Store the wrong answer but don't end round
        update_field = "player1_answer" if is_player1 else "player2_answer"
        in_memory_rounds[round_id][update_field] = data.answer
        await rounds_collection.update_one(
            {"_id": round_id},
            {"$set": {update_field: data.answer}}
        )
        
        # Also update match rounds array with the wrong answer
        await matches_collection.update_one(
            {"_id": data.match_id, "rounds.round_number": round_doc["round_number"]},
            {"$set": {
                f"rounds.$.{update_field}": data.answer,
                "updated_at": datetime.utcnow()
            }}
        )
        
        return {
            "correct": False,
            "round_winner": None,
            "player1_score": match["player1_score"],
            "player2_score": match["player2_score"],
            "match_winner": None,
            "elo_change": 0
        }
    
    # Answer is correct! Determine round winner
    round_winner = None
    
    if is_bot_match:
        # Simulate bot answer with calibrated difficulty
        import time
        difficulty_factor = round_doc["difficulty"] * 0.25
        bot_correct = random.random() < (1 - difficulty_factor)

        # Bot response time depends on user ELO: higher ELO = faster bot
        # 600 ELO: 8-22s, 1600 ELO: 3-12s, 2000 ELO: 1.5-6s, interpolate piecewise
        user_elo = match["player1_elo"] if is_player1 else match["player2_elo"]
        if user_elo <= 1600:
            min_elo, max_elo = 600, 1600
            min_bot_min, min_bot_max = 8, 22
            max_bot_min, max_bot_max = 3, 12
            capped_elo = max(min_elo, min(max_elo, user_elo))
            t = (capped_elo - min_elo) / (max_elo - min_elo)
            bot_min = min_bot_min + (max_bot_min - min_bot_min) * t
            bot_max = min_bot_max + (max_bot_max - min_bot_max) * t
        else:
            # 1600 to 2000: 3-12s to 1.5-6s
            min_elo, max_elo = 1600, 2000
            min_bot_min, min_bot_max = 3, 12
            max_bot_min, max_bot_max = 1.5, 6
            capped_elo = min(max_elo, max(min_elo, user_elo))
            t = (capped_elo - min_elo) / (max_elo - min_elo)
            bot_min = min_bot_min + (max_bot_min - min_bot_min) * t
            bot_max = min_bot_max + (max_bot_max - min_bot_max) * t
        bot_time = random.uniform(bot_min, bot_max)

        # For realism, store the time user took to answer (if not already present)
        now = utc_now()
        if is_player1:
            user_time_field = "player1_answer_time"
        else:
            user_time_field = "player2_answer_time"
        if user_time_field not in round_doc:
            round_doc[user_time_field] = now
            in_memory_rounds[round_id][user_time_field] = now
            await rounds_collection.update_one(
                {"_id": round_id},
                {"$set": {user_time_field: now}}
            )

        # Use synchronized round start time for fair bot timing
        round_start_raw = match.get("round_start_time") or round_doc.get("started_at") or round_doc.get("created_at")
        round_start = parse_round_start(round_start_raw) or now
        user_time = max(0.0, (now - round_start).total_seconds())

        if not bot_correct:
            round_winner = current_user["_id"]
        else:
            if user_time < bot_time:
                round_winner = current_user["_id"]
            else:
                round_winner = match["player2_id"]
    else:
        # Friend match - first to answer correctly wins
        round_winner = current_user["_id"]
    
    # Update round with winner
    in_memory_rounds[round_id]["winner_id"] = round_winner
    update_field = "player1_answer" if is_player1 else "player2_answer"
    in_memory_rounds[round_id][update_field] = data.answer
    
    await rounds_collection.update_one(
        {"_id": round_id},
        {"$set": {
            "winner_id": round_winner,
            update_field: data.answer
        }}
    )
    
    # Update match scores
    player1_score = match["player1_score"]
    player2_score = match["player2_score"]
    
    if str(round_winner) == str(match["player1_id"]):
        player1_score += 1
    elif str(round_winner) == str(match["player2_id"]):
        player2_score += 1
    
    in_memory_matches[data.match_id]["player1_score"] = player1_score
    in_memory_matches[data.match_id]["player2_score"] = player2_score
    
    # Update match rounds array with winner and answers
    winner_name = "player1" if str(round_winner) == str(match["player1_id"]) else "player2" if str(round_winner) == str(match["player2_id"]) else "tie"
    await matches_collection.update_one(
        {"_id": data.match_id, "rounds.round_number": round_doc["round_number"]},
        {"$set": {
            "rounds.$.winner": winner_name,
            f"rounds.$.{update_field}": data.answer,
            "player1_score": player1_score,
            "player2_score": player2_score,
            "updated_at": datetime.utcnow()
        }}
    )
    
    # Check if match is over
    match_winner = None
    elo_change = 0
    if player1_score >= 3:
        match_winner = match["player1_id"]
    elif player2_score >= 3:
        match_winner = match["player2_id"]
    
    if match_winner:
        # Calculate ELO for ranked matches (human vs human) and bot matches
        if match.get("match_type") in ["random", "ranked"]:
            elo_change = calculate_elo_change(
                match["player1_elo"] if match_winner == match["player1_id"] else match["player2_elo"],
                match["player2_elo"] if match_winner == match["player1_id"] else match["player1_elo"]
            )
            # Update both winner and loser ELOs
            winner_id = match["player1_id"] if match_winner == match["player1_id"] else match["player2_id"]
            loser_id = match["player2_id"] if match_winner == match["player1_id"] else match["player1_id"]
            # Winner: +elo_change, +1 win
            await users_collection.update_one(
                {"_id": winner_id},
                {"$inc": {"elo": elo_change, "wins": 1}}
            )
            # Loser: -elo_change, +1 loss (applies to both human and bot matches)
            await users_collection.update_one(
                {"_id": loser_id},
                {"$inc": {"elo": -elo_change, "losses": 1}}
            )
        
        # Update match
        in_memory_matches[data.match_id]["status"] = "completed"
        in_memory_matches[data.match_id]["winner_id"] = match_winner
        in_memory_matches[data.match_id]["elo_change"] = elo_change
        in_memory_matches[data.match_id]["updated_at"] = datetime.utcnow()
        
        # Update match in database
        await matches_collection.update_one(
            {"_id": data.match_id},
            {"$set": {
                "status": "completed",
                "winner_id": match_winner,
                "elo_change": elo_change,
                "updated_at": datetime.utcnow()
            }}
        )
    
    return {
        "correct": correct,
        "player1_score": player1_score,
        "player2_score": player2_score,
        "round_winner": str(round_winner) if round_winner else None,
        "match_winner": str(match_winner) if match_winner else None,
        "elo_change": elo_change if match_winner else 0
    }


@app.get("/api/game/status/{match_id}")
async def get_game_status(match_id: str, current_user = Depends(get_current_user)):
    """Get current game status for polling"""
    match = in_memory_matches.get(match_id)
    if not match:
        # Try to find in database
        match = await matches_collection.find_one({"_id": match_id})
        if not match:
            raise HTTPException(status_code=404, detail="Match not found")
        # Cache so presence tracking below survives across polls
        in_memory_matches[match_id] = match
    
    user_id = str(current_user["_id"])
    if user_id not in (str(match["player1_id"]), str(match["player2_id"])):
        raise HTTPException(status_code=403, detail="Not your match")

    # Presence: each poll is a heartbeat; the opponent is "connected" while
    # their last heartbeat is recent enough.
    mark_player_seen(match, user_id)
    opponent_id = match["player2_id"] if user_id == str(match["player1_id"]) else match["player1_id"]
    opponent_connected = is_player_connected(match, opponent_id)

    # Get current round info if exists
    current_round_id = match.get("current_round_id")
    round_winner = None
    player1_gave_up = False
    player2_gave_up = False
    
    if current_round_id and current_round_id in in_memory_rounds:
        round_doc = in_memory_rounds[current_round_id]
        round_winner = str(round_doc.get("winner_id")) if round_doc.get("winner_id") else None
        player1_gave_up = round_doc.get("player1_gave_up", False)
        player2_gave_up = round_doc.get("player2_gave_up", False)
    
    # Get player usernames
    player1 = await users_collection.find_one({"_id": match["player1_id"]})
    
    # Handle bot opponent
    if match["player2_id"] == "bot-opponent":
        player2_name = "AI Opponent"
    else:
        player2 = await users_collection.find_one({"_id": match["player2_id"]})
        player2_name = player2.get("username", "Player 2") if player2 else "Player 2"
    
    return {
        "match_id": match_id,
        "player1_id": str(match["player1_id"]),
        "player2_id": str(match["player2_id"]),
        "player1_name": player1.get("username", "Player 1") if player1 else "Player 1",
        "player2_name": player2_name,
        "player1_score": match["player1_score"],
        "player2_score": match["player2_score"],
        "status": match["status"],
        "winner_id": str(match["winner_id"]) if match.get("winner_id") else None,
        "elo_change": match.get("elo_change", 0),
        "round_winner": round_winner,
        "round_start_time": match.get("round_start_time"),
        "player1_gave_up": player1_gave_up,
        "player2_gave_up": player2_gave_up,
        "opponent_connected": opponent_connected
    }


@app.get("/api/leaderboard")
async def get_leaderboard():
    # Get all non-bot users from database
    cursor = users_collection.find(
        {"email": {"$ne": "bot@derivative-duel.com"}}
    ).sort("elo", -1).limit(25)
    
    users = await cursor.to_list(length=25)
    
    return [
        {
            "id": str(u["_id"]),
            "username": u.get("username") or u.get("name") or u.get("email", "Anonymous"),
            "elo": u.get("elo", 1000),
            "wins": u.get("wins", 0),
            "losses": u.get("losses", 0),
            "time_trial_best": u.get("time_trial_best")
        }
        for u in users
    ]


@app.get("/matches/all")
async def get_all_matches(current_user = Depends(get_current_user)):
    """Get all matches from database for debugging"""
    matches = await matches_collection.find().sort("created_at", -1).limit(50).to_list(50)
    
    result = []
    for match in matches:
        player1 = await users_collection.find_one({"_id": match["player1_id"]})
        if match["player2_id"] == "bot-opponent":
            player2_name = "AI Opponent"
        else:
            player2 = await users_collection.find_one({"_id": match["player2_id"]})
            player2_name = player2.get("username", "Player 2") if player2 else "Player 2"
        
        result.append({
            "match_id": match["_id"],
            "player1": player1.get("username", "Player 1") if player1 else "Player 1",
            "player2": player2_name,
            "score": f"{match['player1_score']}-{match['player2_score']}",
            "status": match["status"],
            "rounds_count": len(match.get("rounds", [])),
            "created_at": match["created_at"].isoformat() if isinstance(match["created_at"], datetime) else match["created_at"]
        })
    
    return result

@app.get("/match/{match_id}/details")
async def get_match_details(match_id: str, current_user = Depends(get_current_user)):
    """Get detailed match information for verification"""
    # Try to find match in database first, then memory
    match = await matches_collection.find_one({"_id": match_id})
    if not match:
        match = in_memory_matches.get(match_id)
    
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    
    # Get player information
    player1 = await users_collection.find_one({"_id": match["player1_id"]})
    if match["player2_id"] == "bot-opponent":
        player2_name = "AI Opponent"
    else:
        player2 = await users_collection.find_one({"_id": match["player2_id"]})
        player2_name = player2.get("username", "Player 2") if player2 else "Player 2"
    
    return {
        "match_id": match["_id"],
        "match_code": match["match_code"],
        "match_type": match.get("match_type", "unknown"),
        "player1": {
            "id": str(match["player1_id"]),
            "username": player1.get("username", "Player 1") if player1 else "Player 1",
            "elo": match["player1_elo"]
        },
        "player2": {
            "id": str(match["player2_id"]),
            "username": player2_name,
            "elo": match["player2_elo"]
        },
        "score": f"{match['player1_score']}-{match['player2_score']}",
        "status": match["status"],
        "winner": str(match["winner_id"]) if match.get("winner_id") else None,
        "elo_change": match.get("elo_change", 0),
        "rounds": match.get("rounds", []),
        "created_at": match["created_at"].isoformat() if isinstance(match["created_at"], datetime) else match["created_at"],
        "updated_at": match.get("updated_at", match["created_at"]).isoformat() if isinstance(match.get("updated_at", match["created_at"]), datetime) else match.get("updated_at", match["created_at"])
    }


# Time Trial Endpoints
@app.post("/api/time-trial/start")
async def start_time_trial(current_user = Depends(get_current_user)):
    """Start a new time trial - generates 5 questions (easy, medium, medium, hard, hard)"""
    # Check if user is logged in (not a guest)
    if current_user["_id"] == "guest-user-id":
        raise HTTPException(status_code=401, detail="You must be logged in to play Time Trial")
    
    user_id = current_user["_id"]
    
    # Generate questions with specific difficulties
    questions = []
    difficulties = [1, 2, 2, 3, 3]  # easy, medium, medium, hard, hard
    
    for difficulty in difficulties:
        # Generate question at specific difficulty
        if difficulty == 1:
            expr, deriv = random_poly_easy()
            special_type = None
        elif difficulty == 2:
            expr, deriv = random_poly_medium()
            special_type = None
        else:
            result = random_poly_hard()
            expr, deriv = result[0], result[1]
            special_type = result[2] if len(result) == 3 else None
        
        evaluate_at = random.randint(1, 5)
        
        # Evaluate the derivative at the point
        import math
        x = evaluate_at
        
        # Handle special function types
        if special_type == "is_exp":
            if "·x" in deriv and "·e" in deriv:
                coef = int(deriv.split("·")[0])
                if "·x" in expr:
                    inner_coef = int(expr.split("(")[1].split("·")[0])
                    answer = coef * math.exp(inner_coef * x)
                else:
                    answer = math.exp(x)
            else:
                answer = math.exp(x)
        elif special_type == "is_trig":
            if "cos" in deriv:
                if "·cos" in deriv:
                    coef = int(deriv.split("·")[0])
                    if "·x" in expr:
                        inner_coef = int(expr.split("(")[1].split("·")[0])
                        answer = coef * math.cos(inner_coef * x)
                    else:
                        answer = math.cos(x)
                else:
                    answer = math.cos(x)
            else:  # sin derivative
                coef = int(deriv.split("·")[0].replace("-", ""))
                if "·x" in expr:
                    inner_coef = int(expr.split("(")[1].split("·")[0])
                    answer = -coef * math.sin(inner_coef * x)
                else:
                    answer = -math.sin(x)
        elif special_type == "is_log":
            answer = 1 / x
        else:
            # Polynomial evaluation
            deriv_clean = deriv.replace("·", "*").replace("^", "**").replace("x", str(x))
            deriv_clean = deriv_clean.replace("+ -", "- ")
            answer = eval(deriv_clean)
        
        questions.append({
            "expression": expr,
            "derivative": deriv,
            "evaluate_at": evaluate_at,
            "answer": float(answer),
            "difficulty": difficulty
        })
    
    # Generate unique trial ID
    trial_id = secrets.token_urlsafe(8)
    
    # Store in memory (you could also store in DB)
    time_trials[trial_id] = {
        "user_id": user_id,
        "questions": questions,
        "current_question": 0,
        "start_time": datetime.utcnow(),
        "answers": []
    }
    
    return {
        "trial_id": trial_id,
        "questions": questions
    }


@app.post("/api/time-trial/{trial_id}/submit")
async def submit_time_trial(
    trial_id: str,
    user_answers: dict,
    current_user = Depends(get_current_user)
):
    """Submit answers for time trial and calculate final time"""
    if trial_id not in time_trials:
        raise HTTPException(status_code=404, detail="Time trial not found")
    
    trial = time_trials[trial_id]
    
    # Verify user
    if trial["user_id"] != current_user["_id"]:
        raise HTTPException(status_code=403, detail="Not your time trial")
    
    # Calculate time taken
    end_time = datetime.utcnow()
    time_taken = (end_time - trial["start_time"]).total_seconds()
    
    # Check answers
    correct_count = 0
    for i, question in enumerate(trial["questions"]):
        user_answer_str = user_answers.get(f"answer_{i}", "")
        
        if question.get("ask_for_derivative_only"):
            # For derivative questions, compare strings (normalize whitespace)
            correct_answer = question["answer"].replace(" ", "").lower()
            user_answer = str(user_answer_str).replace(" ", "").lower()
            if user_answer == correct_answer:
                correct_count += 1
        else:
            # For numerical answers, compare with tolerance
            try:
                user_answer = float(user_answer_str)
                if abs(user_answer - question["answer"]) < 0.01:
                    correct_count += 1
            except (ValueError, TypeError):
                pass  # Invalid number, count as wrong
    
    # Only save time if all answers are correct
    if correct_count == 5:
        # Update best time if this is better
        current_best = current_user.get("time_trial_best")
        if current_best is None or time_taken < current_best:
            await users_collection.update_one(
                {"_id": current_user["_id"]},
                {"$set": {"time_trial_best": time_taken}}
            )
            is_new_record = True
        else:
            is_new_record = False
    else:
        is_new_record = False
    
    # Clean up
    del time_trials[trial_id]
    
    return {
        "time_taken": time_taken,
        "correct_count": correct_count,
        "total_questions": 5,
        "is_new_record": is_new_record,
        "previous_best": current_user.get("time_trial_best")
    }


# ============================================
# Daily Challenge Endpoints
# ============================================

@app.get("/api/daily-challenge/today")
async def get_daily_challenge(current_user: dict = Depends(get_current_user)):
    """Get today's daily challenge"""
    today = datetime.now(timezone.utc).date().isoformat()
    
    print(f"[DEBUG] Fetching challenge for date: {today}")
    print(f"[DEBUG] Date type: {type(today)}")
    
    # Try to load from MongoDB first
    try:
        # First, let's see what documents exist
        all_docs = await daily_challenges_collection.find().to_list(3)
        print(f"[DEBUG] First 3 documents in DB: {all_docs}")
        
        # Try different query formats
        query1 = {"date": today}
        query2 = {"date": f'"{today}"'}  # With quotes as literal string
        
        print(f"[DEBUG] Trying query 1: {query1}")
        db_challenge = await daily_challenges_collection.find_one(query1)
        print(f"[DEBUG] Query 1 result: {db_challenge}")
        
        if not db_challenge:
            print(f"[DEBUG] Trying query 2 with quoted date: {query2}")
            db_challenge = await daily_challenges_collection.find_one(query2)
            print(f"[DEBUG] Query 2 result: {db_challenge}")
        
        if db_challenge:
            print(f"[DEBUG] Found challenge in MongoDB: {db_challenge.get('expression')}")
            # Store in memory for faster access
            daily_challenges_storage[today] = {
                "date": db_challenge.get("date"),
                "expression": db_challenge.get("expression"),
                "derivative": db_challenge.get("derivative"),
                "answer": db_challenge.get("answer"),
                "difficulty": db_challenge.get("difficulty", 1700)
            }
        else:
            print(f"[DEBUG] Challenge not found in MongoDB with either query format")
    except Exception as e:
        print(f"[DEBUG] Error loading from MongoDB: {e}")
        import traceback
        traceback.print_exc()
    
    # Check if daily challenge exists for today
    if today not in daily_challenges_storage:
        print(f"[DEBUG] Challenge not in memory, generating new one...")
        # Generate a new challenge with deterministic ELO based on date
        # Use date hash to ensure same challenge for everyone on same day
        date_hash = sum(ord(c) for c in today)
        elo_options = [1300, 1400, 1500, 1600]
        elo = elo_options[date_hash % len(elo_options)]
        
        # Set random seed based on date for consistent challenge generation
        # This ensures everyone gets the same question on the same day
        old_state = random.getstate()
        random.seed(today)
        question = generate_question(elo)
        random.setstate(old_state)  # Restore previous random state
        
        print(f"[DEBUG] Generated question: {question['expression']}")
        
        daily_challenges_storage[today] = {
            "date": today,
            "expression": question["expression"],
            "derivative": question["derivative"],
            "answer": question["answer"],
            "difficulty": question.get("difficulty", 2)
        }
    
    daily_challenge = daily_challenges_storage[today]
    print(f"[DEBUG] Challenge found: {daily_challenge['expression']}")
    
    # Check if user has completed today's challenge
    completion_key = (str(current_user["_id"]), today)
    user_completion = daily_completions_storage.get(completion_key)
    
    return {
        "date": today,
        "expression": daily_challenge["expression"],
        "user_completed": user_completion is not None,
        "user_time": user_completion.get("time") if user_completion else None,
        "user_rank": user_completion.get("rank") if user_completion else None
    }


@app.get("/api/daily-challenge/leaderboard")
async def get_daily_leaderboard(current_user: dict = Depends(get_current_user)):
    """Get top 10 times for today's challenge"""
    today = datetime.now(timezone.utc).date().isoformat()
    
    print(f"[DEBUG] Getting leaderboard for {today}")
    print(f"[DEBUG] Completions storage: {daily_completions_storage}")
    
    # Try to load from MongoDB first
    try:
        completions_docs = await daily_completions_collection.find({"date": today, "correct": True}).to_list(None)
        print(f"[DEBUG] Found {len(completions_docs)} MongoDB completions for {today}")
        
        # Convert MongoDB documents to completion format
        completions = []
        for doc in completions_docs:
            completion = {
                "user_id": str(doc.get("user_id")),
                "time": doc.get("time"),
                "correct": doc.get("correct", True),
                "rank": doc.get("rank")
            }
            completions.append(completion)
            # Also populate in-memory for consistency
            daily_completions_storage[(str(doc.get("user_id")), today)] = {
                "time": doc.get("time"),
                "correct": True,
                "rank": doc.get("rank")
            }
    except Exception as e:
        print(f"[DEBUG] Error loading from MongoDB: {e}")
        # Fall back to in-memory
        completions = [
            {"user_id": user_id, **data}
            for (user_id, date), data in daily_completions_storage.items()
            if date == today and data.get("correct", False)
        ]
    
    # Get all completions for today, sorted by time
    if not completions:
        completions = [
            {"user_id": user_id, **data}
            for (user_id, date), data in daily_completions_storage.items()
            if date == today and data.get("correct", False)
        ]
    
    # Guard against legacy rows that stored a missing time; treat them as slowest
    # rather than crashing the sort on a None value.
    completions = [c for c in completions if c.get("time") is not None]
    completions.sort(key=lambda x: x["time"])
    completions = completions[:10]
    
    print(f"[DEBUG] Top 10 completions: {completions}")
    
    # Populate with usernames from MongoDB
    top_times = []
    for completion in completions:
        user_id_str = str(completion["user_id"])
        
        # Try to get user from MongoDB
        user = None
        try:
            user_doc = await users_collection.find_one({"_id": ObjectId(user_id_str) if len(user_id_str) == 24 else user_id_str})
            if user_doc:
                user = {"username": user_doc.get("username", "Anonymous")}
        except Exception as e:
            print(f"[DEBUG] Error loading user {user_id_str}: {e}")
        
        # Fall back to in-memory
        if not user:
            user = in_memory_users.get(completion["user_id"]) or in_memory_users.get(user_id_str)
        
        username = user.get("username", "Anonymous") if user else "Anonymous"
        top_times.append({
            "username": username,
            "time": completion["time"]
        })
    
    print(f"[DEBUG] Returning leaderboard: {top_times}")
    return top_times


@app.get("/api/daily-challenge/history")
async def get_daily_history(current_user: dict = Depends(get_current_user)):
    """Get past 365 days (12 months) of challenges and user completions"""
    year_ago = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
    
    # Get all challenges from the past 365 days
    challenges = [
        data for date, data in daily_challenges_storage.items()
        if date >= year_ago
    ]
    challenges.sort(key=lambda x: x["date"], reverse=True)
    
    # Get user completions
    user_id_str = str(current_user["_id"])
    user_completions = {
        date: {
            "time": data["time"],
            "rank": data.get("rank", 0),
            "correct": data.get("correct", False)
        }
        for (uid, date), data in daily_completions_storage.items()
        if uid == user_id_str and date >= year_ago
    }
    
    # For each challenge, find the winner (fastest time)
    challenges_with_winners = []
    for challenge in challenges:
        date = challenge["date"]
        
        # Find fastest completion for this date
        date_completions = [
            {"user_id": uid, **data}
            for (uid, d), data in daily_completions_storage.items()
            if d == date and data.get("correct", False)
        ]
        
        winner_username = None
        best_time = None
        date_completions = [c for c in date_completions if c.get("time") is not None]
        if date_completions:
            date_completions.sort(key=lambda x: x["time"])
            winner = date_completions[0]
            winner_user = in_memory_users.get(winner["user_id"])
            winner_username = winner_user.get("username", "Anonymous") if winner_user else "Anonymous"
            best_time = winner["time"]
        
        challenges_with_winners.append({
            "date": challenge["date"],
            "expression": challenge["expression"],
            "winner_username": winner_username,
            "best_time": best_time
        })
    
    return {
        "challenges": challenges_with_winners,
        "user_completions": user_completions
    }


@app.post("/api/daily-challenge/submit")
async def submit_daily_challenge(
    request: Request,
    current_user: dict = Depends(get_current_user)
):
    """Submit answer for today's daily challenge"""
    data = await request.json()
    user_answer = data.get("answer")

    # Resolve the time value, accepting either "time" or "time_taken".
    # Use an explicit None check so a legitimate 0 is not discarded.
    raw_time = data.get("time")
    if raw_time is None:
        raw_time = data.get("time_taken")

    # A valid, non-negative number is required. Storing None here would later
    # crash the rank comparison and leaderboard sort for every other player.
    try:
        time_taken = float(raw_time)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A numeric time is required")
    if time_taken < 0:
        raise HTTPException(status_code=400, detail="Time cannot be negative")

    print(f"[DEBUG] Submit: user={current_user['_id']}, time={time_taken}, answer={user_answer}")
    
    # Always use today's date (server-side)
    today = datetime.now(timezone.utc).date().isoformat()
    
    # Get the challenge
    if today not in daily_challenges_storage:
        raise HTTPException(status_code=404, detail="Challenge not found")
    
    challenge = daily_challenges_storage[today]
    
    # Check if user already completed today correctly
    completion_key = (str(current_user["_id"]), today)
    existing = daily_completions_storage.get(completion_key)
    if existing and existing.get("correct"):
        raise HTTPException(status_code=400, detail="You've already completed today's challenge")
    
    # Check answer
    is_correct = check_math_equivalence(challenge["answer"], user_answer)
    
    print(f"[DEBUG] Answer correct: {is_correct}")
    
    # Calculate rank (how many people were faster)
    rank = None
    if is_correct:
        faster_count = sum(
            1 for (uid, d), data in daily_completions_storage.items()
            if d == today and data.get("correct", False) and data["time"] < time_taken
        )
        rank = faster_count + 1
    
    # Save completion only if correct
    if is_correct:
        # Save to in-memory
        daily_completions_storage[completion_key] = {
            "time": time_taken,
            "answer": user_answer,
            "correct": is_correct,
            "rank": rank
        }
        
        # Save to MongoDB
        try:
            completion_doc = {
                "user_id": str(current_user["_id"]),
                "username": current_user.get("username", "Anonymous"),
                "date": today,
                "time": time_taken,
                "answer": user_answer,
                "correct": True,
                "rank": rank,
                "createdAt": datetime.now(timezone.utc)
            }
            
            # Upsert - update if exists, insert if not
            result = await daily_completions_collection.update_one(
                {"user_id": str(current_user["_id"]), "date": today},
                {"$set": completion_doc},
                upsert=True
            )
            print(f"[DEBUG] Saved to MongoDB: upserted={result.upserted_id is not None or result.modified_count > 0}")
        except Exception as e:
            print(f"[DEBUG] Error saving to MongoDB: {e}")
            import traceback
            traceback.print_exc()
    
    # Check if this is the fastest time
    is_fastest = False
    if is_correct and rank == 1:
        is_fastest = True
    
    return {
        "correct": is_correct,
        "correct_answer": challenge["derivative"],
        "time_taken": time_taken,
        "rank": rank,
        "is_fastest": is_fastest
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
