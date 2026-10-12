"""
Regression tests for the daily challenge submit/leaderboard flow.

These pin down a bug where a submission without a numeric time stored ``None``
and then crashed the rank comparison and leaderboard sort for every later
player. Time is now validated and coerced up front.
"""

from datetime import datetime, timezone

import main


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _seed_challenge():
    """Put a deterministic challenge in memory so submit does not 404."""
    today = _today()
    main.daily_challenges_storage[today] = {
        "date": today,
        "expression": "x^2",
        "derivative": "2·x",
        "answer": "2·x",
        "difficulty": 1,
    }
    return today


def _submit(client, guest_id: str, answer: str, payload_extra: dict):
    body = {"answer": answer, **payload_extra}
    return client.post(
        "/api/daily-challenge/submit",
        json=body,
        headers={"Authorization": f"Bearer {guest_id}"},
    )


def test_submit_rejects_missing_time(client):
    _seed_challenge()
    try:
        res = _submit(client, "guest-aaaa", "2*x", {})
        assert res.status_code == 400
        assert "time" in res.json()["detail"].lower()
    finally:
        main.daily_challenges_storage.pop(_today(), None)


def test_submit_rejects_non_numeric_time(client):
    _seed_challenge()
    try:
        res = _submit(client, "guest-aaaa", "2*x", {"time": "fast"})
        assert res.status_code == 400
    finally:
        main.daily_challenges_storage.pop(_today(), None)


def test_second_submission_ranks_without_crash(client):
    """A prior valid submission must not break a later player's rank calc."""
    _seed_challenge()
    try:
        first = _submit(client, "guest-aaaa", "2*x", {"time": 10.0})
        assert first.status_code == 200
        assert first.json()["rank"] == 1

        second = _submit(client, "guest-bbbb", "2*x", {"time": 5.0})
        assert second.status_code == 200
        # Faster time beats the earlier 10.0s submission.
        assert second.json()["rank"] == 1

        third = _submit(client, "guest-cccc", "2*x", {"time": 20.0})
        assert third.status_code == 200
        assert third.json()["rank"] == 3
    finally:
        main.daily_challenges_storage.pop(_today(), None)


def test_time_taken_zero_is_accepted(client):
    """A legitimate 0.0 second time must not be treated as missing."""
    _seed_challenge()
    try:
        res = _submit(client, "guest-aaaa", "2*x", {"time": 0})
        assert res.status_code == 200
        assert res.json()["time_taken"] == 0.0
    finally:
        main.daily_challenges_storage.pop(_today(), None)


def test_leaderboard_sorts_after_submissions(client):
    _seed_challenge()
    try:
        _submit(client, "guest-aaaa", "2*x", {"time": 8.0})
        _submit(client, "guest-bbbb", "2*x", {"time": 3.0})
        res = client.get(
            "/api/daily-challenge/leaderboard",
            headers={"Authorization": "Bearer guest-aaaa"},
        )
        assert res.status_code == 200
        times = [row["time"] for row in res.json()]
        assert times == sorted(times)
        assert times[0] == 3.0
    finally:
        main.daily_challenges_storage.pop(_today(), None)


def test_duplicate_correct_submission_blocked(client):
    _seed_challenge()
    try:
        first = _submit(client, "guest-aaaa", "2*x", {"time": 10.0})
        assert first.status_code == 200
        again = _submit(client, "guest-aaaa", "2*x", {"time": 4.0})
        assert again.status_code == 400
    finally:
        main.daily_challenges_storage.pop(_today(), None)


def test_generate_daily_challenge_determinism():
    """Generating challenge for the same date must always yield identical results."""
    c1 = main.generate_daily_challenge("2026-10-15")
    c2 = main.generate_daily_challenge("2026-10-15")
    assert c1 == c2
    assert c1["date"] == "2026-10-15"
    assert "expression" in c1
    assert "derivative" in c1
    assert "difficulty" in c1
    assert "category" in c1


def test_generate_daily_challenge_weekly_schedule():
    """
    Weekly schedule:
    - Monday: easy (Speed Run)
    - Tuesday, Wednesday: medium (Chain Rule)
    - Thursday - Sunday: hard
    """
    # 2026-10-12 is Monday
    mon = main.generate_daily_challenge("2026-10-12")
    assert mon["difficulty"] == "easy"
    assert mon["category"] == "Speed Run"

    # 2026-10-13 is Tuesday
    tue = main.generate_daily_challenge("2026-10-13")
    assert tue["difficulty"] == "medium"
    assert tue["category"] == "Chain Rule"

    # 2026-10-14 is Wednesday
    wed = main.generate_daily_challenge("2026-10-14")
    assert wed["difficulty"] == "medium"
    assert wed["category"] == "Chain Rule"

    # 2026-10-15 is Thursday
    thu = main.generate_daily_challenge("2026-10-15")
    assert thu["difficulty"] == "hard"

    # 2026-10-16 is Friday
    fri = main.generate_daily_challenge("2026-10-16")
    assert fri["difficulty"] == "hard"

    # 2026-10-17 is Saturday
    sat = main.generate_daily_challenge("2026-10-17")
    assert sat["difficulty"] == "hard"

    # 2026-10-18 is Sunday
    sun = main.generate_daily_challenge("2026-10-18")
    assert sun["difficulty"] == "hard"


def test_generate_daily_challenge_sympy_equivalence_across_year():
    """Every generated question across an entire year must be mathematically valid and equivalent to its derivative."""
    from datetime import date, timedelta
    start = date(2026, 1, 1)
    for i in range(365):
        d_str = (start + timedelta(days=i)).isoformat()
        ch = main.generate_daily_challenge(d_str)
        assert ch["difficulty"] in ("easy", "medium", "hard")
        assert len(ch["expression"]) > 0
        assert len(ch["derivative"]) > 0
        # Check that the answer passes check_math_equivalence
        assert main.check_math_equivalence(ch["answer"], ch["derivative"]) is True


def test_normalize_daily_difficulty():
    """Normalize old numerical or string difficulties accurately."""
    assert main.normalize_daily_difficulty({"difficulty": "hard", "category": "Product Rule"}) == ("hard", "Product Rule")
    assert main.normalize_daily_difficulty({"difficulty": "easy"}) == ("easy", "Speed Run")
    assert main.normalize_daily_difficulty({"expression": "cos(x)"}) == ("easy", "Speed Run")
    assert main.normalize_daily_difficulty({"expression": "3·x·cos(2·x)"}) == ("hard", "Product Rule")
    assert main.normalize_daily_difficulty({"expression": "sin(2·x)"}) == ("medium", "Chain Rule")


def test_get_daily_challenge_endpoint_returns_difficulty(client, mock_mongo):
    """GET /api/daily-challenge must return difficulty and category fields."""
    today = _today()
    main.daily_challenges_storage.pop(today, None)
    res = client.get(
        "/api/daily-challenge/today",
        headers={"Authorization": "Bearer guest-test-user"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["date"] == today
    assert "expression" in data
    assert data["difficulty"] in ("easy", "medium", "hard")
    assert isinstance(data["category"], str)
    assert len(data["category"]) > 0


def test_submit_without_prior_get_succeeds_with_fallback(client, mock_mongo):
    """Submitting directly without hitting GET first auto-generates challenge and succeeds."""
    today = _today()
    main.daily_challenges_storage.pop(today, None)
    # Generate expected challenge to get correct answer
    expected = main.generate_daily_challenge(today)
    res = _submit(client, "guest-auto-gen", expected["answer"], {"time": 4.5})
    assert res.status_code == 200
    assert res.json()["correct"] is True
    assert res.json()["rank"] == 1

