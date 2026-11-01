"""
Tests for Push Notification endpoints and 60-second queue / bot-match shortcuts.
"""

import pytest
import main


def test_get_vapid_public_key(client):
    res = client.get("/api/notifications/vapid-public-key")
    assert res.status_code == 200
    data = res.json()
    assert "public_key" in data
    assert len(data["public_key"]) > 30


def test_subscribe_and_unsubscribe_push_notifications(client, auth_headers, monkeypatch):
    stored_subs = {}

    async def mock_update(filt, update, upsert=False):
        stored_subs[filt["endpoint"]] = update["$set"]
        class R:
            modified_count = 1
        return R()

    async def mock_delete(filt):
        stored_subs.pop(filt["endpoint"], None)
        class R:
            deleted_count = 1
        return R()

    monkeypatch.setattr(main.push_subscriptions_collection, "update_one", mock_update)
    monkeypatch.setattr(main.push_subscriptions_collection, "delete_one", mock_delete)

    payload = {
        "endpoint": "https://push.example.com/sub/12345",
        "keys": {
            "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QT9AcDnVwTJaWwVuWYxgkVsvcUzWxj3qajioO6SJIBaImf2A",
            "auth": "tBHItJI5svbpez7KI4CCXg"
        }
    }

    # Subscribe
    res = client.post(
        "/api/notifications/subscribe",
        json=payload,
        headers=auth_headers("guest-sub-1")
    )
    assert res.status_code == 200
    assert res.json()["status"] == "subscribed"
    assert "https://push.example.com/sub/12345" in stored_subs
    assert stored_subs["https://push.example.com/sub/12345"]["user_id"] == "guest-sub-1"

    # Unsubscribe
    unsub_res = client.post(
        "/api/notifications/unsubscribe",
        json={"endpoint": "https://push.example.com/sub/12345"},
        headers=auth_headers("guest-sub-1")
    )
    assert unsub_res.status_code == 200
    assert unsub_res.json()["status"] == "unsubscribed"
    assert "https://push.example.com/sub/12345" not in stored_subs


def test_force_bot_bypasses_queue_instantly(client, auth_headers):
    # force_bot = True starts match immediately against bot
    res = client.post(
        "/api/game/start",
        json={"mode": "random", "force_bot": True},
        headers=auth_headers("guest-speedy-1")
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "matched"
    assert "(bot)" in data["opponent"]

    # Verify user was never left in queue
    assert "guest-speedy-1" not in main.matchmaking_queue


def test_mode_bot_bypasses_queue_instantly(client, auth_headers):
    res = client.post(
        "/api/game/start",
        json={"mode": "bot"},
        headers=auth_headers("guest-speedy-2")
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "matched"
    assert "(bot)" in data["opponent"]
    assert "guest-speedy-2" not in main.matchmaking_queue


def test_random_queue_has_60s_countdown(client, auth_headers):
    res = client.post(
        "/api/game/start",
        json={"mode": "random"},
        headers=auth_headers("guest-waiter-1")
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "searching"
    assert data["time_remaining"] == 60
    assert "guest-waiter-1" in main.matchmaking_queue
