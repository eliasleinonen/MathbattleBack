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
