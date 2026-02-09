# 🛠️ Development Guide

## Local Development Setup

### Prerequisites
- Python 3.11 or higher
- MongoDB 4.6+ (local installation or Docker)
- Google Cloud Platform account (for OAuth credentials)
- Git
- Code editor (VS Code recommended)

### Quick Start

1. **Clone and Setup**
   ```bash
   git clone https://github.com/Skriptiensolmija/mathbattlebackend.git
   cd mathbattlebackend
   python -m venv venv
   
   # Windows
   venv\Scripts\activate
   
   # macOS/Linux  
   source venv/bin/activate
   
   pip install -r requirements.txt
   ```

2. **Database Setup**
   
   **Option A: Local MongoDB**
   ```bash
   # Install MongoDB from https://www.mongodb.com/try/download/community
   # Start MongoDB service
   # Windows: MongoDB runs as a service automatically
   # macOS: brew services start mongodb-community
   # Linux: sudo systemctl start mongod
   ```
   
   **Option B: MongoDB Docker**
   ```bash
   docker run -d -p 27017:27017 --name mongodb mongo:latest
   ```
   
   **Option C: MongoDB Atlas (Cloud)**
   - Create account at https://www.mongodb.com/cloud/atlas
   - Create a free cluster
   - Get connection string
   - Whitelist your IP address

3. **Google OAuth Setup**
   
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project or select existing
   - Enable "Google+ API"
   - Navigate to Credentials → Create Credentials → OAuth 2.0 Client ID
   - Configure OAuth consent screen
   - Add authorized redirect URIs:
     - `http://localhost:3000` (frontend dev)
     - `http://localhost:8000` (backend dev)
     - Your production URLs
   - Save Client ID and Client Secret

4. **Environment Configuration**
   ```bash
   cp .env.example .env
   ```
   
   Edit `.env` with your actual values:
   - Generate `SECRET_KEY`: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   - Add your `MONGODB_URL`
   - Add Google OAuth `GOOGLE_CLIENT_ID` and `OAUTH` secret

5. **Run Development Server**
   ```bash
   uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```
   
   Server runs at: http://localhost:8000
   
   **API Documentation:**
   - Swagger UI: http://localhost:8000/docs
   - ReDoc: http://localhost:8000/redoc

## Development Workflow

### Code Structure

The project currently uses a monolithic `main.py` file (2546 lines). For contributions, maintain consistency:

```python
# Typical flow in main.py:
# 1. Imports
# 2. Configuration
# 3. Database initialization
# 4. Pydantic models
# 5. Helper functions
# 6. Game logic functions
# 7. API routes
```

**Recommended future refactoring:**
```
app/
├── main.py           # FastAPI app and startup
├── config.py         # Configuration
├── models/
│   ├── user.py
│   ├── match.py
│   └── challenge.py
├── routes/
│   ├── auth.py
│   ├── game.py
│   └── leaderboard.py
├── services/
│   ├── question_generator.py
│   ├── elo_calculator.py
│   └── matchmaking.py
└── utils/
    ├── math_checker.py
    └── database.py
```

### Testing

**Run existing tests:**
```bash
# Answer checking tests
python test_answer_checking.py

# Daily challenge seeding
python seed_daily_challenges.py
```

**Adding new tests:**
Create tests following the pattern in `test_answer_checking.py`:
```python
from main import check_math_equivalence

def test_your_feature():
    result = check_math_equivalence("2*x", "x + x")
    assert result == True
```

**Future: pytest setup**
```bash
pip install pytest pytest-asyncio httpx
pytest tests/
```

### API Testing with Swagger

1. Go to http://localhost:8000/docs
2. Click "Authorize" button
3. For endpoints requiring auth:
   - Register via `/auth/register`
   - Copy the `access_token` from response
   - Click "Authorize" and enter: `Bearer <your_token>`
4. Test endpoints directly in browser

### Database Management

**Viewing data:**
```bash
# MongoDB Compass (GUI): https://www.mongodb.com/products/compass
# Or use mongosh CLI:
mongosh
use derivative_duel
db.users.find()
db.matches.find()
db.DailyChallenge.find()
```

**Reset database:**
```javascript
// In mongosh:
use derivative_duel
db.dropDatabase()
```

### Common Development Tasks

**Add a new API endpoint:**
```python
@app.get("/api/your-endpoint")
async def your_function(current_user = Depends(get_current_user)):
    # Your logic here
    return {"message": "success"}
```

**Add a new question type:**
Edit the `random_poly_hard()` or create new functions, then update `generate_question()`.

**Modify ELO calculation:**
Edit `calculate_elo_change()` function.

**Add database collection:**
```python
# Add to database section:
your_collection = db.your_collection_name

# Create Pydantic model:
class YourModel(BaseModel):
    field: str
```

## Debugging

### Enable detailed logging:
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Common issues:

**"Invalid authentication credentials"**
- Check JWT token is valid
- Verify `SECRET_KEY` matches
- Check token hasn't expired

**"Connection refused" to MongoDB**
- Verify MongoDB is running: `mongosh` or check service
- Check `MONGODB_URL` in `.env`
- For Atlas: whitelist your IP

**Google OAuth fails**
- Verify `GOOGLE_CLIENT_ID` matches your GCP project
- Check redirect URIs are configured
- Ensure Google+ API is enabled

**CORS errors**
- Add your frontend URL to `CORS_ORIGINS` in `.env`
- Restart server after .env changes

## Git Workflow

### Commit Message Guidelines

Use descriptive, professional commit messages:

**Good:**
```
feat: Add time trial mode with difficulty scaling
fix: Resolve math equivalence check for logarithms
docs: Update API documentation for matchmaking
refactor: Extract ELO calculation to separate function
```

**Bad:**
```
Update main.py
Fixed stuff
Changes
```

**Format:**
```
<type>: <description>

[optional body]

[optional footer]
```

**Types:**
- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation
- `refactor:` Code restructuring
- `test:` Adding tests
- `chore:` Maintenance

### Branching Strategy

```bash
# Create feature branch
git checkout -b feature/your-feature-name

# Make changes, commit
git add .
git commit -m "feat: Add your feature"

# Push to GitHub
git push origin feature/your-feature-name

# Open pull request on GitHub
```

## Code Style

- **Python:** Follow PEP 8
- **Max line length:** 100 characters
- **Type hints:** Use where possible
- **Docstrings:** Add to all functions
- **Comments:** Explain "why", not "what"

**Example:**
```python
def calculate_elo_change(winner_elo: int, loser_elo: int) -> int:
    """
    Calculate ELO rating change using standard chess formula with dynamic K-factor.
    
    Args:
        winner_elo: Current ELO rating of the winning player
        loser_elo: Current ELO rating of the losing player
        
    Returns:
        int: ELO points to add to winner (subtract from loser)
        
    Note:
        K-factor decreases with higher ELO to stabilize ratings for advanced players.
    """
    # Implementation...
```

## Performance Optimization

### Current bottlenecks:
- In-memory storage alongside MongoDB (legacy code)
- 2546-line main.py (slow to parse)
- Synchronous question generation

### Optimization tips:
- Use MongoDB indexes on frequently queried fields
- Cache daily challenges in memory
- Consider Redis for matchmaking queue
- Profile with `cProfile` or `py-spy`

## Deployment

See [README.md](README.md) for production deployment to Render.

### Environment-specific settings:

**Development:**
- `uvicorn main:app --reload` (auto-reload on code changes)
- Debug mode enabled
- CORS allows localhost

**Production:**
- `uvicorn main:app --host 0.0.0.0 --port 10000` (no reload)
- HTTPS only
- Restricted CORS origins
- MongoDB Atlas (not local)

## Contributing

1. Fork the repository
2. Create your feature branch
3. Write tests for new features
4. Ensure all tests pass
5. Write clear commit messages
6. Submit a pull request

## Need Help?

- **API Docs:** http://localhost:8000/docs
- **FastAPI Docs:** https://fastapi.tiangolo.com/
- **MongoDB Docs:** https://docs.mongodb.com/
- **SymPy Docs:** https://docs.sympy.org/

## Future Improvements

- [ ] Modularize `main.py` into separate files
- [ ] Add comprehensive test suite (pytest)
- [ ] Implement WebSocket for real-time updates
- [ ] Add Redis caching layer
- [ ] Database migrations system
- [ ] CI/CD pipeline (GitHub Actions)
- [ ] Rate limiting
- [ ] Admin dashboard
- [ ] Analytics and metrics
