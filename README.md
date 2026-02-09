# 🧮 Derivative Duel - Backend API

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104.1-009688.svg)](https://fastapi.tiangolo.com)
[![MongoDB](https://img.shields.io/badge/MongoDB-4.6+-green.svg)](https://www.mongodb.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Live Demo:** [https://mathbattle.xyz](https://mathbattle.xyz)

A real-time competitive mathematics game backend where players battle each other by solving derivative problems. Features ELO-based matchmaking, adaptive difficulty, daily challenges, and bot opponents.

> **Demo Status:** The backend is deployed and fully functional in "Guest Mode" (no OAuth required). However, the frontend integration for guest sessions is currently in progress. 
> 
> You can verify the API directly via the [Swagger Documentation](https://mathbattlebackend.onrender.com/docs).

## 🎯 Features

### Core Gameplay
- **Real-time PvP Battles** - Compete against other players in derivative solving matches
- **Bot Matchmaking** - Practice against AI opponents with adaptive difficulty
- **Friend Matches** - Create private games with shareable match codes
- **Daily Challenges** - Pre-generated daily derivative problems with global leaderboards
- **Time Trials** - Test your speed with timed derivative challenges

### Player Progression
- **ELO Rating System** - Dynamic K-factor based ranking (500-2000+ range)
- **Adaptive Difficulty** - Questions scale with player ELO (easy/medium/hard)
- **Win/Loss Tracking** - Complete player statistics and match history
- **Username System** - Set unique usernames for friend matches

### Technical Features
- **Google OAuth 2.0** - Secure authentication with Google Sign-In
- **JWT Authentication** - Token-based session management
- **MongoDB Persistence** - Scalable NoSQL database for user data and matches
- **CORS Enabled** - Ready for frontend integration
- **RESTful API** - Clean, documented endpoints

## 🏗️ Architecture

```
FastAPI Backend (Python 3.11)
├── Authentication Layer (Google OAuth + JWT)
├── Game Logic Engine
│   ├── Question Generator (Polynomial, Trig, Exponential, Logarithmic)
│   ├── Math Equivalence Checker (SymPy-based)
│   └── ELO Calculator (Dynamic K-factor)
├── Matchmaking System
│   ├── Random Queue (ELO-weighted)
│   ├── Bot Matches (Difficulty-adjusted)
│   └── Friend Matches (Code-based)
└── MongoDB Data Layer
    ├── Users Collection
    ├── Matches Collection
    ├── Rounds Collection
    ├── Daily Challenges Collection
    └── Daily Completions Collection
```

## 🚀 Tech Stack

- **Framework:** FastAPI 0.104.1
- **Runtime:** Python 3.11+
- **Database:** MongoDB 4.6+ (Motor async driver)
- **Authentication:** Google OAuth 2.0, JWT (python-jose)
- **Password Hashing:** Bcrypt (passlib)
- **Math Engine:** SymPy
- **Deployment:** Render (Production)

## 📋 Prerequisites

- Python 3.11 or higher
- MongoDB 4.6+ (local or Atlas)
- Google OAuth 2.0 credentials
- pip package manager

## ⚙️ Installation & Setup

### 1. Clone the Repository
```bash
git clone https://github.com/Skriptiensolmija/MathbattleBack.git
cd MathbattleBack
```

### 2. Create Virtual Environment
```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Environment Configuration
Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Required environment variables:
- `MONGODB_URL` - Your MongoDB connection string
- `DATABASE_NAME` - Database name (default: `derivative_duel`)
- `SECRET_KEY` - JWT secret (generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
- `GOOGLE_CLIENT_ID` - Google OAuth Client ID
- `OAUTH` - Google OAuth Client Secret
- `ACCESS_TOKEN_EXPIRE_MINUTES` - JWT expiration (default: 10080 = 7 days)
- `CORS_ORIGINS` - Comma-separated allowed origins

### 5. Run the Server
```bash
# Development
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Production
uvicorn main:app --host 0.0.0.0 --port 10000
```

The API will be available at `http://localhost:8000`

## 📚 API Documentation

Once running, visit:
- **Interactive Docs (Swagger UI):** `http://localhost:8000/docs`
- **Alternative Docs (ReDoc):** `http://localhost:8000/redoc`

### Key Endpoints

#### Authentication
- `POST /auth/register` - Email/password registration
- `POST /auth/login` - Email/password login
- `POST /auth/google` - Google OAuth authentication

#### User Management
- `GET /api/user/profile` - Get current user profile
- `POST /api/user/set-username` - Set unique username
- `GET /api/users/search?username=<query>` - Search users by username

#### Game Modes
- `POST /api/game/start` - Start random matchmaking
- `POST /api/game/cancel` - Cancel matchmaking queue
- `POST /api/game/bot-match` - Start bot match
- `POST /api/game/friend/create` - Create friend match with code
- `POST /api/game/friend/join` - Join friend match by code

#### Gameplay
- `POST /api/game/answer` - Submit answer to current question
- `GET /api/game/match/{match_id}` - Get match details
- `POST /api/game/time-trial/start` - Start time trial mode

#### Daily Challenges
- `GET /api/daily-challenge` - Get today's challenge
- `POST /api/daily-challenge/submit` - Submit daily challenge answer
- `GET /api/daily-challenge/leaderboard` - Get daily leaderboard

#### Leaderboard
- `GET /api/leaderboard` - Global ELO leaderboard (top 100)

## 🧪 Testing

```bash
# Run answer checking tests
python test_answer_checking.py

# Test daily challenge seeding
python seed_daily_challenges.py
```

## 🐳 Docker Deployment

```dockerfile
# Already includes Dockerfile
docker build -t derivative-duel-backend .
docker run -p 10000:10000 --env-file .env derivative-duel-backend
```

## 🎮 Game Mechanics

### Question Difficulty Tiers

- **Easy (ELO < 1200):** Simple polynomials (degree 2-3, 3-4 terms)
- **Medium (ELO 1200-1500):** Higher-degree polynomials, basic trig, exponentials, logarithms
- **Hard (ELO 1500+):** Complex derivatives, chain rule, product/quotient rule, nested functions

### ELO System

- **Dynamic K-factor:**
  - ELO < 1200: K=40 (rapid adjustment for beginners)
  - ELO 1200-1800: K=32 (moderate adjustment)
  - ELO 1800+: K=24 (stable for advanced players)
- **Expected Score Formula:** Standard chess ELO formula
- **Minimum Change:** 1 point per game

### Answer Validation

Uses SymPy's symbolic math engine with multiple equivalence checks:
- Direct simplification
- Polynomial expansion
- Trigonometric simplification
- Logarithmic combination
- Symbolic equality

## 🔐 Security

- **Password Hashing:** Bcrypt with salting
- **JWT Tokens:** 7-day expiration, HS256 algorithm
- **OAuth 2.0:** Google ID token verification
- **CORS:** Whitelisted origins only
- **Environment Variables:** All secrets in `.env` (gitignored)

## 📁 Project Structure

```
.
├── main.py                      # Main FastAPI application
├── check_math_equiv.py          # Math equivalence testing utilities
├── seed_daily_challenges.py     # Daily challenge generation script
├── test_answer_checking.py      # Answer validation tests
├── requirements.txt             # Python dependencies
├── runtime.txt                  # Python version
├── Dockerfile                   # Container configuration
├── .env.example                 # Environment template
├── .gitignore                   # Git ignore rules
└── README.md                    # This file
```

## 🚢 Deployment (Render)

### Build Command
```bash
pip install -r requirements.txt
```

### Start Command
```bash
uvicorn main:app --host 0.0.0.0 --port 10000
```

### Environment Variables
Set in Render dashboard:
- `DATABASE_URL` (MongoDB connection string)
- `OAUTH` (Google OAuth secret)
- `GOOGLE_CLIENT_ID`
- `SECRET_KEY`

## 🤝 Contributing

See [DEVELOPMENT.md](DEVELOPMENT.md) for local development setup and coding guidelines.

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🔗 Related Projects

- **Frontend Repository:** [Math Battle Frontend](https://github.com/Skriptiensolmija/mathbattlefront)

## 👨‍💻 Author

**Skriptiensolmija**
- GitHub: [@Skriptiensolmija](https://github.com/Skriptiensolmija)

## 🙏 Acknowledgments

- **FastAPI** - Modern Python web framework
- **SymPy** - Symbolic mathematics library
- **MongoDB** - NoSQL database
- **Google OAuth** - Authentication provider

---

**⚠️ Note:** This is a portfolio project demonstrating full-stack development capabilities, real-time game mechanics, and production-ready API design.
