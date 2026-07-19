"""
Shared pytest fixtures for Derivative Duel API tests.

Mongo is mocked so tests exercise in-memory game state without a real database.
"""

import os

import pytest
from fastapi.testclient import TestClient

# Ensure predictable env before importing the app module.
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017")

import main  # noqa: E402


class _FakeUpdateResult:
    modified_count = 1
    upserted_id = None
    matched_count = 1


class _FakeCursor:
    def __init__(self, docs=None):
        self._docs = docs or []

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        if length is None:
            return list(self._docs)
        return list(self._docs)[:length]


async def _mock_find_one(*args, **kwargs):
    return None


async def _mock_insert_one(*args, **kwargs):
    return None


async def _mock_update_one(*args, **kwargs):
    return _FakeUpdateResult()


def _mock_find(*args, **kwargs):
    return _FakeCursor([])


def _auth(guest_id: str) -> dict:
    return {"Authorization": f"Bearer {guest_id}"}


@pytest.fixture
def auth_headers():
    return _auth


@pytest.fixture(autouse=True)
def reset_game_state():
    """Clear process-local game state between tests."""
    main.in_memory_matches.clear()
    main.in_memory_rounds.clear()
    main.in_memory_users.clear()
    main.matchmaking_queue.clear()
    main.cancelled_users.clear()
    main.time_trials.clear()
    main.daily_completions_storage.clear()
    main.match_counter = 0
    main.round_counter = 0
    main.user_counter = 0
    yield


@pytest.fixture
def mock_mongo(monkeypatch):
    """Stub all Motor collection methods used by gameplay routes."""
    collections = [
        main.users_collection,
        main.matches_collection,
        main.rounds_collection,
        main.daily_challenges_collection,
        main.daily_completions_collection,
    ]
    for collection in collections:
        monkeypatch.setattr(collection, "find_one", _mock_find_one)
        monkeypatch.setattr(collection, "insert_one", _mock_insert_one)
        monkeypatch.setattr(collection, "update_one", _mock_update_one)
        monkeypatch.setattr(collection, "find", _mock_find)


@pytest.fixture
def client(mock_mongo):
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def fixed_question(monkeypatch):
    """Deterministic question so answer checks don't depend on RNG/HTML forms."""

    def _generate(_elo: int) -> dict:
        return {
            "expression": "x^2",
            "derivative": "2·x",
            "evaluate_at": 0,
            "answer": "2·x",
            "difficulty": 1,
            "ask_for_derivative_only": True,
        }

    monkeypatch.setattr(main, "generate_question", _generate)
    return _generate
