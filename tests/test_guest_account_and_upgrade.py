"""
Unit and integration tests for guest player persistence, profile is_guest field,
and guest account upgrade functionality.
"""

import pytest
from datetime import datetime, timezone
from jose import jwt
import main
from main import SECRET_KEY, ALGORITHM
from passlib.hash import pbkdf2_sha256


class InMemoryUserStore:
    def __init__(self):
        self.users = {}  # _id -> dict

    async def find_one(self, filter_dict):
        for doc in self.users.values():
            match = True
            for k, v in filter_dict.items():
                if k == "_id":
                    if isinstance(v, dict) and "$ne" in v:
                        if str(doc.get("_id")) == str(v["$ne"]):
                            match = False
                            break
                    else:
                        if str(doc.get("_id")) != str(v):
                            match = False
                            break
                elif isinstance(v, dict) and "$ne" in v:
                    if doc.get(k) == v["$ne"]:
                        match = False
                        break
                else:
                    if doc.get(k) != v:
                        match = False
                        break
            if match:
                return dict(doc)
        return None

    async def insert_one(self, doc):
        doc_copy = dict(doc)
        self.users[str(doc_copy["_id"])] = doc_copy
        return None

    async def update_one(self, filter_dict, update_dict):
        user_id = str(filter_dict.get("_id"))
        doc = self.users.get(user_id)
        if not doc:
            for d in self.users.values():
                if d.get("email") == filter_dict.get("email") or d.get("_id") == filter_dict.get("_id"):
                    doc = d
                    user_id = str(d["_id"])
                    break
        if doc:
            if "$set" in update_dict:
                for k, v in update_dict["$set"].items():
                    doc[k] = v
            if "$inc" in update_dict:
                for k, v in update_dict["$inc"].items():
                    doc[k] = doc.get(k, 0) + v
            self.users[user_id] = doc
            return type("UpdateResult", (), {"modified_count": 1})()
        return type("UpdateResult", (), {"modified_count": 0})()


    def find(self, filter_dict=None, *args, **kwargs):
        filter_dict = filter_dict or {}
        matches = []
        for doc in self.users.values():
            match = True
            for k, v in filter_dict.items():
                if isinstance(v, dict) and "$regex" in v:
                    pattern = v["$regex"]
                    flags = v.get("$options", "")
                    import re
                    regex_flags = re.IGNORECASE if "i" in flags else 0
                    val = str(doc.get(k, ""))
                    if not re.search(pattern, val, regex_flags):
                        match = False
                        break
                elif isinstance(v, dict) and "$ne" in v:
                    if str(doc.get(k)) == str(v["$ne"]):
                        match = False
                        break
                elif doc.get(k) != v:
                    match = False
                    break
            if match:
                matches.append(dict(doc))
        
        class FakeCursor:
            def __init__(self, docs):
                self._docs = docs
            def limit(self, n):
                return FakeCursor(self._docs[:n])
            def sort(self, *args, **kwargs):
                return self
            async def to_list(self, length=None):
                return self._docs[:length] if length else self._docs
        
        return FakeCursor(matches)


@pytest.fixture
def mongo_user_db(monkeypatch):
    store = InMemoryUserStore()
    monkeypatch.setattr(main.users_collection, "find_one", store.find_one)
    monkeypatch.setattr(main.users_collection, "insert_one", store.insert_one)
    monkeypatch.setattr(main.users_collection, "update_one", store.update_one)
    monkeypatch.setattr(main.users_collection, "find", store.find)
    return store


# 1. Guest first request creates DB record
def test_guest_first_request_creates_db_record(client, mongo_user_db, auth_headers):
    res = client.get("/api/user/profile", headers=auth_headers("guest-test-1001"))
    assert res.status_code == 200
    doc = mongo_user_db.users.get("guest-test-1001")
    assert doc is not None
    assert doc["_id"] == "guest-test-1001"
    assert doc["is_guest"] is True
    assert doc["elo"] == 1000
    assert doc["wins"] == 0
    assert doc["losses"] == 0
    assert doc["username"] == "Guest_1001"


# 2. Guest subsequent request retrieves existing DB record
def test_guest_subsequent_request_retrieves_existing_db_record(client, mongo_user_db, auth_headers):
    mongo_user_db.users["guest-test-1002"] = {
        "_id": "guest-test-1002",
        "email": "guest-test-1002@derivative-duel.com",
        "name": "Guest 1002",
        "username": "Guest_1002",
        "elo": 1250,
        "wins": 5,
        "losses": 2,
        "is_guest": True,
        "created_at": datetime.now(timezone.utc)
    }
    res = client.get("/api/user/profile", headers=auth_headers("guest-test-1002"))
    assert res.status_code == 200
    data = res.json()
    assert data["elo"] == 1250
    assert data["wins"] == 5
    assert data["losses"] == 2
    assert data["is_guest"] is True


# 3. Guest profile response contains is_guest: True
def test_guest_profile_includes_is_guest_true(client, mongo_user_db, auth_headers):
    res = client.get("/api/user/profile", headers=auth_headers("guest-test-1003"))
    assert res.status_code == 200
    assert res.json()["is_guest"] is True


# 4. Guest profile contains expected fields
def test_guest_profile_contains_expected_fields(client, mongo_user_db, auth_headers):
    res = client.get("/api/user/profile", headers=auth_headers("guest-test-1004"))
    assert res.status_code == 200
    data = res.json()
    for field in ["id", "email", "name", "username", "elo", "wins", "losses", "is_guest"]:
        assert field in data


# 5. Registered user profile contains is_guest: False
def test_registered_user_profile_includes_is_guest_false(client, mongo_user_db):
    mongo_user_db.users["user-reg-1"] = {
        "_id": "user-reg-1",
        "email": "registered@example.com",
        "name": "RegUser",
        "username": "RegUser",
        "elo": 1100,
        "wins": 3,
        "losses": 1,
        "is_guest": False
    }
    token = main.create_access_token({"sub": "registered@example.com"})
    res = client.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["is_guest"] is False


# 6. Successful upgrade returns access token
def test_upgrade_guest_success(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-1"))
    
    payload = {
        "email": "newuser@example.com",
        "username": "newuser",
        "password": "Password123!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-1"))
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


# 7. Upgraded user profile returns is_guest: False with new JWT
def test_upgraded_user_profile_returns_is_guest_false(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-2"))
    
    payload = {
        "email": "upgraded2@example.com",
        "username": "upgraded2",
        "password": "Password123!"
    }
    upg_res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-2"))
    token = upg_res.json()["access_token"]
    
    prof_res = client.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})
    assert prof_res.status_code == 200
    data = prof_res.json()
    assert data["is_guest"] is False


# 8. Upgraded user profile returns new email and username
def test_upgraded_user_profile_returns_new_email_and_username(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-3"))
    
    payload = {
        "email": "upgraded3@example.com",
        "username": "upgraded3_name",
        "password": "Password123!"
    }
    upg_res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-3"))
    token = upg_res.json()["access_token"]
    
    prof_res = client.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})
    data = prof_res.json()
    assert data["email"] == "upgraded3@example.com"
    assert data["username"] == "upgraded3_name"
    assert data["name"] == "upgraded3_name"


# 9. Upgraded user preserves ELO and match stats
def test_upgraded_user_preserves_elo_and_match_stats(client, mongo_user_db, auth_headers):
    mongo_user_db.users["guest-upgrade-4"] = {
        "_id": "guest-upgrade-4",
        "email": "guest-upgrade-4@derivative-duel.com",
        "name": "Guest ade4",
        "username": "Guest_ade4",
        "elo": 1420,
        "wins": 12,
        "losses": 4,
        "is_guest": True,
        "created_at": datetime.now(timezone.utc)
    }
    
    payload = {
        "email": "upgraded4@example.com",
        "username": "upgraded4",
        "password": "Password123!"
    }
    upg_res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-4"))
    token = upg_res.json()["access_token"]
    
    prof_res = client.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})
    data = prof_res.json()
    assert data["elo"] == 1420
    assert data["wins"] == 12
    assert data["losses"] == 4


# 10. Upgrade rejects non-guest user
def test_upgrade_rejects_non_guest_user(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-5"))
    payload = {
        "email": "upgraded5@example.com",
        "username": "upgraded5",
        "password": "Password123!"
    }
    upg_res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-5"))
    token = upg_res.json()["access_token"]
    
    second_res = client.post("/api/user/upgrade-guest", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert second_res.status_code == 400
    assert "Only guest accounts" in second_res.json()["detail"]


# 11. Upgrade rejects duplicate email
def test_upgrade_rejects_duplicate_email(client, mongo_user_db, auth_headers):
    mongo_user_db.users["existing-user-1"] = {
        "_id": "existing-user-1",
        "email": "taken@example.com",
        "username": "existing1",
        "is_guest": False
    }
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-6"))
    payload = {
        "email": "taken@example.com",
        "username": "unique6",
        "password": "Password123!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-6"))
    assert res.status_code == 400
    assert "Email is already taken" in res.json()["detail"]


# 12. Upgrade rejects duplicate username
def test_upgrade_rejects_duplicate_username(client, mongo_user_db, auth_headers):
    mongo_user_db.users["existing-user-2"] = {
        "_id": "existing-user-2",
        "email": "user2@example.com",
        "username": "taken_username",
        "is_guest": False
    }
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-7"))
    payload = {
        "email": "unique7@example.com",
        "username": "taken_username",
        "password": "Password123!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-7"))
    assert res.status_code == 400
    assert "Username is already taken" in res.json()["detail"]


# 13. Upgrade email is converted to lowercase
def test_upgrade_email_is_case_insensitive(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-8"))
    payload = {
        "email": "MixedCase8@Example.COM",
        "username": "user8",
        "password": "Password123!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-8"))
    assert res.status_code == 200
    doc = mongo_user_db.users.get("guest-upgrade-8")
    assert doc["email"] == "mixedcase8@example.com"


# 14. Upgrade duplicate email check is case-insensitive
def test_upgrade_duplicate_email_case_insensitive_check(client, mongo_user_db, auth_headers):
    mongo_user_db.users["existing-user-3"] = {
        "_id": "existing-user-3",
        "email": "lowercase3@example.com",
        "username": "user3",
        "is_guest": False
    }
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-9"))
    payload = {
        "email": "LOWERCASE3@EXAMPLE.COM",
        "username": "user9",
        "password": "Password123!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-9"))
    assert res.status_code == 400
    assert "Email is already taken" in res.json()["detail"]


# 15. Password is correctly hashed with pbkdf2_sha256
def test_upgrade_password_is_hashed(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-10"))
    payload = {
        "email": "user10@example.com",
        "username": "user10",
        "password": "SecretPassword10!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-10"))
    assert res.status_code == 200
    doc = mongo_user_db.users.get("guest-upgrade-10")
    hashed = doc["hashed_password"]
    assert hashed != "SecretPassword10!"
    assert pbkdf2_sha256.verify("SecretPassword10!", hashed)


# 16. Empty email rejected
def test_upgrade_with_empty_email_rejected(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-11"))
    payload = {
        "email": "   ",
        "username": "user11",
        "password": "Password123!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-11"))
    assert res.status_code == 400


# 17. Empty username rejected
def test_upgrade_with_empty_username_rejected(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-12"))
    payload = {
        "email": "user12@example.com",
        "username": "  ",
        "password": "Password123!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-12"))
    assert res.status_code == 400


# 18. Empty password rejected
def test_upgrade_with_empty_password_rejected(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-13"))
    payload = {
        "email": "user13@example.com",
        "username": "user13",
        "password": ""
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-13"))
    assert res.status_code == 400


# 19. Default guest upgrade
def test_upgrade_default_guest_user(client, mongo_user_db):
    payload = {
        "email": "defaultguest@example.com",
        "username": "defguest",
        "password": "Password123!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload)
    assert res.status_code == 200
    assert "access_token" in res.json()


# 20. Returned JWT token contains sub = email
def test_upgrade_jwt_token_payload_sub_field(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-upgrade-20"))
    payload = {
        "email": "user20@example.com",
        "username": "user20",
        "password": "Password123!"
    }
    res = client.post("/api/user/upgrade-guest", json=payload, headers=auth_headers("guest-upgrade-20"))
    token = res.json()["access_token"]
    decoded = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    assert decoded.get("sub") == "user20@example.com"


# 21. Multiple guests have isolated DB documents
def test_multiple_guests_have_independent_db_documents(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-multi-1"))
    client.get("/api/user/profile", headers=auth_headers("guest-multi-2"))
    assert "guest-multi-1" in mongo_user_db.users
    assert "guest-multi-2" in mongo_user_db.users
    assert mongo_user_db.users["guest-multi-1"]["username"] == "Guest_ti-1"
    assert mongo_user_db.users["guest-multi-2"]["username"] == "Guest_ti-2"


# 22. Multiple guests can upgrade independently
def test_multiple_guests_can_upgrade_independently(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-multi-3"))
    client.get("/api/user/profile", headers=auth_headers("guest-multi-4"))
    
    r1 = client.post("/api/user/upgrade-guest", json={
        "email": "multi3@example.com", "username": "multi3", "password": "Password123!"
    }, headers=auth_headers("guest-multi-3"))
    
    r2 = client.post("/api/user/upgrade-guest", json={
        "email": "multi4@example.com", "username": "multi4", "password": "Password123!"
    }, headers=auth_headers("guest-multi-4"))
    
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert mongo_user_db.users["guest-multi-3"]["is_guest"] is False
    assert mongo_user_db.users["guest-multi-4"]["is_guest"] is False


# 23. ELO updates in DB reflect in profile endpoint for guest
def test_guest_elo_update_in_db_persists_across_calls(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-elo-1"))
    mongo_user_db.users["guest-elo-1"]["elo"] = 1150
    res = client.get("/api/user/profile", headers=auth_headers("guest-elo-1"))
    assert res.json()["elo"] == 1150


# 24. Win/loss updates in DB reflect in profile endpoint for guest
def test_guest_win_loss_update_in_db_persists_across_calls(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-wl-1"))
    mongo_user_db.users["guest-wl-1"]["wins"] = 3
    mongo_user_db.users["guest-wl-1"]["losses"] = 1
    res = client.get("/api/user/profile", headers=auth_headers("guest-wl-1"))
    assert res.json()["wins"] == 3
    assert res.json()["losses"] == 1


# 25. Upgraded user document preserves document ID
def test_upgrade_preserves_document_id(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-id-1"))
    client.post("/api/user/upgrade-guest", json={
        "email": "preserveid@example.com", "username": "preserveid", "password": "Password123!"
    }, headers=auth_headers("guest-id-1"))
    assert "guest-id-1" in mongo_user_db.users
    assert mongo_user_db.users["guest-id-1"]["email"] == "preserveid@example.com"


# 26. Guest username formatting default
def test_guest_username_default_format(client, mongo_user_db, auth_headers):
    res = client.get("/api/user/profile", headers=auth_headers("guest-uuid-9876"))
    assert res.json()["username"] == "Guest_9876"


# 27. Upgrade strips whitespace from email and username
def test_upgrade_strips_whitespace_from_email_and_username(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-ws-1"))
    res = client.post("/api/user/upgrade-guest", json={
        "email": "   spaced@example.com   ",
        "username": "   spaced_user   ",
        "password": "Password123!"
    }, headers=auth_headers("guest-ws-1"))
    assert res.status_code == 200
    doc = mongo_user_db.users["guest-ws-1"]
    assert doc["email"] == "spaced@example.com"
    assert doc["username"] == "spaced_user"


# 28. Upgraded account has updated_at timestamp
def test_upgraded_account_has_updated_at_timestamp(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-ts-1"))
    client.post("/api/user/upgrade-guest", json={
        "email": "ts1@example.com", "username": "ts1", "password": "Password123!"
    }, headers=auth_headers("guest-ts-1"))
    doc = mongo_user_db.users["guest-ts-1"]
    assert "updated_at" in doc


# 29. Guest account has created_at timestamp
def test_guest_account_created_at_timestamp_exists(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-ts-2"))
    doc = mongo_user_db.users["guest-ts-2"]
    assert "created_at" in doc


# 30. Upgrade with long password works
def test_upgrade_with_long_password(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-pw-long"))
    long_pw = "VeryLongAndComplexPassword!@#$%^&*()_+" * 5
    res = client.post("/api/user/upgrade-guest", json={
        "email": "longpw@example.com", "username": "longpw", "password": long_pw
    }, headers=auth_headers("guest-pw-long"))
    assert res.status_code == 200
    doc = mongo_user_db.users["guest-pw-long"]
    assert pbkdf2_sha256.verify(long_pw, doc["hashed_password"])


# 31. Upgraded user can use set-username endpoint
def test_upgraded_account_can_update_username_via_set_username(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-setuser-1"))
    upg = client.post("/api/user/upgrade-guest", json={
        "email": "setuser1@example.com", "username": "initialname", "password": "Password123!"
    }, headers=auth_headers("guest-setuser-1"))
    token = upg.json()["access_token"]
    
    set_res = client.post("/api/user/set-username", json={"username": "newname123"}, headers={"Authorization": f"Bearer {token}"})
    assert set_res.status_code == 200
    
    prof = client.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})
    assert prof.json()["username"] == "newname123"


# 32. Upgrade request invalid JSON payload returns 422
def test_upgrade_invalid_payload(client, auth_headers):
    res = client.post("/api/user/upgrade-guest", json={"invalid": "data"}, headers=auth_headers("guest-inv-1"))
    assert res.status_code == 422


# 33. Upgraded user can participate in daily challenge lookup
def test_upgraded_user_can_get_daily_challenge(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-daily-1"))
    upg = client.post("/api/user/upgrade-guest", json={
        "email": "daily1@example.com", "username": "daily1", "password": "Password123!"
    }, headers=auth_headers("guest-daily-1"))
    token = upg.json()["access_token"]
    
    res = client.get("/api/daily-challenge/today", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert "expression" in res.json()


# 34. Upgraded user can get pending challenges
def test_upgraded_user_can_get_pending_challenges(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-pend-1"))
    upg = client.post("/api/user/upgrade-guest", json={
        "email": "pend1@example.com", "username": "pend1", "password": "Password123!"
    }, headers=auth_headers("guest-pend-1"))
    token = upg.json()["access_token"]
    
    res = client.get("/api/challenges/pending", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json() == []


# 35. Upgraded user active match endpoint returns null when no active match
def test_upgraded_user_get_active_match(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-act-1"))
    upg = client.post("/api/user/upgrade-guest", json={
        "email": "act1@example.com", "username": "act1", "password": "Password123!"
    }, headers=auth_headers("guest-act-1"))
    token = upg.json()["access_token"]
    
    res = client.get("/api/game/active", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["has_active_match"] is False


# 36. User profile model validation handles missing fields gracefully with defaults
def test_profile_model_defaults(client, mongo_user_db, auth_headers):
    mongo_user_db.users["guest-def-1"] = {
        "_id": "guest-def-1",
        "email": "guest-def-1@derivative-duel.com",
        "name": "Guest def1",
        "elo": 1000,
        "wins": 0,
        "losses": 0
    }
    res = client.get("/api/user/profile", headers=auth_headers("guest-def-1"))
    assert res.status_code == 200
    assert res.json()["is_guest"] is False


# 37. Upgrade with invalid email format rejected
def test_upgrade_invalid_email_format_rejected(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-fmt-1"))
    res = client.post("/api/user/upgrade-guest", json={
        "email": "invalidemailformat",
        "username": "validuser",
        "password": "Password123!"
    }, headers=auth_headers("guest-fmt-1"))
    assert res.status_code == 400
    assert "Invalid email address format" in res.json()["detail"]


# 38. Upgrade with too short username rejected (< 3 chars)
def test_upgrade_too_short_username_rejected(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-short-usr"))
    res = client.post("/api/user/upgrade-guest", json={
        "email": "valid@example.com",
        "username": "ab",
        "password": "Password123!"
    }, headers=auth_headers("guest-short-usr"))
    assert res.status_code == 400
    assert "Username must be between 3 and 20 characters" in res.json()["detail"]


# 39. Upgrade with too long username rejected (> 20 chars)
def test_upgrade_too_long_username_rejected(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-long-usr"))
    res = client.post("/api/user/upgrade-guest", json={
        "email": "valid@example.com",
        "username": "a" * 21,
        "password": "Password123!"
    }, headers=auth_headers("guest-long-usr"))
    assert res.status_code == 400
    assert "Username must be between 3 and 20 characters" in res.json()["detail"]


# 40. Upgrade with too short password rejected (< 6 chars)
def test_upgrade_too_short_password_rejected(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-short-pw"))
    res = client.post("/api/user/upgrade-guest", json={
        "email": "valid@example.com",
        "username": "validuser",
        "password": "12345"
    }, headers=auth_headers("guest-short-pw"))
    assert res.status_code == 400
    assert "Password must be at least 6 characters long" in res.json()["detail"]


# 41. Upgraded JWT token contains valid expiration field
def test_upgraded_jwt_token_has_valid_expiration(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-exp-1"))
    upg = client.post("/api/user/upgrade-guest", json={
        "email": "exp1@example.com",
        "username": "exp1user",
        "password": "Password123!"
    }, headers=auth_headers("guest-exp-1"))
    token = upg.json()["access_token"]
    decoded = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    assert "exp" in decoded
    assert decoded["exp"] > datetime.utcnow().timestamp()


# 42. Upgraded account can fetch user info via /api/user/me
def test_upgraded_account_user_info_endpoint(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-me-1"))
    upg = client.post("/api/user/upgrade-guest", json={
        "email": "me1@example.com",
        "username": "me1user",
        "password": "Password123!"
    }, headers=auth_headers("guest-me-1"))
    token = upg.json()["access_token"]
    
    res = client.get("/api/user/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.json()
    assert data["email"] == "me1@example.com"
    assert data["username"] == "me1user"


# 43. Upgraded user search can find newly upgraded account
def test_search_finds_upgraded_account_by_username(client, mongo_user_db, auth_headers):
    client.get("/api/user/profile", headers=auth_headers("guest-srch-1"))
    client.post("/api/user/upgrade-guest", json={
        "email": "srch1@example.com",
        "username": "findme_user",
        "password": "Password123!"
    }, headers=auth_headers("guest-srch-1"))
    
    res = client.get("/api/users/search?username=findme", headers=auth_headers("guest-srch-2"))
    assert res.status_code == 200
    usernames = [u.get("username") for u in res.json()]
    assert "findme_user" in usernames


# 44. Upgraded user ELO and stats are preserved when queried via /api/user/me
def test_upgraded_account_preserves_custom_elo_in_user_info(client, mongo_user_db, auth_headers):
    mongo_user_db.users["guest-elo-me"] = {
        "_id": "guest-elo-me",
        "email": "guest-elo-me@derivative-duel.com",
        "name": "Guest elo-me",
        "username": "Guest_lo-me",
        "elo": 1550,
        "wins": 15,
        "losses": 3,
        "is_guest": True,
        "created_at": datetime.now(timezone.utc)
    }
    upg = client.post("/api/user/upgrade-guest", json={
        "email": "elome@example.com",
        "username": "elomeuser",
        "password": "Password123!"
    }, headers=auth_headers("guest-elo-me"))
    token = upg.json()["access_token"]
    
    res = client.get("/api/user/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["elo"] == 1550


# 45. DB lookup failure in get_current_user degrades gracefully to default guest
def test_get_current_user_db_failure_degrades_gracefully(client, monkeypatch, auth_headers):
    async def _failing_find_one(*args, **kwargs):
        raise RuntimeError("DB connection down")
    
    monkeypatch.setattr(main.users_collection, "find_one", _failing_find_one)
    res = client.get("/api/user/profile", headers=auth_headers("guest-fail-1"))
    assert res.status_code == 200
    assert res.json()["is_guest"] is True

