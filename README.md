# Derivative Duel — Backend

API for [Derivative Duel](https://mathbattle.xyz), a competitive 1v1 game where players race to solve derivative problems.

Frontend: [eliasleinonen/mathbattlefront](https://github.com/eliasleinonen/mathbattlefront) 

## What it does

- Random matchmaking with a bot fallback after a short queue wait
- Friend matches via shareable match codes
- ELO-based difficulty for generated questions
- SymPy-based answer checking (algebraic equivalence, not string match)
- Daily challenges and a global leaderboard
- Guest play (Bearer `guest-<id>`) plus JWT auth paths for registered users

Gameplay is HTTP-polled (no WebSockets). Matchmaking and active rounds live primarily in process memory, with MongoDB used for persistence (users, matches, rounds, daily data).

## Stack

- Python 3.11+ / FastAPI / Uvicorn
- MongoDB (Motor)
- SymPy
- JWT + bcrypt (Google OAuth is currently disabled in demo mode)

## Setup

```bash
git clone https://github.com/eliasleinonen/MathbattleBack.git
cd MathbattleBack

python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env
```

Set at least:

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | MongoDB connection string |
| `SECRET_KEY` | JWT signing secret |

Optional / legacy names in `.env.example` may not all be read by `main.py`. Prefer checking `os.getenv` usage in `main.py` if something does not apply.

Run locally (default in `main.py` is port 8080; frontend often expects that):

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8080
```

API docs: `http://localhost:8080/docs`

## Tests

```bash
pytest -v
```

Tests mock Mongo and exercise friend/PvP flow, guest matchmaking, and a few core helpers. See `tests/`.

## Main API surface

Auth (no `/api` prefix):

- `POST /auth/register`
- `POST /auth/login`

Game / user (under `/api`):

- `POST /api/game/start` — queue for a random match
- `POST /api/game/cancel`
- `POST /api/game/friend/create` / `join`
- `GET /api/game/question`
- `POST /api/game/answer`
- `POST /api/game/give-up`
- `GET /api/game/status/{match_id}`
- `GET /api/user/profile`
- `GET /api/leaderboard`
- `GET /api/daily-challenge/today`
- `POST /api/daily-challenge/submit`

## Project layout

```
main.py                   # FastAPI app and game logic
tests/                    # pytest suite
seed_daily_challenges.py  # seed daily challenges into Mongo
check_math_equiv.py       # standalone math-equivalence helper
Dockerfile
requirements.txt
```

## Deploy

Docker image runs Uvicorn on `$PORT`. Typical hosts: Render or Railway. Set `DATABASE_URL` and `SECRET_KEY` in the host environment. CORS allows `mathbattle.xyz` and common localhost frontend ports.

## License

MIT — see [LICENSE](LICENSE).

## Author

Elias Leinonen  
GitHub: [eliasleinonen](https://github.com/eliasleinonen)
